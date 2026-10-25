#!/usr/bin/env bash
# 한 줄 실행: ./run.sh  (포트 변경: PORT=9000 ./run.sh)
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --python 3.12 uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8765}"
