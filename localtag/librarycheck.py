"""Music 보관함 점검 (macOS 전용, 읽기만): 커버 없는 곡 · 앨범이 갈라진 곡 · 파일을 못 찾는 곡.

Music 앱에 스크립트를 보내지 않고 Apple 의 iTunesLibrary 프레임워크로 보관함을 읽는다 (Music 이 파일을
다시 확인하거나 옮기지 않는다). 찾은 곡은 LocalTag 목록으로 열어 고친다.
"""
from __future__ import annotations

import ctypes
import os
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

SONG = 2  # ITLibMediaItemMediaKindSong
FILE = 1  # ITLibMediaItemLocationTypeFile


@dataclass
class LibTrack:
    pid: str
    title: str
    artist: str
    album: str
    albumartist: str = ""
    compilation: bool = False
    disc: int = 0
    path: str | None = None
    has_art: bool = True
    exists: bool = True


@dataclass
class Group:
    """앨범 하나 (갈라진 앨범이면 Music 에서 따로 보이는 묶음들)."""
    album: str
    artist: str
    tracks: list = field(default_factory=list)
    parts: list = field(default_factory=list)  # 갈라진 앨범: [(설명, [LibTrack])]


@dataclass
class Report:
    nocover: list = field(default_factory=list)  # [Group]
    split: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    total: int = 0


# ------------------------------------------------------------------ 읽기

def read_library() -> list[LibTrack]:
    """보관함의 노래 전부 (권한: 미디어 및 Apple Music). 작업 스레드에서 부른다."""
    from . import music
    if music.media_library_status() == "ask":
        music.request_media_library()
    o = music._bridge()
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/iTunesLibrary.framework/iTunesLibrary")
    o.lib.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    o.lib.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    pool = o.lib.objc_autoreleasePoolPush()
    try:
        err = ctypes.c_void_p()
        lib = o.send(o.cls("ITLibrary"), "libraryWithAPIVersion:error:", o.nsstr("1.0"), ctypes.byref(err),
                     argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        if not lib:
            raise RuntimeError("Music 보관함을 열지 못했습니다. 시스템 설정 → 개인정보 보호 및 보안 → "
                               "미디어 및 Apple Music 에서 LocalTag 를 허용해 주세요.")
        s = lambda obj, sel: (o.pystr(o.send(obj, sel)) or "") if obj else ""  # noqa: E731 — 곡의 artist 는 ITLibArtist
        out = []
        for it in o.array(o.send(lib, "allMediaItems")):
            if o.send(it, "mediaKind", restype=ctypes.c_long) != SONG:
                continue
            alb = o.send(it, "album")
            path = None
            if o.send(it, "locationType", restype=ctypes.c_long) == FILE:
                url = o.send(it, "location")
                path = o.pystr(o.send(url, "path")) if url else None
            elif not o.send(it, "location"):
                continue  # 구독 곡처럼 파일이 없는 곡은 점검하지 않는다
            pid = o.send(o.send(it, "persistentID"), "unsignedLongLongValue", restype=ctypes.c_ulonglong)
            out.append(LibTrack(
                f"{pid:016X}", s(it, "title"), s(o.send(it, "artist"), "name"), s(alb, "title"), s(alb, "albumArtist"),
                bool(o.send(alb, "isCompilation", restype=ctypes.c_bool)),
                int(o.send(alb, "discNumber", restype=ctypes.c_long) or 0), path,
                bool(o.send(it, "hasArtworkAvailable", restype=ctypes.c_bool)),
                bool(path) and os.path.exists(path)))
        return out
    finally:
        o.lib.objc_autoreleasePoolPop(pool)


# ------------------------------------------------------------------ 점검

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip().casefold()


def _music_key(t: LibTrack):
    """Music 이 앨범을 묶는 기준: 앨범 이름 + (앨범 아티스트, 없으면 컴필레이션이 아닐 때 아티스트) + 컴필레이션."""
    who = t.albumartist or ("" if t.compilation else t.artist)
    return _norm(who), t.compilation


def _part_label(key, tracks) -> str:
    who, comp = key
    t = tracks[0]
    name = t.albumartist or (t.artist if not comp else "")
    bits = [f"앨범 아티스트 '{t.albumartist}'" if t.albumartist else
            ("앨범 아티스트 없음 · 컴필레이션" if comp else f"앨범 아티스트 없음 → 아티스트 '{name}'로 묶임")]
    if comp and t.albumartist:
        bits.append("컴필레이션")
    return f"{' · '.join(bits)} — {len(tracks)}곡"


def analyze(tracks) -> Report:
    rep = Report(total=len(tracks))
    by_album = defaultdict(list)
    for t in tracks:
        if t.exists:
            by_album[_norm(t.album)].append(t)

    def group(ts):
        t = ts[0]
        return Group(t.album or "(앨범 이름 없음)", t.albumartist or t.artist, sorted(ts, key=lambda x: (x.disc, x.title)))

    for key, ts in sorted(by_album.items()):
        nc = [t for t in ts if not t.has_art]
        if nc:
            rep.nocover.append(group(nc))
        if not key:
            continue
        parts = defaultdict(list)
        for t in ts:
            parts[_music_key(t)].append(t)
        if len(parts) < 2:
            continue
        # 이름만 같은 다른 앨범(예: 서로 다른 가수의 'Greatest Hits')은 빼고, 같은 폴더나 같은 아티스트가 겹칠 때만
        folders = [{os.path.dirname(t.path) for t in p} for p in parts.values()]
        artists = [{_norm(t.artist) for t in p} for p in parts.values()]
        related = any(folders[i] & folders[j] or artists[i] & artists[j]
                      for i in range(len(folders)) for j in range(i + 1, len(folders)))
        if related:
            g = group(ts)
            g.parts = [(_part_label(k, p), sorted(p, key=lambda x: (x.disc, x.title)))
                       for k, p in sorted(parts.items(), key=lambda kv: -len(kv[1]))]
            rep.split.append(g)
    gone = defaultdict(list)
    for t in tracks:
        if not t.exists:
            gone[_norm(t.album)].append(t)
    rep.missing = [group(ts) for _k, ts in sorted(gone.items())]
    return rep


# ------------------------------------------------------------------ 창

TABS = [("nocover", "커버 없는 곡", "Music 에서 커버가 보이지 않는 곡입니다. 열어서 커버 → 앨범아트 찾기 등으로 넣으세요."),
        ("split", "갈라진 앨범", "이름이 같은데 Music 에서 여러 앨범으로 나뉘어 보이는 앨범입니다. "
                               "열어서 편집 → 앨범 점검(⇧⌘L)으로 앨범 아티스트·컴필레이션을 맞추세요."),
        ("missing", "파일 없는 곡", "Music 보관함에는 있지만 파일을 찾지 못한 곡입니다 (옮겨졌거나 지워짐). "
                                "열 수는 없고, Music 에서 직접 정리하세요.")]


class LibraryCheckDialog(QDialog):
    """결과: self.picked (열 파일 경로), self.fix_albums (열고 앨범 점검까지)"""

    def __init__(self, parent, report: Report):
        super().__init__(parent)
        self.report = report
        self.picked = []
        self.fix_albums = False
        self.setWindowTitle("Music 보관함 점검")
        self.resize(900, 620)
        v = QVBoxLayout(self)
        n = {k: sum(len(g.tracks) for g in getattr(report, k)) for k, *_ in TABS}
        v.addWidget(QLabel(f"보관함의 노래 {report.total}곡 — 커버 없음 {n['nocover']}곡 · "
                           f"갈라진 앨범 {len(report.split)}개 · 파일 없음 {n['missing']}곡 (Music 은 건드리지 않고 읽기만 했습니다)"))
        self.tabs = QTabWidget()
        self.trees = {}
        for key, title, hint in TABS:
            box = QWidget()
            page = QVBoxLayout(box)
            h = QLabel(hint)
            h.setWordWrap(True)
            h.setStyleSheet("color: gray;")
            page.addWidget(h)
            tree = QTreeWidget()
            tree.setHeaderLabels(["앨범 · 곡", "아티스트", "파일"])
            tree.setColumnWidth(0, 360)
            tree.setColumnWidth(1, 160)
            tree.setSelectionMode(QTreeWidget.ExtendedSelection)
            tree.itemDoubleClicked.connect(lambda *_: self.open_picked())
            self._fill(tree, getattr(report, key))
            page.addWidget(tree)
            self.trees[key] = tree
            groups = getattr(report, key)
            self.tabs.addTab(box, f"{title} ({len(groups) if key == 'split' else n[key]})")
        v.addWidget(self.tabs, 1)
        row = QHBoxLayout()
        self.open_btn = QPushButton("고른 곡을 LocalTag 에서 열기")
        self.open_btn.setToolTip("앨범을 고르면 그 앨범의 곡 전부. 아무것도 안 고르면 이 탭의 곡 전부")
        self.open_btn.clicked.connect(self.open_picked)
        row.addWidget(self.open_btn)
        self.fix_btn = QPushButton("열어서 앨범 점검으로 맞추기")
        self.fix_btn.setToolTip("고른 앨범(안 고르면 전부)을 열고 바로 앨범 점검(⇧⌘L)을 띄웁니다. "
                                "맞춘 뒤 Music 에도 반영해 앨범을 하나로 합칩니다")
        self.fix_btn.clicked.connect(self.fix_picked)
        row.addWidget(self.fix_btn)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("닫기")
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._tab_changed(0)

    def _fill(self, tree, groups):
        def leaf(parent, t):
            it = QTreeWidgetItem(parent, [t.title, t.artist, t.path or "(파일 없음)"])
            it.setData(0, Qt.UserRole, t.path if t.exists else None)
            it.setToolTip(2, t.path or "")
            return it

        for g in groups:
            top = QTreeWidgetItem(tree, [g.album, g.artist, f"{len(g.tracks)}곡"])
            if g.parts:
                for label, ts in g.parts:
                    mid = QTreeWidgetItem(top, [label])
                    for t in ts:
                        leaf(mid, t)
                    mid.setExpanded(True)
                top.setExpanded(True)  # 어떻게 갈라졌는지 바로 보이게
            else:
                for t in g.tracks:
                    leaf(top, t)

    def _tab_changed(self, i):
        self.open_btn.setEnabled(TABS[i][0] != "missing" and self.trees[TABS[i][0]].topLevelItemCount() > 0)
        self.fix_btn.setVisible(TABS[i][0] == "split")
        self.fix_btn.setEnabled(self.trees["split"].topLevelItemCount() > 0)

    def fix_picked(self):
        self.picked = self.paths_of(self.trees["split"])
        if self.picked:
            self.fix_albums = True
            self.accept()

    def paths_of(self, tree) -> list[str]:
        items = tree.selectedItems() or [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        out = []

        def walk(it):
            p = it.data(0, Qt.UserRole)
            if p:
                out.append(p)
            for k in range(it.childCount()):
                walk(it.child(k))

        for it in items:
            walk(it)
        return list(dict.fromkeys(out))

    def open_picked(self):
        key = TABS[self.tabs.currentIndex()][0]
        if key == "missing":
            return
        self.picked = self.paths_of(self.trees[key])
        if self.picked:
            self.accept()
