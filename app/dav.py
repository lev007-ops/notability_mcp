"""WebDAV endpoint for Notability's auto-backup (Basic auth, TLS terminated by Caddy)."""

from __future__ import annotations

import logging
import time

from a2wsgi import WSGIMiddleware
from wsgidav.dc.base_dc import BaseDomainController
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from .config import Settings
from .security import Throttle, VerifiedCache, verify_password

log = logging.getLogger("dav")


class HashedPasswordDC(BaseDomainController):
    def __init__(self, wsgidav_app, config):
        super().__init__(wsgidav_app, config)
        opts = config["notability_dc"]
        self.user: str = opts["user"]
        self.password_hash: str = opts["password_hash"]
        self.throttle: Throttle = opts["throttle"]
        self.cache = VerifiedCache(ttl=600)

    def __str__(self) -> str:
        return "HashedPasswordDC()"

    def get_domain_realm(self, path_info, environ):
        return "Notability"

    def require_authentication(self, realm, environ):
        return True

    def supports_http_digest_auth(self):
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        ip = (environ or {}).get("REMOTE_ADDR", "?")
        if self.throttle.blocked(ip):
            log.warning("dav: blocked %s", ip)
            return False
        if user_name == self.user and self.cache.check(user_name, password):
            return True
        if user_name == self.user and verify_password(password, self.password_hash):
            self.cache.add(user_name, password)
            self.throttle.success(ip)
            return True
        self.throttle.fail(ip)
        time.sleep(1)
        log.warning("dav: failed login for %r from %s", user_name, ip)
        return False


def make_dav_app(s: Settings):
    s.dav_root.mkdir(parents=True, exist_ok=True)
    config = {
        "provider_mapping": {"/": FilesystemProvider(str(s.dav_root), readonly=False, fs_opts={})},
        "mount_path": "/dav",
        "http_authenticator": {
            "domain_controller": HashedPasswordDC,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
            "trusted_auth_header": None,
        },
        "notability_dc": {"user": s.dav_user, "password_hash": s.dav_password_hash, "throttle": Throttle()},
        "dir_browser": {"enable": False},
        "lock_storage": True,
        "property_manager": True,
        "verbose": 1,
        "logging": {"enable": False, "enable_loggers": []},
        "hotfixes": {"re_encode_path_info": True, "unquote_path_info": False},
    }
    return WSGIMiddleware(WsgiDAVApp(config))
