#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python is not installed. Get it from https://www.python.org/downloads/"
  read -n 1 -s -r -p "Press any key to close"
  exit 1
fi
python3 app.py
