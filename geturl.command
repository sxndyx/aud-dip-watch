#!/bin/zsh
# Double-click to see the current public URL (it changes each time the tunnel restarts).
cd "$(dirname "$0")"
URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' data/tunnel.log | tail -1)
echo "AUD Dip Watch"
echo "  public:  ${URL:-(tunnel not up yet - try again in a few seconds)}"
echo "  on this Mac: http://localhost:8765"
echo
echo "Press any key to close."; read -k1 -s
