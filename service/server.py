#!/usr/bin/env python3
"""Loopback-only shared transcription service for local Whisper large-v3."""
from __future__ import annotations

import gc
import json
import os
import queue
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOST = "127.0.0.1"  # Never expose the transcriber to the LAN.
PORT = int(os.environ.get("PI_WHISPER_PORT", "10301"))
MODEL_NAME = os.environ.get("PI_WHISPER_MODEL", "large-v3")
MODEL_PATH = Path.home() / ".cache" / "whisper" / f"{MODEL_NAME}.pt"
MAX_BODY_BYTES = 50 * 1024 * 1024
QUEUE_CAPACITY = max(1, int(os.environ.get("PI_WHISPER_QUEUE_SIZE", "2")))
IDLE_UNLOAD_SECONDS = max(1, int(os.environ.get("PI_WHISPER_IDLE_SECONDS", "600")))
MAX_INFLIGHT = QUEUE_CAPACITY + 1  # One active job plus the bounded FIFO queue.
ALLOWED_MODELS = {MODEL_NAME, "whisper-1"}  # whisper-1 is the plugin's generic local default.

_PROCESS_STARTED_MONOTONIC = time.perf_counter()
_PROCESS_STARTED_AT = datetime.now(timezone.utc)
_SERVER_READY_AT: datetime | None = None
_SERVER_STARTUP_SECONDS: float | None = None
_MODEL = None
# Reentrant because do_POST holds this lock for the entire inference and load_model also protects initialization.
_MODEL_LOCK = threading.RLock()
_STATE_LOCK = threading.Lock()
_MODEL_LOADED_AT: datetime | None = None
_MODEL_LOAD_SECONDS: float | None = None
_MODEL_UNLOADED_AT: datetime | None = None
_MODEL_LAST_USE_MONOTONIC: float | None = None
_LAST_ACTIVITY_AT: datetime | None = None
_LAST_ACTIVITY_EPOCH: float | None = None
_LAST_ACTIVITY_MONOTONIC: float | None = None
_LAST_COMPLETED_AT: datetime | None = None
_LAST_TRANSCRIPTION_SECONDS: float | None = None
_LAST_QUEUE_WAIT_SECONDS: float | None = None
_ACTIVE_REQUESTS = 0
_INFLIGHT_REQUESTS = 0
_REQUESTS_ACCEPTED = 0
_REQUESTS_COMPLETED = 0
_REQUESTS_FAILED = 0
_QUEUE_REJECTIONS = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


def mark_activity(at: datetime | None = None) -> None:
    global _LAST_ACTIVITY_AT, _LAST_ACTIVITY_EPOCH, _LAST_ACTIVITY_MONOTONIC
    current = at or utc_now()
    with _STATE_LOCK:
        _LAST_ACTIVITY_AT = current
        _LAST_ACTIVITY_EPOCH = current.timestamp()
        _LAST_ACTIVITY_MONOTONIC = time.monotonic()


def load_model():
    """Load the cached checkpoint, measure it, and never download weights."""
    global _MODEL, _MODEL_LOADED_AT, _MODEL_LOAD_SECONDS, _MODEL_LAST_USE_MONOTONIC
    with _MODEL_LOCK:
        if _MODEL is None:
            if not MODEL_PATH.is_file():
                raise RuntimeError(f"Local Whisper checkpoint not found: {MODEL_PATH}")
            started = time.perf_counter()
            try:
                # Include the first Whisper/PyTorch import in the measured load time.
                import whisper

                model = whisper.load_model(str(MODEL_PATH), device="cpu")
            finally:
                _MODEL_LOAD_SECONDS = round(time.perf_counter() - started, 3)
            _MODEL = model
            _MODEL_LOADED_AT = utc_now()
            print(f"Whisper {MODEL_NAME} loaded in {_MODEL_LOAD_SECONDS:.3f}s", flush=True)
        with _STATE_LOCK:
            _MODEL_LAST_USE_MONOTONIC = time.monotonic()
        return _MODEL


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes, str]:
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    )
    if not message.is_multipart():
        raise ValueError("Expected multipart/form-data.")

    fields: dict[str, str] = {}
    audio: bytes | None = None
    filename = ""
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "file":
            if audio is not None:
                raise ValueError("Send exactly one audio file.")
            audio = payload
            filename = part.get_filename() or ""
        elif name:
            charset = part.get_content_charset() or "utf-8"
            try:
                fields[name] = payload.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise ValueError(f"Invalid text field: {name}.") from exc

    if not audio:
        raise ValueError("Missing or empty audio file field 'file'.")
    return fields, audio, filename


@dataclass
class TranscriptionJob:
    audio_path: Path
    language: str
    enqueued_monotonic: float = field(default_factory=time.monotonic)
    accepted: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    status_code: int = 200
    text: str = ""
    error: str = ""


_JOBS: queue.Queue[TranscriptionJob] = queue.Queue(maxsize=QUEUE_CAPACITY)
_UPLOAD_ADMISSION = threading.BoundedSemaphore(MAX_INFLIGHT)


def process_job(job: TranscriptionJob) -> None:
    global _ACTIVE_REQUESTS, _INFLIGHT_REQUESTS, _REQUESTS_COMPLETED, _REQUESTS_FAILED
    global _LAST_ACTIVITY_AT, _LAST_ACTIVITY_EPOCH, _LAST_ACTIVITY_MONOTONIC
    global _LAST_COMPLETED_AT, _LAST_TRANSCRIPTION_SECONDS, _LAST_QUEUE_WAIT_SECONDS
    global _MODEL_LAST_USE_MONOTONIC

    started = time.perf_counter()
    queue_wait = started - job.enqueued_monotonic
    with _STATE_LOCK:
        _ACTIVE_REQUESTS += 1
        _LAST_QUEUE_WAIT_SECONDS = round(queue_wait, 3)

    try:
        # Keep the reentrant lock across load and inference: only one Whisper job uses the model at a time.
        with _MODEL_LOCK:
            model = load_model()
            result = model.transcribe(
                str(job.audio_path),
                language=job.language or None,
                task="transcribe",
                fp16=False,
            )
        text = result.get("text", "").strip()
        if not text:
            job.status_code = 422
            job.error = "Whisper returned an empty transcription."
            with _STATE_LOCK:
                _REQUESTS_FAILED += 1
        else:
            job.text = text
            with _STATE_LOCK:
                _REQUESTS_COMPLETED += 1
    except Exception as exc:  # Keep internals out of the HTTP response.
        print(f"Transcription failed: {type(exc).__name__}: {exc}", flush=True)
        job.status_code = 500
        job.error = "Local transcription failed; see the server log for details."
        with _STATE_LOCK:
            _REQUESTS_FAILED += 1
    finally:
        completed_at = utc_now()
        elapsed = time.perf_counter() - started
        with _STATE_LOCK:
            _ACTIVE_REQUESTS -= 1
            _INFLIGHT_REQUESTS -= 1
            _LAST_ACTIVITY_AT = completed_at
            _LAST_ACTIVITY_EPOCH = completed_at.timestamp()
            _LAST_ACTIVITY_MONOTONIC = time.monotonic()
            _LAST_COMPLETED_AT = completed_at
            _LAST_TRANSCRIPTION_SECONDS = round(elapsed, 3)
            _MODEL_LAST_USE_MONOTONIC = time.monotonic()
        print(f"Transcription finished in {elapsed:.3f}s (queue wait {queue_wait:.3f}s, HTTP {job.status_code})", flush=True)
        job.completed.set()


def worker_loop() -> None:
    while True:
        job = _JOBS.get()
        job.accepted.wait()
        try:
            process_job(job)
        finally:
            _JOBS.task_done()


def unload_model_if_idle() -> None:
    global _MODEL, _MODEL_UNLOADED_AT, _MODEL_LAST_USE_MONOTONIC
    with _MODEL_LOCK:
        if _MODEL is None:
            return
        with _STATE_LOCK:
            active = _ACTIVE_REQUESTS
            inflight = _INFLIGHT_REQUESTS
            queued = _JOBS.qsize()
            last_use = _MODEL_LAST_USE_MONOTONIC
        if active or inflight or queued or last_use is None:
            return
        idle_for = time.monotonic() - last_use
        if idle_for < IDLE_UNLOAD_SECONDS:
            return

        started = time.perf_counter()
        _MODEL = None
        gc.collect()
        elapsed = time.perf_counter() - started
        unloaded_at = utc_now()
        with _STATE_LOCK:
            _MODEL_UNLOADED_AT = unloaded_at
            _MODEL_LAST_USE_MONOTONIC = None
        print(f"Whisper {MODEL_NAME} unloaded after {idle_for:.1f}s idle (cleanup {elapsed:.3f}s)", flush=True)


def idle_unload_loop() -> None:
    interval = min(30, max(1, IDLE_UNLOAD_SECONDS // 10))
    while True:
        time.sleep(interval)
        unload_model_if_idle()


class TranscriptionHandler(BaseHTTPRequestHandler):
    server_version = "LocalWhisperSTT/1.2"

    def log_message(self, fmt: str, *args: object) -> None:
        # Log request metadata only; never log headers, form fields, or audio.
        super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/health":
            self._json(404, {"error": "Not found."})
            return
        now = time.monotonic()
        with _STATE_LOCK:
            active = _ACTIVE_REQUESTS
            inflight = _INFLIGHT_REQUESTS
            queued = _JOBS.qsize()
            last_activity = _LAST_ACTIVITY_AT
            last_activity_epoch = _LAST_ACTIVITY_EPOCH
            last_activity_mono = _LAST_ACTIVITY_MONOTONIC
            idle_seconds = round(max(0.0, now - last_activity_mono), 3) if last_activity_mono is not None else None
            payload: dict[str, object] = {
                "status": "ok",
                "model": MODEL_NAME,
                "loaded": _MODEL is not None,
                "process_started_at": iso_timestamp(_PROCESS_STARTED_AT),
                "server_ready_at": iso_timestamp(_SERVER_READY_AT),
                "server_startup_seconds": _SERVER_STARTUP_SECONDS,
                "model_loaded_at": iso_timestamp(_MODEL_LOADED_AT),
                "model_load_seconds": _MODEL_LOAD_SECONDS,
                "model_unloaded_at": iso_timestamp(_MODEL_UNLOADED_AT),
                "last_activity_at": iso_timestamp(last_activity),
                "last_activity_epoch": last_activity_epoch,
                "idle_seconds": idle_seconds,
                "active_requests": active,
                "inflight_requests": inflight,
                "queued_requests": queued,
                "queue_capacity": QUEUE_CAPACITY,
                "max_inflight": MAX_INFLIGHT,
                "idle_unload_seconds": IDLE_UNLOAD_SECONDS,
                "requests_accepted": _REQUESTS_ACCEPTED,
                "requests_completed": _REQUESTS_COMPLETED,
                "requests_failed": _REQUESTS_FAILED,
                "queue_rejections": _QUEUE_REJECTIONS,
                "last_queue_wait_seconds": _LAST_QUEUE_WAIT_SECONDS,
                "last_transcription_seconds": _LAST_TRANSCRIPTION_SECONDS,
            }
        self._json(200, payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        global _QUEUE_REJECTIONS, _ACTIVE_REQUESTS
        request_path = urlsplit(self.path).path
        if request_path == "/warmup":
            started = utc_now()
            with _STATE_LOCK:
                _ACTIVE_REQUESTS += 1
            mark_activity(started)
            try:
                with _MODEL_LOCK:
                    load_model()
                self._json(200, {
                    "status": "ready",
                    "model": MODEL_NAME,
                    "model_loaded_at": iso_timestamp(_MODEL_LOADED_AT),
                    "model_load_seconds": _MODEL_LOAD_SECONDS,
                })
            except Exception as exc:
                self.log_error("Model warmup failed: %s", exc)
                self._json(500, {"error": "Local Whisper model failed to load; see the server log."})
            finally:
                with _STATE_LOCK:
                    _ACTIVE_REQUESTS -= 1
                mark_activity()
            return

        if request_path != "/v1/audio/transcriptions":
            self._json(404, {"error": "Not found."})
            return

        if not _UPLOAD_ADMISSION.acquire(blocking=False):
            with _STATE_LOCK:
                _QUEUE_REJECTIONS += 1
            self.close_connection = True
            self._json(429, {"error": "Local Whisper is busy; retry shortly."}, {"Retry-After": "2"})
            return
        try:
            self.handle_transcription()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            print(f"Request failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                self._json(500, {"error": "Local transcription request failed; see the server log."})
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            _UPLOAD_ADMISSION.release()

    def handle_transcription(self) -> None:
        global _REQUESTS_ACCEPTED, _QUEUE_REJECTIONS, _INFLIGHT_REQUESTS
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            self._json(411, {"error": "Content-Length is required."})
            return
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": f"Request exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MiB limit."})
            return
        if length == 0:
            self._json(400, {"error": "Empty request body."})
            return

        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data;"):
                raise ValueError("Expected multipart/form-data.")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body.")
            fields, audio, filename = parse_multipart(content_type, body)
            requested_model = fields.get("model", "").strip()
            if requested_model and requested_model not in ALLOWED_MODELS:
                raise ValueError(f"This service is configured for '{MODEL_NAME}', not '{requested_model}'.")
            response_format = fields.get("response_format", "json").strip().lower()
            if response_format != "json":
                raise ValueError("Only response_format=json is supported.")

            language = fields.get("language", "").strip()
            if language.lower() == "auto":
                language = ""
            suffix = Path(filename).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
                suffix = ".audio"

            # Keep queued audio on disk, not in RAM. The temporary directory stays
            # alive until the worker completes and is then removed automatically.
            with tempfile.TemporaryDirectory(prefix="pi-whisper-stt-") as temp_dir:
                audio_path = Path(temp_dir) / f"input{suffix}"
                audio_path.write_bytes(audio)
                del body, audio
                job = TranscriptionJob(audio_path=audio_path, language=language)
                with _STATE_LOCK:
                    _INFLIGHT_REQUESTS += 1
                try:
                    _JOBS.put_nowait(job)
                except queue.Full:
                    with _STATE_LOCK:
                        _INFLIGHT_REQUESTS -= 1
                        _QUEUE_REJECTIONS += 1
                    self._json(429, {
                        "error": "Local Whisper queue is full; retry shortly.",
                        "queue_capacity": QUEUE_CAPACITY,
                    }, {"Retry-After": "2"})
                    return

                with _STATE_LOCK:
                    _REQUESTS_ACCEPTED += 1
                mark_activity()
                job.accepted.set()
                job.completed.wait()
                if job.status_code != 200:
                    self._json(job.status_code, {"error": job.error})
                    return
                self._json(200, {"text": job.text})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            print(f"Transcription request failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                self._json(500, {"error": "Local transcription request failed; see the server log."})
            except (BrokenPipeError, ConnectionResetError):
                return


def create_server(port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), TranscriptionHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    global _SERVER_READY_AT, _SERVER_STARTUP_SECONDS
    if not 1 <= PORT <= 65535:
        raise SystemExit("PI_WHISPER_PORT must be between 1 and 65535.")
    if MODEL_NAME != "large-v3":
        raise SystemExit("This service is deliberately pinned to large-v3.")

    threading.Thread(target=worker_loop, name="whisper-stt-worker", daemon=True).start()
    threading.Thread(target=idle_unload_loop, name="whisper-stt-idle-unload", daemon=True).start()
    server = create_server()
    _SERVER_READY_AT = utc_now()
    _SERVER_STARTUP_SECONDS = round(time.perf_counter() - _PROCESS_STARTED_MONOTONIC, 3)
    print(f"Local Whisper STT listening at http://{HOST}:{server.server_port}", flush=True)
    print(f"HTTP server ready in {_SERVER_STARTUP_SECONDS:.3f}s", flush=True)
    print(f"Model: {MODEL_NAME} (loads on demand, unloads after {IDLE_UNLOAD_SECONDS}s idle; CPU only)", flush=True)
    print(f"FIFO queue: 1 active + up to {QUEUE_CAPACITY} waiting request(s)", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping local Whisper STT…", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
