#!/bin/bash
# Starts hotspot + engine + dashboard together. Ctrl+C stops everything and removes shaping.
# Usage: ./start_all.sh                    (v2, auto link speed, port 8501)
#        AUTO=0 ./start_all.sh             (fixed link 3000/1500, e.g. for experiments)
#        MODE=v3 DOWN=5000 UP=2000 ./start_all.sh
cd "$(dirname "$0")"
MODE=${MODE:-v2}; DOWN=${DOWN:-3000}; UP=${UP:-1500}; PORT=${PORT:-8501}; AUTO=${AUTO:-1}

sudo -v || exit 1                         # ask for the password once, up front
./start_hotspot.sh
mkdir -p logs

sudo .venv/bin/python -m controller.service --iface wlp0s20f3 \
     --mode "$MODE" --down "$DOWN" --up "$UP" ${DRY:+--dry-run} $([ "$AUTO" = 1 ] && echo --auto) \
     > logs/service.log 2>&1 &
ENGINE=$!

cleanup() {
  echo; echo "Stopping engine..."
  sudo kill -TERM "$ENGINE" 2>/dev/null
  wait "$ENGINE" 2>/dev/null
  tail -1 logs/service.log
}
trap cleanup EXIT

sleep 4
if ! kill -0 "$ENGINE" 2>/dev/null; then
  echo "Engine failed to start. Last lines of logs/service.log:"
  tail -20 logs/service.log
  exit 1
fi
echo "Engine running (log: logs/service.log)"
echo "Dashboard: http://localhost:$PORT   (phones on hotspot: http://10.42.0.1:$PORT)"
.venv/bin/streamlit run dashboard/app.py --server.address 0.0.0.0 --server.port "$PORT"
