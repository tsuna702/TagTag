"""커버 짝짓기: 곡마다 그림 파일을 직접 골라 커버로 넣는다.

JDownloader 등으로 받은 섬네일(곡명 (BQ).jpg …)을, 다른 곳에서 받은 고음질 곡에 넣을 때 쓴다.
왼쪽은 곡(지금 커버 → 새 커버), 오른쪽은 그림 폴더. 그림을 곡 위로 끌어다 놓거나 더블클릭하면 짝이 된다.
자동으로 짝짓지는 않는다 — 곡을 고르면 이름이 비슷한 그림을 앞에 보여 찾기만 돕는다.
"""
from __future__ import annotations

import difflib
import os
import re
import unicodedata

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QSize, Qt
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import artwork as art

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SMALL, BIG = 48, 112
SIZES = [0, 500, 600, 1000]
SIZE_LABELS = ["원본", "500×500", "600×600", "1000×1000"]


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"\((?:\d+kbit_\w+|bq|hq|mq|sd|maxres)\)", " ", s)   # JDownloader 꼬리말
    s = re.sub(r"\b(pitch|master|fixed|vocal)\b[\s\d-]*", " ", s)    # 고음질 파일 꼬리말
    return re.sub(r"[\W_]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    return max(len(ta & tb) / max(1, len(ta)), difflib.SequenceMatcher(None, a, b).ratio())


def thumb(path=None, data=None, side=BIG) -> QPixmap:
    if data is not None:
        ba = QByteArray(data)  # 읽는 동안 살아 있어야 한다
        buf = QBuffer(ba)
        buf.open(QIODevice.ReadOnly)
        reader = QImageReader(buf)
    else:
        reader = QImageReader(path)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(side, side, Qt.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        pm = QPixmap(side, side)
        pm.fill(Qt.lightGray)
        return pm
    return QPixmap.fromImage(img)


def track_name(f) -> str:
    return f.get("title").split("\\\\")[0] or f.stem


class CoverMatchDialog(QDialog):
    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.setWindowTitle("커버 짝짓기")
        self.resize(1180, 720)
        self.files = files
        self.settings = settings
        self.match: dict[int, str] = {}   # 곡 순번 → 그림 경로
        self._processed = {}
        self.paths: list[str] = []
        self.icons: dict[str, QIcon] = {}

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("그림 폴더:"))
        self.folder = QLabel()
        self.folder.setStyleSheet("color: gray;")
        top.addWidget(self.folder, 1)
        b = QPushButton("폴더 바꾸기…")
        b.clicked.connect(self.choose_folder)
        top.addWidget(b)
        top.addSpacing(16)
        top.addWidget(QLabel("크기:"))
        self.size = QComboBox()
        self.size.addItems(SIZE_LABELS)
        self.size.setCurrentIndex(self.settings.value("match/size", 3, int))
        top.addWidget(self.size)
        self.crop = QCheckBox("중앙 자르기")
        self.crop.setToolTip("켜면: 가운데를 정사각형으로 잘라 꽉 채움\n끄면: 그림 전체를 넣고 남는 위아래(또는 양옆)를 흰 여백으로")
        self.crop.setChecked(self.settings.value("match/crop", True, bool))
        top.addWidget(self.crop)
        self.trim = QCheckBox("검은 띠 빼기")
        self.trim.setToolTip("YouTube 섬네일 위아래·양옆의 검은 띠를 먼저 걷어냅니다")
        self.trim.setChecked(self.settings.value("match/trim", True, bool))
        top.addWidget(self.trim)
        self.size.currentIndexChanged.connect(self._options_changed)
        self.crop.toggled.connect(self._options_changed)
        self.trim.toggled.connect(self._options_changed)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        # ---- 왼쪽: 곡
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>곡</b> — 고른 뒤 오른쪽 그림을 더블클릭하거나 끌어다 놓기"))
        self.table = QTableWidget(len(files), 4)
        self.table.setHorizontalHeaderLabels(["지금", "곡", "새 커버", "그림 파일"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(SMALL + 8)
        self.table.setIconSize(QSize(SMALL, SMALL))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for c, wdt in ((0, SMALL + 12), (2, SMALL + 24)):
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            self.table.setColumnWidth(c, wdt)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(3, 170)
        for r, f in enumerate(files):
            cur = QTableWidgetItem()
            if f.cover:
                cur.setIcon(QIcon(thumb(data=f.cover.data, side=SMALL)))
            else:
                cur.setText("없음")
            self.table.setItem(r, 0, cur)
            self.table.setItem(r, 1, QTableWidgetItem(track_name(f)))
            self.table.setItem(r, 2, QTableWidgetItem("—"))
            self.table.setItem(r, 3, QTableWidgetItem(""))
        self.table.itemSelectionChanged.connect(self.resort)
        self.table.viewport().setAcceptDrops(True)
        self.table.viewport().installEventFilter(self)
        lv.addWidget(self.table, 1)
        row = QHBoxLayout()
        b = QPushButton("짝 지우기")
        b.setToolTip("선택한 곡의 짝을 지웁니다")
        b.clicked.connect(self.clear_selected)
        row.addWidget(b)
        row.addStretch(1)
        self.count = QLabel()
        row.addWidget(self.count)
        lv.addLayout(row)
        split.addWidget(left)

        # ---- 오른쪽: 그림
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>그림</b>"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("이름으로 찾기")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.resort)
        row.addWidget(self.search, 1)
        self.view = QComboBox()
        self.view.addItems(["그림으로 보기", "제목으로 보기"])
        row.addWidget(self.view)
        self.by_name = QCheckBox("고른 곡과 이름이 비슷한 순")
        self.by_name.setToolTip("끄면 폴더의 이름순으로 고정됩니다 (받은 순서대로 훑으며 넣을 때)")
        self.by_name.setChecked(self.settings.value("match/by_name", True, bool))
        self.by_name.toggled.connect(lambda on: (self.settings.setValue("match/by_name", on), self.resort()))
        row.addWidget(self.by_name)
        rv.addLayout(row)
        self.images = QListWidget()
        self.images.setResizeMode(QListWidget.Adjust)
        self.images.setMovement(QListWidget.Static)
        self.images.setWordWrap(True)
        self.images.setDragEnabled(True)
        self.images.setDragDropMode(QAbstractItemView.DragOnly)
        self.images.itemDoubleClicked.connect(self.assign)
        self.images.itemActivated.connect(self.assign)  # 제목 보기에서 Return 으로도
        rv.addWidget(self.images, 1)
        split.addWidget(right)
        split.setSizes([600, 580])
        v.addWidget(split, 1)

        row = QHBoxLayout()
        note = QLabel("적용하면 짝지은 곡에만 커버를 넣습니다 (안전 저장 · 백업 · 되돌리기 · Music 자동 반영 그대로).")
        note.setStyleSheet("color: gray;")
        row.addWidget(note, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        self.ok = bb.button(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        v.addLayout(row)

        self.view.setCurrentIndex(self.settings.value("match/view", 0, int))
        self.view.currentIndexChanged.connect(self.set_view)
        start = self.settings.value("match/dir", "", str)
        if not os.path.isdir(start):
            start = os.path.dirname(files[0].path) if files else os.path.expanduser("~/Downloads")
        self.load_folder(start)
        self.set_view(self.view.currentIndex())
        if files:
            self.table.selectRow(0)
        self._update_count()

    # ------------------------------------------------------------ 그림 폴더
    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "그림 폴더", self.folder.text())
        if d:
            self.load_folder(d)

    def load_folder(self, folder):
        self.folder.setText(folder)
        self.settings.setValue("match/dir", folder)
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        self.paths = sorted((os.path.join(folder, n) for n in names
                             if not n.startswith(".") and os.path.splitext(n)[1].lower() in IMG_EXTS),
                            key=lambda p: os.path.basename(p).casefold())
        self.icons = {p: QIcon(thumb(p)) for p in self.paths}
        self.resort()

    # ------------------------------------------------------------ 적용 결과 (고른 크기·자르기를 실제로 거친 그림)
    def _fit(self):
        return "crop" if self.crop.isChecked() else "contain"

    def processed(self, path):
        key = (path, self.size.currentIndex(), self._fit(), self.trim.isChecked())
        if key not in self._processed:
            with open(path, "rb") as f:
                data = f.read()
            self._processed[key] = art.process(data, SIZES[self.size.currentIndex()], self._fit(), self.trim.isChecked())
        return self._processed[key]

    def _options_changed(self, *_):
        self.settings.setValue("match/size", self.size.currentIndex())
        self.settings.setValue("match/crop", self.crop.isChecked())
        self.settings.setValue("match/trim", self.trim.isChecked())
        for r in list(self.match):
            self._show_match(r)

    def _show_match(self, r):
        cell, name = self.table.item(r, 2), self.table.item(r, 3)
        p = self.match.get(r)
        if p is None:
            cell.setIcon(QIcon())
            cell.setText("—")
            name.setText("")
            return
        try:
            cell.setIcon(QIcon(thumb(data=self.processed(p).data, side=SMALL)))
            cell.setText("")
            cell.setToolTip("")
        except (OSError, art.ArtworkError) as e:
            cell.setIcon(QIcon())
            cell.setText("⚠")
            cell.setToolTip(str(e))
        name.setText(os.path.basename(p))

    # ------------------------------------------------------------ 보기
    def set_view(self, i):
        """0: 그림(섬네일 격자), 1: 제목(파일 이름만 한 줄씩 — 받은 순서대로 훑어보며 넣기 좋다)"""
        self.settings.setValue("match/view", i)
        if i == 1:
            self.images.setViewMode(QListWidget.ListMode)
            self.images.setIconSize(QSize(0, 0))
            self.images.setGridSize(QSize())
            self.images.setSpacing(1)
            self.images.setAlternatingRowColors(True)
        else:
            self.images.setViewMode(QListWidget.IconMode)
            self.images.setIconSize(QSize(BIG, BIG))
            self.images.setGridSize(QSize(BIG + 40, BIG + 52))
            self.images.setSpacing(0)
            self.images.setAlternatingRowColors(False)
        self.resort()

    def _current(self):
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        return rows[0] if rows else None

    def resort(self, *_):
        r = self._current()
        q = norm(self.search.text())
        name = track_name(self.files[r]) if r is not None else ""
        paths = [p for p in self.paths if not q or q in norm(os.path.basename(p))]
        if self.by_name.isChecked() and name:
            paths.sort(key=lambda p: -similarity(name, os.path.splitext(os.path.basename(p))[0]))
        self.images.clear()
        used = set(self.match.values())
        titles = self.view.currentIndex() == 1
        for p in paths:
            text = ("✓ " if p in used else "") + os.path.basename(p)
            it = QListWidgetItem(text) if titles else QListWidgetItem(self.icons[p], text)
            it.setData(Qt.UserRole, p)
            it.setToolTip(os.path.basename(p))
            self.images.addItem(it)
        if not paths:
            it = QListWidgetItem("이 폴더에 그림(jpg·png·webp)이 없습니다." if not self.paths else "찾는 그림이 없습니다.")
            it.setFlags(Qt.NoItemFlags)
            self.images.addItem(it)

    # ------------------------------------------------------------ 짝짓기
    def assign(self, item, row=None):
        r = self._current() if row is None else row
        p = item.data(Qt.UserRole) if item else None
        if r is None or not p:
            return
        self.match[r] = p
        self._show_match(r)
        self._update_count()
        # 짝 없는 다음 곡으로
        order = list(range(r + 1, len(self.files))) + list(range(r))
        nxt = next((i for i in order if i not in self.match), None)
        if nxt is not None:
            self.table.selectRow(nxt)
            self.table.scrollToItem(self.table.item(nxt, 1))
        else:
            self.resort()

    def clear_selected(self):
        for ix in self.table.selectionModel().selectedRows():
            if self.match.pop(ix.row(), None) is not None:
                self._show_match(ix.row())
        self._update_count()
        self.resort()

    def eventFilter(self, obj, e):
        if obj is self.table.viewport() and e.type() in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            if e.source() is not self.images:
                e.ignore()
                return True
            row = self.table.rowAt(int(e.position().y()))
            item = self.images.currentItem()
            if row < 0 or item is None or not item.data(Qt.UserRole):
                e.ignore()
                return True
            e.acceptProposedAction()
            self.table.selectRow(row)  # 놓을 곡 줄을 강조
            if e.type() == QEvent.Drop:
                self.assign(item, row)
            return True
        return super().eventFilter(obj, e)

    def _update_count(self):
        self.count.setText(f"{len(self.match)}/{len(self.files)}곡 짝지음")
        self.ok.setText(f"적용 ({len(self.match)}곡)")
        self.ok.setEnabled(bool(self.match))

    # ------------------------------------------------------------ 결과
    def result_pictures(self):
        """[(파일, 새 커버 Picture)] — 짝지은 곡만. 그림을 읽지 못하면 ArtworkError."""
        out = []
        for r, p in sorted(self.match.items()):
            out.append((self.files[r], self.processed(p)))
        return out

    def accept(self):
        try:
            self._result = self.result_pictures()
        except (OSError, art.ArtworkError) as e:
            QMessageBox.warning(self, "커버 짝짓기", f"그림을 읽지 못했습니다.\n{e}")
            return
        super().accept()
