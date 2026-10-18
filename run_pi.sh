#!/usr/bin/env bash
# SmartMart on the Raspberry Pi. Usage from the project folder:
#   ./run_pi.sh            real hardware (needs ADMIN_PASSWORD in .env or the environment)
#   ./run_pi.sh --demo     no hardware, Test scan + fake scale in Admin
# Extra arguments are passed to app.py (e.g. --port 5001).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
  echo "Creating venv (with system packages so apt's python3-lgpio is visible) ..."
  python3 -m venv --system-site-packages venv
  venv/bin/pip install -q -r requirements.txt
  if ! venv/bin/pip install -q -r requirements-pi.txt; then
    echo "WARNING: some Pi packages failed to install; see README 'Troubleshooting'. The web app still runs."
  fi
  venv/bin/pip install -q --no-deps mfrc522 || true
fi

exec venv/bin/python app.py --server waitress "$@"
