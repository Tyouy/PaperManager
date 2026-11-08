#!/bin/bash
set -e
cd "$(dirname "$0")"
exec .venv/bin/python desktop_launcher.py --stop
