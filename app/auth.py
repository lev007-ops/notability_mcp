"""OAuth 2.1 authorization server for the MCP endpoint.

Single-user: the "identity provider" is a password login page served by this app.
Clients (claude.ai, Claude Code, ...) register dynamically, then go through
authorization code + PKCE. Tokens are opaque random strings; only their SHA-256
is stored, in SQLite, so a leaked database can't be replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import Settings
from .security import Throttle, verify_password

CODE_TTL = 300
PENDING_TTL = 600
SUBJECT = "owner"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    expires_at: float


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL);
                CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens (
                    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL, data TEXT NOT NULL, expires REAL
                );
                CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
                """
            )

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def purge(self) -> None:
        now = time.time()
        self.q("DELETE FROM codes WHERE expires < ?", (now,))
        self.q("DELETE FROM tokens WHERE expires IS NOT NULL AND expires < ?", (now,))


class _StoredAccess(AccessToken):
    grant_id: str


class _StoredRefresh(RefreshToken):
    grant_id: str


class OwnerOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(settings.data_dir / "auth.sqlite3")
        self.pending: dict[str, _Pending] = {}
        self.throttle = Throttle(max_failures=5, window=900, lockout=900, global_max=30)

    # ---- clients -------------------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.store.q("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            host = urlparse(str(uri)).hostname or ""
            if host not in self.s.allowed_redirect_hosts:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect host {host!r} is not allowed (ALLOWED_REDIRECT_HOSTS)",
                )
        n = self.store.q("SELECT COUNT(*) FROM clients")[0][0]
        if n >= 500:
            raise RegistrationError("invalid_client_metadata", "too many registered clients")
        self.store.q(
            "INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
            (client_info.client_id, client_info.model_dump_json(), time.time()),
        )

    # ---- authorize -> login page -----------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.s.mcp_url:
            raise AuthorizeError("invalid_target", "unknown resource")
        now = time.time()
        for k in [k for k, v in self.pending.items() if v.expires_at < now]:
            self.pending.pop(k, None)
        if len(self.pending) > 200:
            raise AuthorizeError("temporarily_unavailable", "too many pending logins")
        req_id = secrets.token_urlsafe(32)
        self.pending[req_id] = _Pending(
            client_id=client.client_id or "",
            client_name=client.client_name or client.client_id or "unknown",
            params=params,
            expires_at=now + PENDING_TTL,
        )
        return f"{self.s.public_url}/login?req={req_id}"

    async def complete_login(self, req_id: str) -> str:
        p = self.pending.pop(req_id)
        code = secrets.token_urlsafe(40)
        ac = AuthorizationCode(
            code=code,
            scopes=p.params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=p.client_id,
            code_challenge=p.params.code_challenge,
            redirect_uri=p.params.redirect_uri,
            redirect_uri_provided_explicitly=p.params.redirect_uri_provided_explicitly,
            resource=p.params.resource or self.s.mcp_url,
            subject=SUBJECT,
        )
        self.store.q("INSERT INTO codes VALUES (?, ?, ?)", (_h(code), ac.model_dump_json(), ac.expires_at))
        return construct_redirect_uri(str(p.params.redirect_uri), code=code, state=p.params.state)

    # ---- codes & tokens -----------------------------------------------------------------
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        rows = self.store.q("SELECT data FROM codes WHERE hash = ?", (_h(authorization_code),))
        if not rows:
            return None
        ac = AuthorizationCode.model_validate_json(rows[0][0])
        if ac.client_id != client.client_id or ac.expires_at < time.time():
            return None
        return ac

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, grant_id: str) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        a = _StoredAccess(
            token="", client_id=client_id, scopes=scopes, expires_at=now + self.s.access_token_ttl,
            resource=resource, subject=SUBJECT, grant_id=grant_id,
        )
        r = _StoredRefresh(
            token="", client_id=client_id, scopes=scopes, expires_at=now + self.s.refresh_token_ttl,
            resource=resource, subject=SUBJECT, grant_id=grant_id,
        )
        self.store.q(
            "INSERT INTO tokens VALUES (?, 'access', ?, ?, ?, ?)",
            (_h(access), client_id, grant_id, a.model_dump_json(), a.expires_at),
        )
        self.store.q(
            "INSERT INTO tokens VALUES (?, 'refresh', ?, ?, ?, ?)",
            (_h(refresh), client_id, grant_id, r.model_dump_json(), r.expires_at),
        )
        self.store.purge()
        return OAuthToken(
            access_token=access,
            expires_in=self.s.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # single use: delete first; if it was already gone, someone replayed it
        with self.store._lock:
            cur = self.store._db.execute("DELETE FROM codes WHERE hash = ?", (_h(authorization_code.code),))
            if cur.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(
            client.client_id or "", authorization_code.scopes, authorization_code.resource, secrets.token_hex(16)
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> _StoredRefresh | None:
        rows = self.store.q("SELECT data FROM tokens WHERE hash = ? AND kind = 'refresh'", (_h(refresh_token),))
        if not rows:
            return None
        rt = _StoredRefresh.model_validate_json(rows[0][0])
        if rt.client_id != client.client_id or (rt.expires_at and rt.expires_at < time.time()):
            return None
        rt.token = refresh_token
        return rt

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: _StoredRefresh, scopes: list[str]
    ) -> OAuthToken:
        # rotation: the whole old grant (old refresh + its access tokens) is invalidated
        with self.store._lock:
            cur = self.store._db.execute("DELETE FROM tokens WHERE hash = ?", (_h(refresh_token.token),))
            if cur.rowcount != 1:
                raise TokenError("invalid_grant", "refresh token already used")
            self.store._db.execute("DELETE FROM tokens WHERE grant_id = ?", (refresh_token.grant_id,))
        return self._issue(
            client.client_id or "", scopes or refresh_token.scopes, refresh_token.resource, refresh_token.grant_id
        )

    async def load_access_token(self, token: str) -> _StoredAccess | None:
        rows = self.store.q("SELECT data FROM tokens WHERE hash = ? AND kind = 'access'", (_h(token),))
        if not rows:
            return None
        at = _StoredAccess.model_validate_json(rows[0][0])
        if at.expires_at and at.expires_at < time.time():
            return None
        at.token = token
        return at

    async def revoke_token(self, token: _StoredAccess | _StoredRefresh) -> None:
        self.store.q("DELETE FROM tokens WHERE grant_id = ?", (token.grant_id,))

    def revoke_all(self) -> int:
        n = self.store.q("SELECT COUNT(*) FROM tokens")[0][0]
        self.store.q("DELETE FROM tokens")
        self.store.q("DELETE FROM codes")
        return n

    # ---- login page ----------------------------------------------------------------------
    def routes(self) -> list[Route]:
        return [Route("/login", self._login, methods=["GET", "POST"])]

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            req_id = request.query_params.get("req", "")
            return self._page(req_id)

        form = await request.form()
        req_id = str(form.get("req", ""))
        password = str(form.get("password", ""))
        ip = request.client.host if request.client else "?"

        if self.throttle.blocked(ip):
            return self._page(req_id, "Слишком много попыток. Попробуйте через 15 минут.", status=429)
        p = self.pending.get(req_id)
        if p is None or p.expires_at < time.time():
            return self._page("", "Запрос устарел. Начните подключение заново.", status=400)

        ok = await asyncio.to_thread(verify_password, password, self.s.login_password_hash)
        if not ok:
            self.throttle.fail(ip)
            await asyncio.sleep(1)
            return self._page(req_id, "Неверный пароль.", status=401)

        self.throttle.success(ip)
        return RedirectResponse(await self.complete_login(req_id), status_code=303)

    def _page(self, req_id: str, error: str = "", status: int = 200) -> Response:
        p = self.pending.get(req_id)
        if p is None and not error:
            error, status = "Запрос не найден или устарел. Начните подключение заново.", 400
        who = ""
        if p is not None:
            redirect_host = urlparse(str(p.params.redirect_uri)).netloc
            who = (
                f"<p><b>{html.escape(p.client_name)}</b> запрашивает доступ к заметкам Notability.<br>"
                f"<small>После входа вы вернётесь на <code>{html.escape(redirect_host)}</code></small></p>"
            )
        form = (
            f"""<form method="post" action="/login">
              <input type="hidden" name="req" value="{html.escape(req_id)}">
              <input type="password" name="password" placeholder="Пароль" autofocus required autocomplete="current-password">
              <button type="submit">Разрешить доступ</button>
            </form>"""
            if p is not None
            else ""
        )
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Notability MCP — вход</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;background:#f4f4f5;color:#18181b;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box}}
 main{{background:#fff;padding:28px;border-radius:14px;max-width:380px;width:100%;box-shadow:0 2px 12px #0001}}
 h1{{font-size:20px;margin:0 0 12px}} input,button{{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border-radius:8px;margin-top:10px}}
 input{{border:1px solid #d4d4d8}} button{{border:0;background:#18181b;color:#fff;cursor:pointer}}
 .err{{color:#b91c1c}} code{{font-size:13px}}
 @media (prefers-color-scheme:dark){{body{{background:#18181b;color:#f4f4f5}} main{{background:#27272a}} input{{background:#18181b;color:#f4f4f5;border-color:#3f3f46}} button{{background:#f4f4f5;color:#18181b}}}}
</style></head><body><main><h1>Notability MCP</h1>{who}{err}{form}</main></body></html>"""
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                # no form-action: browsers apply it to the post-login redirect to the client's callback
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )
