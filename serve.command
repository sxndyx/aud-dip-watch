#!/bin/zsh
# Double-click to run AUD Dip Watch on this Mac. Ctrl-C (or close the window) to stop.
cd "$(dirname "$0")"
exec python3 server.py
