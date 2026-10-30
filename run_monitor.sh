#!/bin/bash
# Usage: ./run_monitor.sh [--mode v1|v2] [--link kbps] [--record LABEL --app NAME]
cd "$(dirname "$0")"
sudo .venv/bin/python live/monitor.py --iface wlp0s20f3 "$@"
