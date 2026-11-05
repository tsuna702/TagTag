#!/bin/zsh
# LocalTag 실행 (처음 실행할 때 가상환경을 만들고 필요한 라이브러리를 설치합니다)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/pyinstaller ]; then  # 처음이거나 지난번 설치가 끊겼을 때
  ./make_venv.sh || exit 1
fi
exec .venv/bin/python -m localtag "$@"
