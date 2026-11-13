#!/usr/bin/env python3
"""Send a real local audio file through the running service without printing its transcript."""
from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import secrets
from pathlib import Path
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 50 * 1024 * 1024


def multipart_layout(audio_path: Path, boundary: str, model: str, language: str):
    fields = [("model", model), ("language", language), ("response_format", "json")]
    field_parts: list[bytes] = []
    for name, value in fields:
        field_parts.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    filename = audio_path.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_header = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = b"\r\n" + f"--{boundary}--\r\n".encode()
    length = sum(map(len, field_parts)) + len(file_header) + audio_path.stat().st_size + len(tail)
    return field_parts, file_header, tail, length


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="Path to a real local audio file (it is sent only to the loopback service).")
    parser.add_argument("--endpoint", default="http://127.0.0.1:10301/v1/audio/transcriptions")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default="auto", help="Whisper language code, or auto (default).")
    args = parser.parse_args()

    audio_path = args.audio.expanduser().resolve()
    if not audio_path.is_file():
        parser.error(f"Audio file does not exist: {audio_path}")
    if audio_path.stat().st_size == 0:
        parser.error("Audio file is empty.")

    url = urlsplit(args.endpoint)
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        parser.error("For safety, the verifier only sends audio to 127.0.0.1 over HTTP.")
    if not url.path:
        parser.error("Endpoint path is required.")

    boundary = "----PiWhisperVerify" + secrets.token_hex(16)
    # Include multipart framing in the request limit, matching the server's 50 MiB cap.
    field_parts, file_header, tail, content_length = multipart_layout(audio_path, boundary, args.model, args.language)
    if content_length > MAX_REQUEST_BYTES:
        parser.error("Audio and multipart body exceed the service's 50 MiB request limit.")

    host = url.hostname
    port = url.port or 80
    connection = http.client.HTTPConnection(host, port, timeout=900)
    try:
        connection.putrequest("POST", url.path + (f"?{url.query}" if url.query else ""))
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        for part in field_parts:
            connection.send(part)
        connection.send(file_header)
        with audio_path.open("rb") as audio:
            while chunk := audio.read(1024 * 1024):
                connection.send(chunk)
        connection.send(tail)
        response = connection.getresponse()
        payload = response.read()
    except OSError as exc:
        raise SystemExit(f"Could not reach the local transcription service: {exc}") from exc
    finally:
        connection.close()

    try:
        result = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Service returned invalid JSON (HTTP {response.status}).") from exc
    if response.status != 200:
        raise SystemExit(f"Transcription failed (HTTP {response.status}): {result.get('error', 'unknown error')}")
    transcript = result.get("text")
    if not isinstance(transcript, str) or not transcript.strip():
        raise SystemExit("Service returned no transcript text.")

    # Do not print private audio contents or the transcript.
    print(f"Integration test passed: {audio_path.name} → local {args.model} ({len(transcript)} transcript characters).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
