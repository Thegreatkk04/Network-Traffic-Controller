#!/bin/bash
# Usage: ./run_engine.sh [--mode default|equal|v1|v2] [--down kbps] [--up kbps] [--dry-run]
cd "$(dirname "$0")"
sudo .venv/bin/python -m controller.engine --iface wlp0s20f3 "$@"
