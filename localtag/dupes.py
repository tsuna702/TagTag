"""같은 곡의 여러 버전 찾기 (YouTube 판 AAC 128kbps 와 고음질 FLAC 이 함께 있을 때 등).

제목(없으면 파일명)에서 YouTube 꼬리말·JDownloader 꼬리말을 걷어 낸 뒤 같은 이름끼리 묶는다.
묶음마다 음질이 가장 좋은 곡을 남기고, 나머지를 체크해 둔다.
"""
from __future__ import annotations

import os
import re
import unicodedata

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QSpinBox,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .covermatch import norm
from .model import fmt_length, fmt_size, quality_label
from .tagio import AudioFile
from .ytclean import split_title, strip_noise


def title_key(f: AudioFile) -> str:
    """묶을 때 쓰는 이름: 꼬리말을 뗀 제목 (앞에 '아티스트 - '가 붙어 있으면 그것도 뗀다)."""
    t = f.get("title")
    if not t:
        # 파일명 "01 제목" · "01. 제목" 의 앞 번호
        t = re.sub(r"^\s*\d{1,3}(?:[\s.\-_)]+)", "", norm_tail(f.stem))
    t = strip_noise(t, keep_cover=True)
    _, t = split_title(t)
    return _plain(norm(t))


def norm_tail(s: str) -> str:
    """JDownloader 꼬리말 '(128kbit_AAC)' 등을 먼저 뗀다 (번호 떼기 전에)."""
    return re.sub(r"\((?:\d+kbit_\w+|bq|hq|mq|sd|maxres)\)", " ", s, flags=re.I).strip()


def _plain(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())


def find_groups(files, same_artist=False, max_gap=None):
    """[[곡…], …] — 두 곡 이상인 묶음만. 묶음 안은 음질 좋은 순."""
    by = {}
    for f in files:
        k = title_key(f)
        if k:
            by.setdefault(k, []).append(f)
    groups = []
    for members in by.values():
        if len(members) < 2:
            continue
        parts = [members]
        if same_artist:
            # 아티스트가 비어 있는 곡은 어느 쪽과도 같다고 본다 (YouTube 에서 받은 곡)
            named = {}
            blank = []
            for f in members:
                a = _plain(f.get("artist"))
                (named.setdefault(a, []) if a else blank).append(f)
            parts = [v + blank for v in named.values()] if named else [blank]
        for part in parts:
            if max_gap is not None:
                part = sorted(part, key=lambda f: f.length)
                runs, cur = [], [part[0]]
                for f in part[1:]:
                    if f.length - cur[-1].length <= max_gap:
                        cur.append(f)
                    else:
                        runs.append(cur)
                        cur = [f]
                runs.append(cur)
            else:
                runs = [part]
            for r in runs:
                if len(r) >= 2:
                    groups.append(sorted(r, key=lambda f: f.quality_key(), reverse=True))
    # 겹치는 곡이 생기지 않게 (아티스트 없는 곡이 여러 묶음에 들어갈 수 있다)
    seen, out = set(), []
    for g in sorted(groups, key=lambda g: -len(g)):
        g = [f for f in g if id(f) not in seen]
        if len(g) >= 2:
            seen.update(id(f) for f in g)
            out.append(g)
    out.sort(key=lambda g: (g[0].get("title") or g[0].stem).casefold())
    return out


def describe_quality(f: AudioFile) -> str:
    parts = [f.codec or f.ext.lstrip(".").upper()]
    if f.bitrate:
        parts.append(f"{f.bitrate // 1000} kbps")
    if f.sample_rate:
        parts.append(f"{f.sample_rate / 1000:g} kHz")
    if f.bits:
        parts.append(f"{f.bits} bit")
    return " · ".join(parts)


class DuplicatesDialog(QDialog):
    """결과: self.picked — 체크한 곡, self.mode — 'select' (목록에서 고르기) 또는 'remove' (목록에서 빼기)."""

    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.files, self.settings = files, settings
        self.picked, self.mode = [], None
        self.setWindowTitle("같은 곡 · 여러 버전 찾기")
        self.resize(1000, 620)
        v = QVBoxLayout(self)
        d = QLabel("제목이 같은 곡을 묶어 보여 줍니다 (YouTube·JDownloader 꼬리말은 빼고 비교). "
                   "묶음마다 음질이 가장 좋은 곡(★)을 남기고 나머지를 체크해 둡니다. "
                   "더블클릭하면 재생해서 들어 볼 수 있습니다.")
        d.setWordWrap(True)
        v.addWidget(d)

        row = QHBoxLayout()
        self.same_artist = QCheckBox("아티스트도 같아야 함 (아티스트가 빈 곡은 어디든 묶음)")
        self.same_artist.setChecked(settings.value("dupes/same_artist", False, bool))
        self.use_gap = QCheckBox("길이 차이가")
        self.use_gap.setChecked(settings.value("dupes/use_gap", False, bool))
        self.gap = QSpinBox()
        self.gap.setRange(1, 120)
        self.gap.setSuffix(" 초")
        self.gap.setValue(settings.value("dupes/gap", 5, int))
        row.addWidget(self.same_artist)
        row.addSpacing(16)
        row.addWidget(self.use_gap)
        row.addWidget(self.gap)
        row.addWidget(QLabel("이내인 곡만 (MV 판처럼 앞뒤가 긴 곡은 빠질 수 있음)"))
        row.addStretch(1)
        v.addLayout(row)
        for w in (self.same_artist, self.use_gap):
            w.toggled.connect(self.rebuild)
        self.gap.valueChanged.connect(self.rebuild)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["파일명", "음질", "형식", "길이", "크기", "아티스트", "경로"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        hh = self.tree.header()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for c, wdt in enumerate((300, 60, 200, 60, 75, 140)):
            self.tree.setColumnWidth(c, wdt)
        hh.setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(self._play)
        self.tree.itemChanged.connect(lambda *_: self._update_summary())
        v.addWidget(self.tree, 1)

        self.summary = QLabel()
        v.addWidget(self.summary)

        bb = QDialogButtonBox()
        self.sel_btn = bb.addButton("체크한 곡을 목록에서 고르기", QDialogButtonBox.AcceptRole)
        self.rm_btn = bb.addButton("체크한 곡을 목록에서 빼기", QDialogButtonBox.AcceptRole)
        bb.addButton("닫기", QDialogButtonBox.RejectRole)
        self.sel_btn.clicked.connect(lambda: self._finish("select"))
        self.rm_btn.clicked.connect(lambda: self._finish("remove"))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.rebuild()

    def rebuild(self, *_):
        self.settings.setValue("dupes/same_artist", self.same_artist.isChecked())
        self.settings.setValue("dupes/use_gap", self.use_gap.isChecked())
        self.settings.setValue("dupes/gap", self.gap.value())
        self.gap.setEnabled(self.use_gap.isChecked())
        groups = find_groups(self.files, self.same_artist.isChecked(),
                             self.gap.value() if self.use_gap.isChecked() else None)
        self.tree.blockSignals(True)
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        for g in groups:
            head = QTreeWidgetItem([f"{g[0].get('title') or g[0].stem}  ({len(g)}곡)"])
            head.setFirstColumnSpanned(True)
            head.setFont(0, bold)
            head.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(head)
            for i, f in enumerate(g):
                it = QTreeWidgetItem([("★ " if i == 0 else "") + f.filename, quality_label(f),
                                      describe_quality(f), fmt_length(f.length), fmt_size(f.size),
                                      f.get("artist"), f.folder])
                it.setData(0, Qt.UserRole, f)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Unchecked if i == 0 else Qt.Checked)
                it.setToolTip(0, f.path)
                if i == 0:
                    it.setFont(0, bold)
                head.addChild(it)
            head.setFirstColumnSpanned(True)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self.groups = groups
        self._update_summary()

    def _checked(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            for j in range(head.childCount()):
                it = head.child(j)
                if it.checkState(0) == Qt.Checked:
                    out.append(it.data(0, Qt.UserRole))
        return out

    def _update_summary(self):
        n = len(self._checked())
        total = sum(len(g) for g in self.groups)
        if not self.groups:
            self.summary.setText(f"곡 {len(self.files)}개 중에서 같은 곡을 찾지 못했습니다.")
        else:
            self.summary.setText(f"곡 {len(self.files)}개 중 묶음 {len(self.groups)}개 ({total}곡) · 체크 {n}곡")
        self.sel_btn.setEnabled(n > 0)
        self.rm_btn.setEnabled(n > 0)

    def _play(self, item, _col):
        f = item.data(0, Qt.UserRole)
        if f is not None and os.path.exists(f.path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(f.path))

    def _finish(self, mode):
        self.picked, self.mode = self._checked(), mode
        if self.picked:
            self.accept()
