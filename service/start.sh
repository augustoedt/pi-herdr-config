#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_PYTHON="$HOME/Projects/Side/ai-apps/summarize-video/.venv/bin/python"
if [ -n "${PI_WHISPER_PYTHON:-}" ]; then
  PYTHON=$PI_WHISPER_PYTHON
elif [ -x "$DEFAULT_PYTHON" ]; then
  PYTHON=$DEFAULT_PYTHON
else
  PYTHON=$(command -v python3 || true)
fi

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  printf 'Python 3 not found. Set PI_WHISPER_PYTHON to an environment with openai-whisper and torch.\n' >&2
  exit 1
fi
if ! "$PYTHON" -c 'import torch, whisper' >/dev/null 2>&1; then
  printf 'Whisper dependencies not available in %s. Set PI_WHISPER_PYTHON to an environment with openai-whisper and torch.\n' "$PYTHON" >&2
  exit 1
fi
exec "$PYTHON" "$SCRIPT_DIR/server.py"
