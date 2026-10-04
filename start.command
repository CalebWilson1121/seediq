#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv || exit 1
fi
source .venv/bin/activate
pip install -r requirements.txt || exit 1
( sleep 2; open "http://127.0.0.1:8000/data-hub.html" ) &
uvicorn server:app --host 127.0.0.1 --port 8000
