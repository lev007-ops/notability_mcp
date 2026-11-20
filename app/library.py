"""Read-only access to the Notability backup tree.

A "note" is a stem inside a folder with any of:
- `<stem>.pdf`  - auto-backup in "PDF" format (notes without audio);
- `<stem>.zip`  - auto-backup in "PDF + audio" format: `<stem>/<stem>.pdf` and
  `<stem>/Recordings/Recording N.m4a` inside;
- `<stem>.note` - native Notability archive (audio is found the same way).
When both a standalone PDF and an archive with a PDF exist, the newer one wins:
switching backup formats leaves stale files behind.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pymupdf

PDF_EXT = ".pdf"
ARCHIVE_EXTS = (".zip", ".note")
AUDIO_EXTS = (".m4a", ".aac", ".caf", ".wav", ".mp3", ".aiff", ".aif", ".mp4", ".ogg", ".opus", ".flac")
MAX_MEMBER_BYTES = 4 * 1024**3
MAX_PDF_IN_ARCHIVE = 500 * 1024**2


class NoteError(ValueError):
    pass


@dataclass
class Note:
    folder: Path
    stem: str
    pdf: Path | None = None
    archive: Path | None = None
    # resolved by Library.note()/notes_in(): where the PDF to show actually lives
    pdf_container: Path | None = None
    pdf_member: str | None = None  # member name inside the archive, None = standalone file

    @property
    def has_pdf(self) -> bool:
        return self.pdf_container is not None

    @property
    def pdf_label(self) -> str:
        if not self.pdf_container:
            return ""
        return f"{self.pdf_container.name} → {self.pdf_member}" if self.pdf_member else self.pdf_container.name

    @property
    def files(self) -> list[Path]:
        return [p for p in (self.pdf, self.archive) if p]

    @property
    def mtime(self) -> float:
        return max(p.stat().st_mtime for p in self.files)


def _visible(p: Path) -> bool:
    return not p.name.startswith(".")


class Library:
    def __init__(self, root: Path, data_dir: Path):
        self.root = root
        self._text = TextCache(data_dir / "text-cache.sqlite3")

    # ---- paths -----------------------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        rel = (rel or "").strip().strip("/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise NoteError("path is outside the notes folder")
        if any(part.startswith(".") for part in p.relative_to(self.root).parts):
            raise NoteError("hidden paths are not accessible")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    # ---- notes -----------------------------------------------------------------------------
    def notes_in(self, folder: Path) -> list[Note]:
        notes: dict[str, Note] = {}
        for f in folder.iterdir():
            if not f.is_file() or not _visible(f):
                continue
            ext = f.suffix.lower()
            if ext == PDF_EXT:
                notes.setdefault(f.stem, Note(folder, f.stem)).pdf = f
            elif ext in ARCHIVE_EXTS:
                notes.setdefault(f.stem, Note(folder, f.stem)).archive = f
        for n in notes.values():
            self._resolve_pdf(n)
        return sorted(notes.values(), key=lambda n: n.mtime, reverse=True)

    def all_notes(self, under: Path | None = None) -> list[Note]:
        base = under or self.root
        out: list[Note] = []
        for folder in [base, *sorted(d for d in base.rglob("*") if d.is_dir())]:
            if folder != base and any(part.startswith(".") for part in folder.relative_to(base).parts):
                continue
            out.extend(self.notes_in(folder))
        return out

    def note(self, rel: str) -> Note:
        """Accepts 'Folder/Note name', with or without extension."""
        p = self.resolve(rel)
        folder, stem = p.parent, p.name
        if p.suffix.lower() in (PDF_EXT, *ARCHIVE_EXTS):
            stem = p.stem
        if not folder.is_dir():
            raise NoteError(f"folder not found: {self.rel(folder) if folder != self.root else '/'}")
        notes = self.notes_in(folder)
        for n in notes:
            if n.stem == stem:
                return n
        # file names like "Физика 10 механика .pdf" have a trailing space that callers drop
        for n in notes:
            if n.stem.strip() == stem.strip():
                return n
        raise NoteError(f"note not found: {rel}")

    def note_id(self, n: Note) -> str:
        return self.rel(n.folder / n.stem)

    # ---- PDF -------------------------------------------------------------------------------
    def _resolve_pdf(self, n: Note) -> None:
        member = self._archive_pdf_member(n.archive) if n.archive else None
        use_archive = member is not None and (
            n.pdf is None or n.archive.stat().st_mtime >= n.pdf.stat().st_mtime  # type: ignore[union-attr]
        )
        if use_archive:
            n.pdf_container, n.pdf_member = n.archive, member
        elif n.pdf:
            n.pdf_container, n.pdf_member = n.pdf, None

    def _archive_pdf_member(self, archive: Path) -> str | None:
        try:
            with zipfile.ZipFile(archive) as z:
                pdfs = [
                    i
                    for i in z.infolist()
                    if not i.is_dir()
                    and i.filename.lower().endswith(PDF_EXT)
                    and "__MACOSX" not in i.filename
                    and 0 < i.file_size <= MAX_PDF_IN_ARCHIVE
                ]
        except (zipfile.BadZipFile, OSError):
            return None
        if not pdfs:
            return None
        # prefer "<stem>/<stem>.pdf", else the largest
        stem = archive.stem
        for i in pdfs:
            if Path(i.filename).stem == stem:
                return i.filename
        return max(pdfs, key=lambda i: i.file_size).filename

    def _open_pdf(self, n: Note) -> pymupdf.Document:
        if not n.pdf_container:
            raise NoteError("this note has no PDF")
        if n.pdf_member is None:
            return pymupdf.open(n.pdf_container)
        with zipfile.ZipFile(n.pdf_container) as z:
            return pymupdf.open(stream=z.read(n.pdf_member), filetype="pdf")

    def pdf_pages_text(self, n: Note) -> list[str]:
        if not n.pdf_container:
            return []

        def extract() -> list[str]:
            with self._open_pdf(n) as doc:
                return [page.get_text().strip() for page in doc]

        return self._text.get(n.pdf_container, f"pdf:{n.pdf_member or ''}", extract)

    def page_count(self, n: Note) -> int:
        with self._open_pdf(n) as doc:
            return doc.page_count

    def render_pages(self, n: Note, pages: list[int], dpi: int) -> list[bytes]:
        out = []
        with self._open_pdf(n) as doc:
            for i in pages:
                page = doc[i - 1]
                # cap the long side at ~2000 px: enough to read handwriting, cheap in tokens
                zoom = dpi / 72
                long_side = max(page.rect.width, page.rect.height) * zoom
                if long_side > 2000:
                    zoom *= 2000 / long_side
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                out.append(pix.tobytes("jpeg", jpg_quality=80))
        return out

    # ---- archives --------------------------------------------------------------------------
    def audio_members(self, archive: Path) -> list[zipfile.ZipInfo]:
        try:
            with zipfile.ZipFile(archive) as z:
                members = [
                    i
                    for i in z.infolist()
                    if not i.is_dir()
                    and i.filename.lower().endswith(AUDIO_EXTS)
                    and "__MACOSX" not in i.filename
                    and 0 < i.file_size <= MAX_MEMBER_BYTES
                ]
        except zipfile.BadZipFile:
            return []
        return sorted(members, key=lambda i: i.filename)

    def archive_listing(self, archive: Path) -> list[tuple[str, int]]:
        with zipfile.ZipFile(archive) as z:
            return [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]


class TextCache:
    """Extracted text keyed by (path, kind), invalidated by mtime+size."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS t (path TEXT, kind TEXT, mtime REAL, size INT, data TEXT, PRIMARY KEY(path, kind))"
        )
        self._lock = threading.Lock()

    def get(self, path: Path, kind: str, compute) -> list[str]:
        st = path.stat()
        key = str(path)
        with self._lock:
            row = self._db.execute(
                "SELECT mtime, size, data FROM t WHERE path = ? AND kind = ?", (key, kind)
            ).fetchone()
        if row and row[0] == st.st_mtime and row[1] == st.st_size:
            return json.loads(row[2])
        try:
            value = compute()
        except Exception:  # corrupt / half-uploaded file: don't cache, retry next time
            return []
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO t VALUES (?, ?, ?, ?, ?)",
                (key, kind, st.st_mtime, st.st_size, json.dumps(value, ensure_ascii=False)),
            )
        return value


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
