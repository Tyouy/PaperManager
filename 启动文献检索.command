#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import pymupdf' >/dev/null 2>&1; then
  exec /bin/bash ./启动程序.command --library
fi
exec .venv/bin/python desktop_launcher.py
