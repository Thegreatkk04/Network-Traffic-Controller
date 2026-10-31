#!/bin/bash
# Engine (needs root). Usage: ./run_service.sh [--down kbps] [--up kbps] [--mode v2] [--dry-run] [--auto]
cd "$(dirname "$0")"
sudo .venv/bin/python -m controller.service --iface wlp0s20f3 "$@"
