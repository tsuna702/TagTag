"""옮겨진 파일 다시 찾기.

Apple Music의 'Music 미디어 폴더 정리'가 켜져 있으면, 태그를 바꾼 곡의 파일·앨범 폴더 이름을
Music이 새 태그에 맞게 바꾼다 (예: 'Album' → 'Album 3'). 같은 디스크 안에서 이름만 바뀌면
파일 번호(inode)는 그대로이므로 그것으로 새 위치를 찾는다.
"""
from __future__ import annotations

import os

MEDIA_DIRS = ("Media", "Media.localized", "Music Media", "Music Media.localized", "iTunes Media")


def _media_root(path):
    for name in MEDIA_DIRS:
        i = path.find("/" + name + "/")
        if i >= 0:
            return path[:i + len(name) + 1]
    return None


def _scan(root, missing, found):
    """root 아래를 돌며 찾는 파일 번호와 같은 파일을 찾는다.

    디렉터리 목록에 이미 들어 있는 파일 번호(inode)로 먼저 거르고, 맞을 때만 stat 으로 확인한다
    (미디어 폴더 전체를 훑어도 파일마다 stat 을 부르지 않아 빠르다). 목록의 번호가 실제 번호와 다른
    디스크(일부 네트워크·외장 디스크)에서는 파일마다 stat 으로 확인한다.
    """
    inodes = {ino for _, ino in missing}
    trust = None  # 목록의 파일 번호를 믿어도 되는지 (첫 파일에서 확인)
    stack = [root]
    while stack and missing:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if trust and e.inode() not in inodes:
                        continue
                    st = e.stat(follow_symlinks=False)
                    if trust is None:
                        trust = st.st_ino == e.inode()
                except OSError:
                    continue
                old = missing.pop((st.st_dev, st.st_ino), None)
                if old is not None:
                    found[old] = e.path
                    if not missing:
                        return


def find_moved(entries, cancelled=lambda: False):
    """entries: [(옛 경로, dev, ino)] → {옛 경로: 새 경로} (지금 없는 파일만 찾는다)."""
    missing = {(dev, ino): path for path, dev, ino in entries if ino and not os.path.exists(path)}
    found = {}
    if not missing:
        return found
    # 1) 아티스트 폴더 (앨범 폴더 이름만 바뀐 경우가 대부분) → 2) 미디어 폴더 전체
    near, far = set(), set()
    for path in missing.values():
        album_dir = os.path.dirname(path)
        near.add(os.path.dirname(album_dir))
        far.add(_media_root(path) or os.path.dirname(os.path.dirname(album_dir)))
    searched = []
    for root in sorted(near) + sorted(far - near):
        if not missing or cancelled():
            break
        if not os.path.isdir(root) or any(root.startswith(s + os.sep) or root == s for s in searched):
            continue
        _scan(root, missing, found)
        searched.append(root)
    return found
