#!/bin/bash
# Stop any existing gunicorn process bound to port 8000
pkill -f 'gunicorn.*8000' || true
sleep 1

# Start gunicorn in background daemon mode
cd "$(dirname "$0")"
./venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --daemon --capture-output --log-file ./gunicorn.log app:app
exit 0
