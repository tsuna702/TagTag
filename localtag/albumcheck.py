"""앨범 점검: Apple Music 에서 앨범이 갈라져 보이게 만드는 태그 차이를 찾아 맞춘다.

같은 앨범(이름이 같은 곡들) 안에서 앨범 이름 표기 · 앨범 아티스트 · 컴필레이션 · 디스크·트랙 총수 · 커버가
서로 다르면 Music 은 앨범을 여러 개로 나눠 보여 준다. 연도·장르 차이는 갈라지지 않지만 함께 보여 준다.
트랙 번호가 겹치거나 빠진 것은 알려만 준다 (자동 번호 매기기로 고친다).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from .plan import Plan
from .tagio import AudioFile

COMPILATION = "COMPILATION"
NONE = "(없음)"
# (종류, 제목, Music 에서 앨범이 갈라지는지 → 기본으로 맞추기에 체크)
FIELDS = [
    ("album", "앨범 이름 표기", True),
    ("albumartist", "앨범 아티스트", True),
    (COMPILATION, "컴필레이션", True),
    ("_disc_total", "디스크 총수", True),
    ("_track_total", "트랙 총수", True),
    ("_cover", "커버", True),
    ("year", "연도", False),
    ("genre", "장르", False),
]


def album_key(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip().casefold()


def _total(f: AudioFile, name: str) -> str:
    v = f.get(name)
    return v.split("/", 1)[1].strip() if "/" in v else ""


def _number(f: AudioFile, name: str) -> str:
    return f.get(name).split("/", 1)[0].strip()


def _cover_id(f: AudioFile) -> str:
    c = f.cover
    return hashlib.sha1(c.data).hexdigest()[:12] if c is not None else ""


def value_of(f: AudioFile, kind: str) -> str:
    if kind == "_disc_total":
        return _total(f, "disc")
    if kind == "_track_total":
        return _total(f, "track")
    if kind == "_cover":
        return _cover_id(f)
    if kind == COMPILATION:
        return "1" if f.get(COMPILATION).strip() in ("1", "true", "True") else ""
    return f.get(kind)


@dataclass
class Issue:
    kind: str
    label: str
    files: list
    counts: Counter = field(default_factory=Counter)  # 값 → 곡 수
    fixable: bool = True
    default_on: bool = True
    note: str = ""
    suggest: list = field(default_factory=list)  # 고를 값 (많은 순)


@dataclass
class AlbumReport:
    name: str
    files: list
    issues: list


def check_albums(files) -> list[AlbumReport]:
    groups = {}
    for f in files:
        name = f.get("album")
        if name.strip():
            groups.setdefault(album_key(name), []).append(f)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        issues = []
        for kind, label, splits in FIELDS:
            counts = Counter(value_of(f, kind) for f in members)
            if len(counts) < 2:
                continue
            # 총수: 비어 있는 곡과 섞여 있는 것만으로는 갈라지지 않는 경우가 많지만 맞춰 두는 편이 낫다
            suggest = [v for v, _ in counts.most_common() if v] or [""]
            note = "" if splits else "Music 앨범은 갈라지지 않음"
            issues.append(Issue(kind, label, members, counts, True, splits, note, suggest))
        # 앨범 아티스트가 모두 비어 있고 곡마다 아티스트가 다르면 Music 이 아티스트별로 나눈다
        if not any(f.get("albumartist") for f in members) and not any(value_of(f, COMPILATION) for f in members):
            artists = Counter(f.get("artist") for f in members)
            if len(artists) > 1:
                issues.append(Issue("albumartist", "앨범 아티스트 없음 — 곡마다 아티스트가 달라 앨범이 갈라짐",
                                    members, Counter({"": len(members)}), True, True, "",
                                    [a for a, _ in artists.most_common() if a] + ["Various Artists"]))
        # 트랙 번호 (알려만 준다)
        seen = Counter((_number(f, "disc") or "1", _number(f, "track")) for f in members if _number(f, "track"))
        dup = sorted(f"{d}-{t}" if d != "1" else t for (d, t), n in seen.items() if n > 1)
        if dup:
            issues.append(Issue("_dup", "트랙 번호 겹침: " + ", ".join(dup), members, fixable=False, default_on=False,
                                note="자동 번호 매기기(⌘K)로 고치세요"))
        missing = [f for f in members if not _number(f, "track")]
        if missing:
            issues.append(Issue("_missing", f"트랙 번호 없는 곡 {len(missing)}개", missing, fixable=False,
                                default_on=False, note="자동 번호 매기기(⌘K)로 고치세요"))
        if issues:
            name = Counter(f.get("album") for f in members).most_common(1)[0][0]
            out.append(AlbumReport(name, members, issues))
    out.sort(key=lambda r: r.name.casefold())
    return out


def plans_for(fixes) -> list[Plan]:
    """fixes: [(Issue, 고른 값)] → 곡마다 Plan 하나로 합친다."""
    by_file = {}
    for issue, value in fixes:
        cover = None
        if issue.kind == "_cover":
            cover = next((f.cover for f in issue.files if _cover_id(f) == value), None)
        for f in issue.files:
            p = by_file.setdefault(id(f), Plan(f))
            if issue.kind in ("_disc_total", "_track_total"):
                name = "disc" if issue.kind == "_disc_total" else "track"
                num = _number(f, name) or ("1" if name == "disc" else "")
                if not num:
                    continue
                new = f"{num}/{value}" if value else num
                if new != f.get(name):
                    p.changes[name] = [new]
            elif issue.kind == "_cover":
                if _cover_id(f) == value:
                    continue
                pics = list(f.pictures)
                if cover is None:
                    pics = [x for x in pics if x.type != 3] if len(pics) > 1 else []
                else:
                    idx = f.primary_index()
                    if idx >= 0:
                        pics[idx] = cover
                    else:
                        pics.insert(0, cover)
                p.pictures = pics
            elif issue.kind == COMPILATION:
                if value_of(f, COMPILATION) != value:
                    p.changes[COMPILATION] = ["1"] if value else []
            else:
                vals = [value] if value else []
                if f.tags.get(issue.kind, []) != vals:
                    p.changes[issue.kind] = vals
    return [p for p in by_file.values() if p.touches_tags]


def _value_text(kind, v, n, files=None):
    if kind == "_cover":
        if not v:
            return f"커버 없음 ({n}곡)"
        f = next((x for x in files if _cover_id(x) == v), None)
        size = ""
        if f is not None:
            from PySide6.QtGui import QImage
            img = QImage.fromData(f.cover.data)
            size = f"{img.width()}×{img.height()} · " if not img.isNull() else ""
        return f"커버 {v[:6]} — {size}{n}곡"
    if kind == COMPILATION:
        return f"{'컴필레이션' if v else '컴필레이션 아님'} ({n}곡)"
    return f"{v or NONE} ({n}곡)" if n else (v or NONE)


class AlbumCheckDialog(QDialog):
    """결과: self.plans (맞추기), self.picked (이 앨범 곡 고르기)."""

    def __init__(self, parent, files):
        super().__init__(parent)
        self.files = files
        self.plans, self.picked = [], []
        self.setWindowTitle("앨범 점검")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        d = QLabel("같은 앨범 이름을 가진 곡들 사이에서 Apple Music 앨범을 갈라지게 하는 차이(앨범 이름 표기, 앨범 아티스트, "
                   "컴필레이션, 디스크·트랙 총수, 커버)를 찾습니다. 맞출 값을 고르고 체크한 항목만 맞춥니다.")
        d.setWordWrap(True)
        v.addWidget(d)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["앨범 / 차이", "맞출 값", "참고"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 320)
        self.tree.itemDoubleClicked.connect(self._pick_album)
        v.addWidget(self.tree, 1)
        self.summary = QLabel()
        v.addWidget(self.summary)
        bb = QDialogButtonBox()
        self.fix_btn = bb.addButton("체크한 것 맞추기", QDialogButtonBox.AcceptRole)
        pick = bb.addButton("고른 앨범의 곡을 목록에서 고르기", QDialogButtonBox.ActionRole)
        bb.addButton("닫기", QDialogButtonBox.RejectRole)
        self.fix_btn.clicked.connect(self._fix)
        pick.clicked.connect(lambda: self._pick_album(self.tree.currentItem()))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.reports = check_albums(files)
        self._fill()

    def _fill(self):
        bold = QFont()
        bold.setBold(True)
        gray = QBrush(QColor(140, 140, 140))
        self.rows = []  # (QTreeWidgetItem, Issue, QComboBox)
        for r in self.reports:
            artist = Counter(f.get("albumartist") for f in r.files).most_common(1)[0][0]
            head = QTreeWidgetItem([f"{r.name}" + (f" — {artist}" if artist else "") + f"  ({len(r.files)}곡)"])
            head.setFont(0, bold)
            head.setData(0, Qt.UserRole, r)
            self.tree.addTopLevelItem(head)
            for issue in r.issues:
                it = QTreeWidgetItem([issue.label, "", issue.note])
                it.setData(0, Qt.UserRole, r)
                head.addChild(it)
                if not issue.fixable:
                    it.setForeground(0, gray)
                    it.setForeground(2, gray)
                    continue
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if issue.default_on else Qt.Unchecked)
                cb = QComboBox()
                text_field = issue.kind not in ("_cover", COMPILATION)
                cb.setEditable(text_field)
                for val in issue.suggest:
                    cb.addItem(_value_text(issue.kind, val, issue.counts.get(val, 0), issue.files), val)
                if issue.kind == COMPILATION and "" not in issue.suggest:
                    cb.addItem(_value_text(COMPILATION, "", issue.counts.get("", 0)), "")
                if issue.kind == "_cover" and "" in issue.counts and "" not in issue.suggest:
                    cb.addItem(_value_text("_cover", "", issue.counts[""]), "")
                cb.setToolTip("\n".join(f"{_value_text(issue.kind, k, n, issue.files)}" for k, n in issue.counts.most_common()))
                self.tree.setItemWidget(it, 1, cb)
                self.rows.append((it, issue, cb))
            head.setExpanded(True)
        n_fix = sum(1 for _, i, _ in self.rows)
        if not self.reports:
            self.summary.setText(f"곡 {len(self.files)}개 — 문제 있는 앨범이 없습니다.")
        else:
            self.summary.setText(f"곡 {len(self.files)}개 · 차이가 있는 앨범 {len(self.reports)}개 · 맞출 수 있는 항목 {n_fix}개")
        self.fix_btn.setEnabled(bool(self.rows))

    def _chosen(self, issue, cb):
        i = cb.currentIndex()
        if cb.isEditable():
            text = cb.currentText()
            # 목록의 항목을 그대로 골랐으면 그 값, 직접 쳤으면 친 글자
            if i >= 0 and cb.itemText(i) == text:
                return cb.itemData(i)
            return text.strip()
        return cb.itemData(i)

    def _fix(self):
        fixes = [(issue, self._chosen(issue, cb)) for it, issue, cb in self.rows if it.checkState(0) == Qt.Checked]
        self.plans = plans_for(fixes)
        if self.plans:
            self.accept()

    def _pick_album(self, item, *_):
        if item is None:
            return
        r = item.data(0, Qt.UserRole)
        if r is not None:
            self.picked = list(r.files)
            self.accept()
