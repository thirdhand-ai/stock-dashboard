#!/usr/bin/env bash
# Production-safe launch script for the Streamlit dashboard.
#
# Reads $PORT if the host platform provides one (a common convention on
# Render/Railway/Heroku-style platforms), otherwise defaults to 8501.
# Binds to 0.0.0.0 so it's reachable from outside the container/VM, not
# just localhost - safe to do here since the dashboard never accepts
# credentials as input and reads them only from the environment, never
# rendering them to the page (see dashboard/data.py's module docstring).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8501}"

exec venv/bin/streamlit run dashboard/app.py \
    --server.port "$PORT" \
    --server.address 0.0.0.0 \
    --server.headless true
