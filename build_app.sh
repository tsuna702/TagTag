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

# 키체인에 자체 서명 인증서가 있으면 그것으로 서명한다. 서명 주체가 버전마다 같아서
# macOS 가 새 버전마다 Music 권한(자동화·보관함 접근)을 다시 묻지 않는다. 없으면 PyInstaller 임시 서명 그대로.
SIGN_ID="${LOCALTAG_SIGN_ID:-LocalTag Signing}"
if security find-identity -p codesigning | grep -q "\"$SIGN_ID\""; then
  codesign --force --deep --timestamp=none --sign "$SIGN_ID" dist/LocalTag.app
  codesign --verify --deep --strict dist/LocalTag.app
  echo "서명: $SIGN_ID"
else
  echo "서명: 임시(ad-hoc) — 키체인에 '$SIGN_ID' 인증서가 없음"
fi
echo
echo "완료: $(pwd)/dist/LocalTag.app"
