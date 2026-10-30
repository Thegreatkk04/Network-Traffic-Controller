#!/bin/bash
# Streamlit dashboard (normal user, no sudo). Opens on port 8501.
cd "$(dirname "$0")"
.venv/bin/streamlit run dashboard/app.py --server.address 0.0.0.0 --server.port 8501
