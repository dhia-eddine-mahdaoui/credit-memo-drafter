#!/usr/bin/env bash
# One command: create venv, install, run the tests, then run the full pipeline.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python -m pytest -q
python -m creditmemo run
