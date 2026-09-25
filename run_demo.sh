#!/usr/bin/env bash
# Starts the web app and the (simulated) Mac mini agent side by side.
# Open http://127.0.0.1:8000 and log in as sales1 / sales123.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { sed "s/change-me-to-a-long-random-string/$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')/" .env.example > .env; echo "created .env"; }
[ -f templates/quotation_template.docx ] || .venv/bin/python scripts/make_template.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
WEB=$!
trap 'kill $WEB $AGENT 2>/dev/null' EXIT
sleep 2
.venv/bin/python -m agent.agent &
AGENT=$!
echo "Web: http://127.0.0.1:8000  (sales1/sales123, admin/admin123) - Ctrl+C to stop"
wait
