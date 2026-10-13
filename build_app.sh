#!/bin/zsh
# LocalTag.app 빌드 → dist/LocalTag.app
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt pyinstaller
[ -f packaging/LocalTag.icns ] || .venv/bin/python packaging/make_icon.py packaging/LocalTag.icns
.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/LocalTag.spec
echo
echo "완료: $(pwd)/dist/LocalTag.app"
