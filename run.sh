#!/bin/bash
cd "$(dirname "$0")"
lsof -ti:${PORT:-3001} | xargs kill 2>/dev/null
PORT=${PORT:-3001} DATABASE_URL="${DATABASE_URL:-sqlite:///cortex.db}" .venv/bin/python3 cortex.py
