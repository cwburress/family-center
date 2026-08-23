#!/bin/bash
# Stop any existing gunicorn process bound to port 8000
pkill -f 'gunicorn.*8000' || true
sleep 1

# Start gunicorn in background daemon mode
cd /home/chrisb/family_center
./venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --daemon --capture-output --log-file /home/chrisb/family_center/gunicorn.log app:app
exit 0
