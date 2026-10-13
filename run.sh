#!/bin/zsh
# LocalTag 실행 (처음 실행할 때 가상환경을 만들고 필요한 라이브러리를 설치합니다)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m localtag "$@"
