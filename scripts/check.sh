#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python -m compileall -q src tests
python -m pytest -q -ra
python -m pip check
if python -c 'import build' >/dev/null 2>&1; then
  python -m build
fi