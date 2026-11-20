"""Audio transcription for recordings embedded in Notability archives.

Notability re-uploads the whole archive whenever a note changes (even one pen
stroke), so transcripts are cached per *recording*: key = CRC32 + size of the
audio member inside the zip. Unchanged audio is never transcribed twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings
from .library import Library

log = logging.getLogger("transcribe")


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass
class Job:
    note_id: str
    state: str = "queued"  # queued | running | done | error
    detail: str = ""
    started: float | None = None
    finished: float | None = None
    recordings_total: int = 0
    recordings_done: int = 0
    finished_event: threading.Event = field(default_factory=threading.Event)


def recording_key(info: zipfile.ZipInfo) -> str:
    return hashlib.sha256(f"{info.CRC:08x}:{info.file_size}".encode()).hexdigest()[:32]


# ---- backends --------------------------------------------------------------------------------
class WhisperBackend:
    name = "whisper"

    def __init__(self, s: Settings):
        self.s = s
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper model %s (%s)", self.s.whisper_model, self.s.whisper_compute_type)
            self._model = WhisperModel(
                self.s.whisper_model,
                device="cpu",
                compute_type=self.s.whisper_compute_type,
                cpu_threads=self.s.whisper_threads,
                download_root=str(self.s.data_dir / "models"),
            )
        return self._model

    def transcribe(self, audio: Path) -> tuple[list[Segment], str | None]:
        model = self._load()
        segments, info = model.transcribe(
            str(audio),
            language=self.s.transcribe_language,
            vad_filter=True,  # lessons have long silences; VAD avoids hallucinated text there
            beam_size=5,
            condition_on_previous_text=False,  # reduces repetition loops on long recordings
        )
        out = [Segment(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
        return out, info.language


class AssemblyAIBackend:
    name = "assemblyai"
    def __init__(self, s: Settings):
        self.s = s
        # https://api.eu.assemblyai.com to keep data in the EU
        self.base = os.environ.get("ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com").rstrip("/")
        # empty = account default model; e.g. "assemblyai-stt-3-7"
        self.speech_models = [m.strip() for m in os.environ.get("ASSEMBLYAI_SPEECH_MODELS", "").split(",") if m.strip()]

    def transcribe(self, audio: Path) -> tuple[list[Segment], str | None]:
        headers = {"authorization": self.s.assemblyai_api_key or ""}
        with httpx.Client(base_url=self.base, headers=headers, timeout=httpx.Timeout(60, read=600)) as c:
            with audio.open("rb") as f:
                r = c.post("/v2/upload", content=f)
            r.raise_for_status()
            # speaker_labels requires punctuate=true
            body: dict = {"audio_url": r.json()["upload_url"], "speaker_labels": True, "punctuate": True}
            if self.speech_models:
                body["speech_models"] = self.speech_models
            if self.s.transcribe_language:
                body["language_code"] = self.s.transcribe_language
            else:
                body["language_detection"] = True
            r = c.post("/v2/transcript", json=body)
            r.raise_for_status()
            tid = r.json()["id"]
            while True:
                time.sleep(5)
                r = c.get(f"/v2/transcript/{tid}")
                r.raise_for_status()
                data = r.json()
                if data["status"] == "completed":
                    break
                if data["status"] == "error":
                    raise RuntimeError(f"AssemblyAI: {data.get('error')}")
            # don't leave the lesson audio on their side
            try:
                c.delete(f"/v2/transcript/{tid}")
            except httpx.HTTPError:
                pass
        utts = data.get("utterances") or []
        if utts:
            segs = [Segment(u["start"] / 1000, u["end"] / 1000, u["text"], u.get("speaker")) for u in utts]
        else:
            segs = [Segment(0, (data.get("audio_duration") or 0), data.get("text") or "")]
        return segs, data.get("language_code")


# ---- service ---------------------------------------------------------------------------------
class Transcriber:
    def __init__(self, s: Settings, lib: Library):
        self.s = s
        self.lib = lib
        self.dir = s.data_dir / "transcripts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tmp = s.data_dir / "tmp"
        self.backend = (
            WhisperBackend(s) if s.transcriber == "whisper" else AssemblyAIBackend(s) if s.transcriber == "assemblyai" else None
        )
        self.jobs: dict[str, Job] = {}
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return self.backend is not None

    # cache
    def _cache_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def cached(self, info: zipfile.ZipInfo) -> dict | None:
        p = self._cache_path(recording_key(info))
        return json.loads(p.read_text()) if p.exists() else None

    def pending_recordings(self, archive: Path) -> list[zipfile.ZipInfo]:
        return [i for i in self.lib.audio_members(archive) if self.cached(i) is None]

    def transcript_text(self, archive: Path, with_timestamps: bool = True) -> tuple[str, int, int]:
        """Returns (text, done_count, total_count)."""
        members = self.lib.audio_members(archive)
        parts, done = [], 0
        for n, info in enumerate(members, 1):
            c = self.cached(info)
            header = f"### Запись {n}" + (f" ({_hms(c['duration'])})" if c and c.get("duration") else "")
            if c is None:
                parts.append(f"{header}\n_(ещё не расшифрована)_")
                continue
            done += 1
            lines = []
            for seg in c["segments"]:
                prefix = f"[{_hms(seg['start'])}] " if with_timestamps else ""
                spk = f"Спикер {seg['speaker']}: " if seg.get("speaker") else ""
                lines.append(f"{prefix}{spk}{seg['text']}")
            parts.append(header + "\n" + "\n".join(lines))
        return "\n\n".join(parts), done, len(members)

    def plain_text(self, archive: Path) -> str:
        out = []
        for info in self.lib.audio_members(archive):
            c = self.cached(info)
            if c:
                out.append(" ".join(seg["text"] for seg in c["segments"]))
        return "\n".join(out)

    # queue
    def enqueue(self, note_id: str) -> Job:
        with self._lock:
            job = self.jobs.get(note_id)
            if job and job.state in ("queued", "running"):
                return job
            job = Job(note_id)
            self.jobs[note_id] = job
        self._q.put(note_id)
        return job

    def start(self) -> None:
        if not self.enabled:
            return
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, name="transcribe-worker", daemon=True).start()
        if self.s.auto_transcribe:
            threading.Thread(target=self._scanner, name="transcribe-scanner", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _scanner(self) -> None:
        time.sleep(10)
        while not self._stop.is_set():
            try:
                for note in self.lib.all_notes():
                    if not note.archive:
                        continue
                    # Notability may still be uploading; wait until the file is quiet for a minute
                    if time.time() - note.archive.stat().st_mtime < 60:
                        continue
                    note_id = self.lib.note_id(note)
                    last = self.jobs.get(note_id)
                    # don't hammer a failing recording: auto-retry errors at most once an hour
                    if last and last.state == "error" and time.time() - (last.finished or 0) < 3600:
                        continue
                    if self.pending_recordings(note.archive):
                        self.enqueue(note_id)
            except Exception:
                log.exception("scan failed")
            self._stop.wait(self.s.scan_interval)

    def _worker(self) -> None:
        while not self._stop.is_set():
            note_id = self._q.get()
            job = self.jobs[note_id]
            job.state, job.started = "running", time.time()
            try:
                self._run(job)
                job.state = "done"
            except Exception as e:
                log.exception("transcription failed for %s", note_id)
                job.state, job.detail = "error", f"{type(e).__name__}: {e}"
            finally:
                job.finished = time.time()
                job.finished_event.set()

    def _run(self, job: Job) -> None:
        assert self.backend is not None
        note = self.lib.note(job.note_id)
        if not note.archive:
            raise RuntimeError("note has no archive with audio")
        todo = self.pending_recordings(note.archive)
        job.recordings_total = len(todo)
        for info in todo:
            job.detail = f"{info.filename} ({info.file_size / 1024**2:.1f} MB)"
            with tempfile.TemporaryDirectory(dir=self.tmp) as td:
                audio = Path(td) / ("audio" + Path(info.filename).suffix.lower())
                with zipfile.ZipFile(note.archive) as z, z.open(info) as src, audio.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                t0 = time.time()
                segs, lang = self.backend.transcribe(audio)
            duration = segs[-1].end if segs else 0
            log.info("%s: %s transcribed in %.0fs (audio %.0fs)", job.note_id, info.filename, time.time() - t0, duration)
            payload = {
                "backend": self.backend.name,
                "language": lang,
                "duration": duration,
                "member": info.filename,
                "created": time.time(),
                "segments": [s.__dict__ for s in segs],
            }
            tmp = self._cache_path(recording_key(info)).with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            tmp.replace(self._cache_path(recording_key(info)))
            job.recordings_done += 1
        job.detail = ""


def _hms(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
