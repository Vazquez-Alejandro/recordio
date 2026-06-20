#!/bin/bash
source venv/bin/activate 2>/dev/null || true
if [ ! -d "venv" ]; then
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
fi
uvicorn main:app --reload --host 0.0.0.0 --port 8223
