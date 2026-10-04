#!/bin/zsh
# Двойной клик в Finder: запускает интерфейс и открывает его в браузере.
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/python -m pip -q install -U pip && .venv/bin/pip -q install proxy_tools && .venv/bin/pip -q install --prefer-binary -r requirements.txt; }
exec .venv/bin/streamlit run app.py
