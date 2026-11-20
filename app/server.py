"""Notability MCP server: MCP (+OAuth) at /mcp, WebDAV at /dav, one port."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config
from .auth import OwnerOAuthProvider
from .dav import make_dav_app
from .library import Library, NoteError, fmt_size, fmt_time
from .security import check_hash_format
from .transcribe import Transcriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notability-mcp")

S = config.load()
check_hash_format(S.login_password_hash, "LOGIN_PASSWORD_HASH")
check_hash_format(S.dav_password_hash, "DAV_PASSWORD_HASH")
S.notes_root.mkdir(parents=True, exist_ok=True)

lib = Library(S.notes_root, S.data_dir)
tx = Transcriber(S, lib)
oauth = OwnerOAuthProvider(S)

INSTRUCTIONS = """\
Read-only access to the user's Notability notes (auto-backup). Folders mirror Notability
subjects/dividers, e.g. "Школа/10 класс/Физика". Each note may have:
- a PDF (the rendered note). Notes are mostly HANDWRITTEN: the PDF text layer is often empty,
  so use view_note_pages to look at the pages as images and read the handwriting.
- an archive (.note/.zip) that can contain audio recordings of the lesson -> get_transcript.
Typical flow: list_notes / recent_notes / search_notes -> read_note -> view_note_pages / get_transcript.
Note paths are relative to the Notability root and may omit the extension.
"""

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

mcp = MCPServer(
    name="notability",
    instructions=INSTRUCTIONS,
    auth_server_provider=oauth,
    auth=AuthSettings(
        issuer_url=S.public_url,
        resource_server_url=S.mcp_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


def _note_flags(n) -> str:
    flags = []
    if n.pdf:
        flags.append("pdf")
    if n.archive:
        rec = lib.audio_members(n.archive)
        if rec:
            pending = len(tx.pending_recordings(n.archive)) if tx.enabled else len(rec)
            flags.append(f"audio×{len(rec)}" + ("" if pending == 0 else f" (не расшифровано: {pending})"))
        else:
            flags.append("archive")
    return ", ".join(flags)


# ---- tools ----------------------------------------------------------------------------------
@mcp.tool(annotations=RO)
def list_notes(path: str = "", recursive: bool = False) -> str:
    """List folders and notes under a folder of the Notability backup.

    Args:
        path: folder relative to the Notability root ("" = root), e.g. "Школа/10 класс/Физика".
        recursive: include all nested folders.
    """
    try:
        folder = lib.resolve(path)
        if not folder.is_dir():
            return f"Not a folder: {path}"
        lines: list[str] = []
        if recursive:
            by_folder: dict[str, list] = {}
            for n in lib.all_notes(folder):
                by_folder.setdefault(lib.rel(n.folder) or "/", []).append(n)
            for f in sorted(by_folder):
                lines.append(f"\n## {f}")
                for n in by_folder[f]:
                    lines.append(f"- {n.stem}  [{_note_flags(n)}]  {fmt_time(n.mtime)}")
        else:
            subdirs = sorted(d.name for d in folder.iterdir() if d.is_dir() and not d.name.startswith("."))
            for d in subdirs:
                lines.append(f"📁 {d}/")
            for n in lib.notes_in(folder):
                lines.append(f"📝 {n.stem}  [{_note_flags(n)}]  {fmt_time(n.mtime)}")
        title = f"/{lib.rel(folder)}" if folder != S.notes_root else "/"
        return f"# {title}\n" + ("\n".join(lines) if lines else "(empty)")
    except NoteError as e:
        return f"Error: {e}"


@mcp.tool(annotations=RO)
def recent_notes(limit: int = 20, path: str = "") -> str:
    """Most recently modified notes (newest first), optionally under a folder.

    Args:
        limit: how many notes to return (max 100).
        path: optional folder to restrict to, e.g. "Школа/10 класс/Физика".
    """
    try:
        notes = sorted(lib.all_notes(lib.resolve(path)), key=lambda n: n.mtime, reverse=True)[: min(limit, 100)]
    except NoteError as e:
        return f"Error: {e}"
    return "\n".join(f"- {lib.note_id(n)}  [{_note_flags(n)}]  {fmt_time(n.mtime)}" for n in notes) or "(no notes)"


@mcp.tool(annotations=RO)
async def search_notes(query: str, path: str = "", limit: int = 20) -> str:
    """Full-text search over note names, PDF text, Notability handwriting index and audio transcripts.
    Case-insensitive; all words must match (substring match, so word stems work: "механик").

    Args:
        query: words to look for.
        path: optional folder to restrict the search to.
        limit: max results.
    """
    words = [w.casefold() for w in query.split() if w.strip()]
    if not words:
        return "Empty query"
    try:
        base = lib.resolve(path)
    except NoteError as e:
        return f"Error: {e}"

    def run() -> list[str]:
        hits = []
        for n in lib.all_notes(base):
            sources: list[tuple[str, str]] = [("name", lib.note_id(n))]
            if n.pdf:
                for i, t in enumerate(lib.pdf_pages_text(n.pdf), 1):
                    if t:
                        sources.append((f"pdf p.{i}", t))
            if n.archive:
                hw = lib.handwriting_text(n.archive)
                if hw:
                    sources.append(("handwriting", hw))
                if tx.enabled:
                    tr = tx.plain_text(n.archive)
                    if tr:
                        sources.append(("transcript", tr))
            blob = "\n".join(t for _, t in sources).casefold()
            if not all(w in blob for w in words):
                continue
            snippets = []
            for label, text in sources:
                low = text.casefold()
                pos = low.find(words[0])
                if pos >= 0 and label != "name":
                    a, b = max(0, pos - 120), min(len(text), pos + 200)
                    snippets.append(f"  {label}: …{' '.join(text[a:b].split())}…")
                if len(snippets) >= 3:
                    break
            hits.append((n.mtime, f"- {lib.note_id(n)}  ({fmt_time(n.mtime)})\n" + "\n".join(snippets)))
        hits.sort(reverse=True)
        return [h for _, h in hits[:limit]]

    results = await asyncio.to_thread(run)
    note = "" if tx.enabled else "\n(transcription disabled: audio not searched)"
    return ("\n".join(results) if results else "Nothing found.") + note


@mcp.tool(annotations=RO)
async def read_note(path: str) -> str:
    """Everything textual about a note: file info, PDF text layer per page, Notability's
    handwriting-recognition text (if present in the archive) and transcript status.
    Handwritten pages usually have no text layer: then use view_note_pages.

    Args:
        path: note path, e.g. "Школа/10 класс/Физика/Note 6 Nov 2026" (extension optional).
    """
    try:
        n = lib.note(path)
    except NoteError as e:
        return f"Error: {e}"

    def run() -> str:
        out = [f"# {lib.note_id(n)}"]
        for f in n.files:
            st = f.stat()
            out.append(f"- {f.name}: {fmt_size(st.st_size)}, modified {fmt_time(st.st_mtime)}")
        if n.pdf:
            pages = lib.pdf_pages_text(n.pdf)
            total_chars = sum(len(p) for p in pages)
            out.append(f"\n## PDF: {len(pages)} pages, text layer {total_chars} chars")
            if total_chars < 30 * max(1, len(pages)):
                out.append("(little or no text layer — the note is handwritten; use view_note_pages)")
            for i, t in enumerate(pages, 1):
                if t:
                    out.append(f"\n### Page {i}\n{t}")
        if n.archive:
            hw = lib.handwriting_text(n.archive)
            if hw:
                out.append(f"\n## Handwriting index (Notability OCR, unordered)\n{hw[:20000]}")
            rec = lib.audio_members(n.archive)
            if rec:
                _, done, total = tx.transcript_text(n.archive) if tx.enabled else ("", 0, len(rec))
                out.append(f"\n## Audio: {total} recording(s), transcribed {done}/{total} — use get_transcript")
        return "\n".join(out)

    return await asyncio.to_thread(run)


@mcp.tool(annotations=RO)
async def view_note_pages(path: str, pages: str = "1-3", dpi: int = 110) -> list:
    """Render pages of a note's PDF as images, so handwriting, drawings and formulas can be read.

    Args:
        path: note path (extension optional).
        pages: page selection like "1", "2-4", "1,3,5" or "last". At most 8 pages per call.
        dpi: render resolution (72-200). 110 is usually enough for handwriting.
    """
    try:
        n = lib.note(path)
    except NoteError as e:
        return [f"Error: {e}"]
    if not n.pdf:
        return ["This note has no PDF backup."]
    total = await asyncio.to_thread(lib.page_count, n.pdf)
    try:
        nums = _parse_pages(pages, total)
    except ValueError as e:
        return [f"Error: {e}"]
    if len(nums) > S.max_render_pages:
        return [f"Too many pages ({len(nums)}); request at most {S.max_render_pages} per call. Total pages: {total}."]
    dpi = max(72, min(200, dpi))
    imgs = await asyncio.to_thread(lib.render_pages, n.pdf, nums, dpi)
    result: list = [f"{lib.note_id(n)} — pages {', '.join(map(str, nums))} of {total}"]
    for num, data in zip(nums, imgs):
        result.append(f"Page {num}:")
        result.append(Image(data=data, format="jpeg"))
    return result


@mcp.tool(annotations=RO)
async def get_transcript(path: str, wait_seconds: int = 0, timestamps: bool = True) -> str:
    """Transcript of the audio recordings inside a note's archive (.note/.zip).
    If not transcribed yet, queues transcription and returns status; call again later
    (or pass wait_seconds up to 600 to wait).

    Args:
        path: note path (extension optional).
        wait_seconds: how long to wait for a running/queued transcription (0-600).
        timestamps: prefix lines with [mm:ss].
    """
    try:
        n = lib.note(path)
    except NoteError as e:
        return f"Error: {e}"
    if not n.archive:
        return "This note has no archive backup, so there is no audio. (Enable the 'Note' format in Notability auto-backup.)"
    if not lib.audio_members(n.archive):
        return "No audio recordings in this note."
    if not tx.enabled:
        return "Transcription is disabled on the server (TRANSCRIBER=none)."

    note_id = lib.note_id(n)
    if tx.pending_recordings(n.archive):
        job = tx.enqueue(note_id)
        if wait_seconds > 0:
            await asyncio.to_thread(job.finished_event.wait, min(wait_seconds, 600))
    text, done, total = await asyncio.to_thread(tx.transcript_text, n.archive, timestamps)
    if done < total:
        job = tx.jobs.get(note_id)
        status = _job_status(job) if job else "queued"
        return f"Transcribed {done}/{total} recordings. Status: {status}\n\n{text}"
    return f"# Transcript: {note_id}\n\n{text}"


@mcp.tool(annotations=RO)
def transcription_status() -> str:
    """Show the transcription queue: what is running, queued, finished or failed."""
    if not tx.enabled:
        return "Transcription disabled (TRANSCRIBER=none)."
    lines = [f"Backend: {tx.backend.name}" + (f" ({S.whisper_model})" if tx.backend.name == "whisper" else "")]
    for job in sorted(tx.jobs.values(), key=lambda j: j.started or 0, reverse=True)[:30]:
        lines.append(f"- {job.note_id}: {_job_status(job)}")
    return "\n".join(lines) if len(lines) > 1 else lines[0] + "\nNo jobs yet."


def _job_status(job) -> str:
    if job.state == "running":
        el = int(time.time() - (job.started or time.time()))
        return f"running {job.recordings_done}/{job.recordings_total}, {el}s elapsed — {job.detail}"
    if job.state == "error":
        return f"error: {job.detail}"
    if job.state == "done":
        return f"done in {int((job.finished or 0) - (job.started or 0))}s"
    return "queued"


def _parse_pages(spec: str, total: int) -> list[int]:
    spec = spec.strip().lower()
    if spec in ("", "all"):
        return list(range(1, total + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip().replace("last", str(total))
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    bad = [p for p in out if p < 1 or p > total]
    if bad:
        raise ValueError(f"page(s) {bad} out of range 1..{total}")
    return list(dict.fromkeys(out))


# ---- HTTP app -------------------------------------------------------------------------------
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


for route in oauth.routes():
    mcp._custom_starlette_routes.append(route)

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[S.public_host],
        allowed_origins=[S.public_url, "https://claude.ai", "https://claude.com"],
    ),
)
dav_app = make_dav_app(S)


@asynccontextmanager
async def lifespan():
    tx.start()
    log.info("notes root: %s | dav root: %s | transcriber: %s", S.notes_root, S.dav_root, S.transcriber)
    async with mcp.session_manager.run():
        yield
    tx.stop()


async def app(scope, receive, send):
    """Top-level ASGI dispatcher: /dav/* -> WebDAV (WSGI), everything else -> MCP/OAuth."""
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                try:
                    app.ctx = lifespan()
                    await app.ctx.__aenter__()
                except Exception as e:
                    log.exception("startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await app.ctx.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return
    path = scope.get("path", "")
    if scope["type"] == "http" and (path == "/dav" or path.startswith("/dav/")):
        await dav_app({**scope, "root_path": "/dav"}, receive, send)
    else:
        await mcp_app(scope, receive, send)
