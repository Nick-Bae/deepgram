#!/bin/bash
# Always run from the backend folder so .env is picked up consistently.
cd "$(dirname "$0")" || exit 1

source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
