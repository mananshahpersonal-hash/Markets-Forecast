#!/bin/bash
cd "$(dirname "$0")"
echo "Setting up (first run takes a minute)..."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
echo "Launching dashboard in your browser..."
streamlit run app.py
