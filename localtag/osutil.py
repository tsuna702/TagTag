"""운영체제마다 다른 부분 (macOS / Windows)."""
from __future__ import annotations

import os
import re
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

FILE_MANAGER = "Finder" if IS_MAC else "탐색기"
REVEAL_LABEL = f"{FILE_MANAGER}에서 보기"


def reveal(path: str):
    """파일 관리자에서 파일을 골라 보여 준다."""
    if IS_MAC:
        subprocess.run(["open", "-R", path], check=False)
    elif IS_WIN:
        subprocess.run(f'explorer /select,"{os.path.normpath(path)}"', check=False)
    else:
        open_folder(os.path.dirname(path))


def open_folder(folder: str):
    """폴더를 파일 관리자로 연다."""
    if IS_MAC:
        subprocess.run(["open", folder], check=False)
    elif IS_WIN:
        os.startfile(os.path.normpath(folder))
    else:
        subprocess.run(["xdg-open", folder], check=False)


_MODS = {"⌃": "Ctrl", "⌥": "Alt", "⇧": "Shift", "⌘": "Ctrl"}
_KEY_RE = re.compile("[⌃⌥⇧⌘]+")


def keys(text: str) -> str:
    """UI 문구 속 맥 단축키 기호(⌘ ⌥ ⇧)를 이 OS 표기로 (Windows: '⇧⌘R' → 'Ctrl+Shift+R')."""
    if IS_MAC:
        return text

    def sub(m):
        mods = [name for sym, name in _MODS.items() if sym in m.group()]
        order = ["Ctrl", "Alt", "Shift"]
        return "+".join(sorted(set(mods), key=order.index)) + "+"
    return _KEY_RE.sub(sub, text)
