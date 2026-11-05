#!/bin/zsh
# 맥의 .venv 를 릴리스를 빌드한 것과 같은 파이썬·라이브러리 버전으로 맞춘다 (requirements-lock.txt). 이미 맞으면 바로 끝난다.
# python3 은 더 새 버전을 가리킬 수 있어 python3.14 라고 적는다.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  if ! command -v python3.14 > /dev/null; then
    echo "파이썬 3.14 가 필요합니다: brew install python@3.14" >&2
    exit 1
  fi
  python3.14 -m venv .venv
fi
.venv/bin/pip install -q -r requirements-lock.txt
