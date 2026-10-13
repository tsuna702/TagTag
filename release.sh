#!/bin/zsh
# 빌드 → zip → GitHub 릴리스까지 한 번에.
# 버전은 packaging/LocalTag.spec 의 CFBundleShortVersionString 을 따른다.
set -e
cd "$(dirname "$0")"

VERSION=$(grep -o '"CFBundleShortVersionString": "[^"]*"' packaging/LocalTag.spec | cut -d'"' -f4)
TAG="v$VERSION"
REPO="tsuna702/TagTag"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "커밋하지 않은 변경이 있습니다. 먼저 커밋해 주세요." >&2
  exit 1
fi
git fetch -q --tags origin
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "$TAG 릴리스가 이미 있습니다. packaging/LocalTag.spec 의 버전을 올려 주세요." >&2
  exit 1
fi

./build_app.sh
ZIP="dist/LocalTag-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent dist/LocalTag.app "$ZIP"

# 지난 릴리스 이후 커밋 제목으로 바뀐 점 목록을 만든다
PREV=$(git tag --sort=-v:refname | grep '^v' | head -1 || true)
NOTES=$(mktemp)
{
  echo "## LocalTag $VERSION"
  echo
  echo "### 바뀐 점"
  if [ -n "$PREV" ]; then
    git log "$PREV"..HEAD --no-merges --pretty='- %s'
  else
    git log --no-merges --pretty='- %s'
  fi
  echo
  echo "### 설치"
  echo "1. \`LocalTag-$VERSION.zip\`을 내려받아 압축을 풉니다."
  echo "2. \`LocalTag.app\`을 \`응용 프로그램\` 폴더로 옮깁니다."
  echo "3. 서명하지 않은 앱이라 처음 한 번은 **우클릭 → 열기**로 엽니다."
  echo
  echo "Apple Silicon Mac에서 빌드했습니다."
} > "$NOTES"

git push -q origin main
gh release create "$TAG" "$ZIP" --repo "$REPO" --title "LocalTag $VERSION" --notes-file "$NOTES" --target main
rm -f "$NOTES"
echo "릴리스 완료: https://github.com/$REPO/releases/tag/$TAG"
