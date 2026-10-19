#!/usr/bin/env bash
# start.sh — запуск охранной камеры (macOS / Linux).
# Просто обёртка над run_local.sh (тот сам находит venv и проверяет окружение).
set -euo pipefail
cd "$(dirname "$0")"
exec ./run_local.sh "$@"
