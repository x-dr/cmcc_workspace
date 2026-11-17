#!/bin/sh
set -eu

cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${LINGXI_PYTHON:-}" ]; then
    python_bin=$LINGXI_PYTHON
elif [ -x .venv/bin/python ]; then
    python_bin=.venv/bin/python
else
    python_bin=python3
fi
exec "$python_bin" -X utf8 app.py
