"""고른 곡 몇 개의 태그를 나란히 놓고 비교한다. 값이 다른 줄은 색으로 표시한다."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .dupes import describe_quality
from .model import fmt_length, fmt_size
from .tagio import FIELD_LABELS, FIELDS, AudioFile

MAX_FILES = 8
COVER_SIDE = 72


def _cover_text(f: AudioFile) -> str:
    if not f.pictures:
        return ""
    from PySide6.QtGui import QImage
    pic = f.cover
    img = QImage.fromData(pic.data)
    dims = f"{img.width()}×{img.height()}" if not img.isNull() else "?"
    more = f" (+{len(f.pictures) - 1})" if len(f.pictures) > 1 else ""
    return f"{dims} · {fmt_size(len(pic.data))}{more}"


# (제목, 값 함수) — 태그 위에 놓는 파일 정보
INFO_ROWS = [
    ("파일명", lambda f: f.filename),
    ("경로", lambda f: f.folder),
    ("음질", describe_quality),
    ("길이", lambda f: fmt_length(f.length)),
    ("크기", lambda f: fmt_size(f.size)),
    ("태그 종류", lambda f: f.tag_type),
]


def rows_for(files):
    """[(제목, [값…], 파일 정보 줄인지)] — 기본 필드, 그 밖의 필드(이름순) 순."""
    rows = [(label, [fn(f) for f in files], True) for label, fn in INFO_ROWS]
    keys = [k for k in FIELDS if any(k in f.tags for f in files)]
    extra = sorted({k for f in files for k in f.tags} - set(FIELDS), key=str.casefold)
    for k in keys + extra:
        rows.append((FIELD_LABELS.get(k, k), [f.get(k) for f in files], False))
    rows.append(("커버", [_cover_text(f) for f in files], False))
    return rows


class CompareDialog(QDialog):
    def __init__(self, parent, files):
        super().__init__(parent)
        self.files, self.total = files[:MAX_FILES], len(files)
        self.setWindowTitle(f"태그 비교 ({len(self.files)}곡)")
        self.resize(min(1400, 220 + 260 * len(self.files)), 680)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.only_diff = QCheckBox("다른 줄만 보기")
        self.only_diff.toggled.connect(self._filter)
        top.addWidget(self.only_diff)
        self.info = QLabel()
        self.info.setStyleSheet("color: gray;")
        top.addWidget(self.info, 1)
        v.addLayout(top)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.setColumnCount(len(self.files))
        self.table.setHorizontalHeaderLabels([f"{i + 1}. {f.get('title') or f.stem}" for i, f in enumerate(self.files)])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setIconSize(QSize(COVER_SIDE, COVER_SIDE))
        v.addWidget(self.table, 1)

        bb = QDialogButtonBox()
        copy = bb.addButton("고른 칸 복사", QDialogButtonBox.ActionRole)
        copy.clicked.connect(self._copy)
        bb.addButton("닫기", QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()

    def _fill(self):
        rows = rows_for(self.files)
        self.table.setRowCount(len(rows))
        self.table.setVerticalHeaderLabels([r[0] for r in rows])
        diff_bg = QBrush(QColor(255, 196, 0, 70))
        missing = QBrush(QColor(140, 140, 140))
        self.diff_rows = set()
        for r, (label, vals, is_info) in enumerate(rows):
            differs = len({x for x in vals}) > 1
            # 파일명·경로는 늘 다르니 색칠하지 않는다
            if differs and label not in ("파일명", "경로"):
                self.diff_rows.add(r)
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val if val else "—")
                it.setToolTip(val)
                if not val:
                    it.setForeground(missing)
                if r in self.diff_rows:
                    it.setBackground(diff_bg)
                if label == "커버" and self.files[c].pictures:
                    from .covermatch import thumb
                    it.setIcon(QIcon(thumb(data=self.files[c].cover.data, side=COVER_SIDE)))
                self.table.setItem(r, c, it)
        self.table.resizeRowsToContents()
        for r, (label, _, _) in enumerate(rows):
            if label == "커버":
                self.table.setRowHeight(r, max(self.table.rowHeight(r), COVER_SIDE + 8))
        n = len(self.diff_rows)
        more = f" · 처음 {MAX_FILES}곡만 보여 줍니다" if self.total > MAX_FILES else ""
        self.info.setText(f"값이 다른 줄 {n}개 (노란색){more}")
        self._filter()

    def _filter(self, *_):
        only = self.only_diff.isChecked()
        for r in range(self.table.rowCount()):
            label = self.table.verticalHeaderItem(r).text()
            keep = r in self.diff_rows or label in ("파일명",)
            self.table.setRowHidden(r, only and not keep)

    def _copy(self):
        items = self.table.selectedItems()
        if items:
            QGuiApplication.clipboard().setText("\n".join(i.toolTip() for i in items))
