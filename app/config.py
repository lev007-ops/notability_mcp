"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    public_url: str
    dav_root: Path
    notes_root: Path
    data_dir: Path

    login_password_hash: str
    dav_user: str
    dav_password_hash: str
    allowed_redirect_hosts: list[str]
    access_token_ttl: int
    refresh_token_ttl: int

    transcriber: str
    transcribe_language: str | None
    auto_transcribe: bool
    scan_interval: int
    whisper_model: str
    whisper_compute_type: str
    whisper_threads: int
    assemblyai_api_key: str | None

    max_render_pages: int = 8

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc

    @property
    def mcp_url(self) -> str:
        return self.public_url + "/mcp"


def load() -> Settings:
    public_url = _env("PUBLIC_URL").rstrip("/")
    if not public_url.startswith("https://") and not _bool("ALLOW_INSECURE_HTTP", False):
        raise RuntimeError("PUBLIC_URL must be https:// (set ALLOW_INSECURE_HTTP=true for local testing only)")

    dav_root = Path(os.environ.get("DAV_ROOT", "/backups")).resolve()
    notes_root = Path(os.environ.get("NOTES_ROOT", str(dav_root / "Notability"))).resolve()
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).resolve()

    transcriber = os.environ.get("TRANSCRIBER", "whisper").strip().lower()
    if transcriber not in {"whisper", "assemblyai", "none"}:
        raise RuntimeError("TRANSCRIBER must be one of: whisper, assemblyai, none")
    api_key = os.environ.get("ASSEMBLYAI_API_KEY") or None
    if transcriber == "assemblyai" and not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is required when TRANSCRIBER=assemblyai")

    lang = os.environ.get("TRANSCRIBE_LANGUAGE", "ru").strip().lower()

    return Settings(
        public_url=public_url,
        dav_root=dav_root,
        notes_root=notes_root,
        data_dir=data_dir,
        login_password_hash=_env("LOGIN_PASSWORD_HASH"),
        dav_user=_env("DAV_USER"),
        dav_password_hash=_env("DAV_PASSWORD_HASH"),
        allowed_redirect_hosts=_list(
            "ALLOWED_REDIRECT_HOSTS", "claude.ai,claude.com,localhost,127.0.0.1"
        ),
        access_token_ttl=int(os.environ.get("ACCESS_TOKEN_TTL", "3600")),
        refresh_token_ttl=int(os.environ.get("REFRESH_TOKEN_TTL", str(60 * 60 * 24 * 60))),
        transcriber=transcriber,
        transcribe_language=None if lang in {"", "auto"} else lang,
        auto_transcribe=_bool("AUTO_TRANSCRIBE", True),
        scan_interval=int(os.environ.get("SCAN_INTERVAL", "300")),
        whisper_model=os.environ.get("WHISPER_MODEL", "large-v3-turbo"),
        whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
        whisper_threads=int(os.environ.get("WHISPER_THREADS", "0")),
        assemblyai_api_key=api_key,
    )
