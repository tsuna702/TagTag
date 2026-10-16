"""파일 목록 표 모델."""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from .tagio import FIELD_LABELS, SORT_FIELDS, AudioFile

SORT_ROLE = Qt.UserRole + 1


def fmt_length(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def quality_label(f: AudioFile) -> str:
    """음질 한눈에 보기: 무손실 / 고음질(256kbps 이상) / 보통 / 낮음(192kbps 미만)."""
    if f.lossless:
        return "무손실"
    kbps = f.bitrate // 1000
    if not kbps:
        return ""
    return "고음질" if kbps >= 256 else "보통" if kbps >= 192 else "낮음"


def _num_key(s: str):
    m = re.match(r"\s*(\d+)", s)
    return (0, int(m.group(1)), s) if m else (1, 0, s.lower())


def _tag(name):
    return (lambda f: f.get(name), lambda f: f.get(name).lower())


def _num_tag(name):
    return (lambda f: f.get(name), lambda f: _num_key(f.get(name)))


# (키, 제목, 표시 함수, 정렬 키 함수, 기본 표시 여부)
COLUMNS = [
    ("filename", "파일명", lambda f: f.filename, lambda f: f.filename.lower(), True),
    ("folder", "경로", lambda f: f.folder, lambda f: f.folder.lower(), True),
    ("title", FIELD_LABELS["title"], *_tag("title"), True),
    ("artist", FIELD_LABELS["artist"], *_tag("artist"), True),
    ("album", FIELD_LABELS["album"], *_tag("album"), True),
    ("albumartist", FIELD_LABELS["albumartist"], *_tag("albumartist"), False),
    ("year", FIELD_LABELS["year"], *_num_tag("year"), True),
    ("track", FIELD_LABELS["track"], *_num_tag("track"), True),
    ("disc", FIELD_LABELS["disc"], *_num_tag("disc"), False),
    ("genre", FIELD_LABELS["genre"], *_tag("genre"), True),
    ("composer", FIELD_LABELS["composer"], *_tag("composer"), False),
    ("comment", FIELD_LABELS["comment"], *_tag("comment"), False),
    ("cover", "커버", lambda f: f"{len(f.pictures)}" if f.pictures else "", lambda f: len(f.pictures), False),
    ("length", "길이", lambda f: fmt_length(f.length), lambda f: f.length, True),
    ("bitrate", "비트레이트", lambda f: f"{f.bitrate // 1000} kbps" if f.bitrate else "", lambda f: f.bitrate, True),
    ("samplerate", "샘플레이트", lambda f: f"{f.sample_rate} Hz" if f.sample_rate else "", lambda f: f.sample_rate, False),
    ("codec", "형식", lambda f: f.ext.lstrip(".").upper(), lambda f: f.ext, True),
    ("tagtype", "태그", lambda f: f.tag_type, lambda f: f.tag_type, True),
    ("size", "크기", lambda f: fmt_size(f.size), lambda f: f.size, False),
    ("mtime", "수정한 날짜", lambda f: time.strftime("%Y-%m-%d %H:%M", time.localtime(f.mtime)), lambda f: f.mtime, False),
    ("path", "전체 경로", lambda f: f.path, lambda f: f.path.lower(), False),
    # 아래는 나중에 더한 열 — 저장해 둔 열 배치가 어긋나지 않게 늘 끝에 붙인다
    ("realcodec", "코덱", lambda f: f.codec, lambda f: f.codec, False),
    ("bits", "비트 깊이", lambda f: f"{f.bits} bit" if f.bits else "", lambda f: f.bits, False),
    ("quality", "음질", quality_label, lambda f: f.quality_key(), False),
    *[(k, FIELD_LABELS[k], *_tag(k), False) for k in SORT_FIELDS],
]
COLUMN_KEYS = [c[0] for c in COLUMNS]
COLUMN_WIDTHS = {
    "filename": 240, "folder": 200, "title": 200, "artist": 150, "album": 170,
    "albumartist": 150, "year": 55, "track": 50, "disc": 50, "genre": 100,
    "composer": 130, "comment": 160, "cover": 45, "length": 60, "bitrate": 80,
    "samplerate": 85, "codec": 55, "realcodec": 60, "bits": 65, "quality": 60, "tagtype": 120, "size": 75, "mtime": 120, "path": 300,
    "TITLESORT": 160, "ARTISTSORT": 140, "ALBUMSORT": 160, "ALBUMARTISTSORT": 140,
}
RIGHT_ALIGNED = {"length", "bitrate", "samplerate", "size", "cover", "bits"}


DISPLAY_ROLES = (Qt.DisplayRole, Qt.ToolTipRole)
# 목록 칸에서 바로 고칠 수 있는 열 (파일명은 확장자를 뺀 이름만 고친다)
EDITABLE = {"filename", "title", "artist", "album", "albumartist", "year", "track", "disc",
            "genre", "composer", "comment", *SORT_FIELDS}


class FileModel(QAbstractTableModel):
    """파일 목록. 정렬도 여기서 한다 (키를 한 번씩만 계산해 정렬하므로 수천 곡도 즉시 끝난다)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: list[AudioFile] = []
        self.sort_column = -1
        self.sort_order = Qt.AscendingOrder
        self._rows: dict[int, int] | None = None  # id(파일) → 행 (필요할 때 만든다)
        self.on_edit = None  # (파일, 열 키, 새 값) — 칸에서 고친 값을 실제로 저장하는 쪽 (MainWindow)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        # 표를 그릴 때 칸마다 여러 역할을 묻는다. 쓰지 않는 역할은 바로 돌려보낸다
        if not index.isValid():
            return None
        if role in DISPLAY_ROLES:
            return COLUMNS[index.column()][2](self.files[index.row()])
        if role == Qt.EditRole:
            key = COLUMNS[index.column()][0]
            f = self.files[index.row()]
            return f.stem if key == "filename" else f.get(key)
        if role == Qt.TextAlignmentRole:
            if COLUMNS[index.column()][0] in RIGHT_ALIGNED:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return None
        if role == SORT_ROLE:
            return COLUMNS[index.column()][3](self.files[index.row()])
        return None

    def flags(self, index):
        fl = super().flags(index)
        if index.isValid() and self.on_edit and COLUMNS[index.column()][0] in EDITABLE:
            fl |= Qt.ItemIsEditable
        return fl

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid() or not self.on_edit:
            return False
        key = COLUMNS[index.column()][0]
        f = self.files[index.row()]
        old = f.stem if key == "filename" else f.get(key)
        if key not in EDITABLE or str(value) == old:
            return False
        # 실제 저장은 MainWindow 가 한다 (안전 저장 · 실행 취소 · Music 반영). 저장되면 행이 새 파일로 바뀐다
        self.on_edit(f, key, str(value))
        return True

    def row_of(self, f: AudioFile) -> int:
        if self._rows is None:
            self._rows = {id(x): i for i, x in enumerate(self.files)}
        return self._rows[id(f)]

    # ---- 정렬
    def _sorted(self, files):
        if self.sort_column < 0:
            return list(files)
        key = COLUMNS[self.sort_column][3]
        # 값이 같으면 전체 경로 순 (내림차순이어도 같은 값끼리는 경로 오름차순 — 안정 정렬)
        out = sorted(files, key=lambda f: f.path.lower())
        try:
            out.sort(key=key, reverse=self.sort_order == Qt.DescendingOrder)
        except TypeError:
            out.sort(key=lambda f: str(key(f)), reverse=self.sort_order == Qt.DescendingOrder)
        return out

    def sort(self, column, order=Qt.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self._resort()

    def _resort(self):
        if self.sort_column < 0 or len(self.files) < 2:
            return
        self._reorder(self._sorted(self.files))

    def move(self, files, delta: int) -> bool:
        """files 를 한 칸 위(-1)·아래(+1)로 옮긴다. 정렬은 '직접 정한 순서'가 된다. 옮겼으면 True."""
        self.sort_column = -1
        ids = {id(f) for f in files}
        order = list(self.files)
        rows = range(len(order)) if delta < 0 else range(len(order) - 1, -1, -1)
        for i in rows:
            j = i + delta
            # 맨 끝에 닿은 곡이 있으면 그 곡을 넘어가지 않는다 (선택한 곡끼리의 순서는 그대로)
            if id(order[i]) in ids and 0 <= j < len(order) and id(order[j]) not in ids:
                order[i], order[j] = order[j], order[i]
        return self._reorder(order)

    def set_order(self, files) -> bool:
        """같은 파일들을 새 순서로 (직접 정한 순서가 된다)."""
        self.sort_column = -1
        return self._reorder(list(files))

    def arrange(self, files) -> bool:
        """files 가 차지하던 자리에 files 를 주어진 순서로 다시 놓는다 (다른 곡의 위치는 그대로)."""
        ids = {id(f) for f in files}
        slots = [i for i, f in enumerate(self.files) if id(f) in ids]
        if len(slots) != len(files):
            return False
        order = list(self.files)
        for i, f in zip(slots, files):
            order[i] = f
        self.sort_column = -1
        return self._reorder(order)

    def _reorder(self, new) -> bool:
        if all(a is b for a, b in zip(new, self.files)):
            return False
        self.layoutAboutToBeChanged.emit()
        old_list = self.persistentIndexList()
        row_of = {id(f): i for i, f in enumerate(new)}
        new_list = [self.index(row_of[id(self.files[i.row()])], i.column()) for i in old_list]
        self.files = new
        self._rows = None
        self.changePersistentIndexList(old_list, new_list)
        self.layoutChanged.emit()
        return True

    # ---- 조작
    def set_files(self, files):
        self.beginResetModel()
        self.files = self._sorted(files)
        self._rows = None
        self.endResetModel()

    def add_files(self, files):
        known = {f.path for f in self.files}
        new = [f for f in files if f.path not in known]
        if not new:
            return
        self.beginInsertRows(QModelIndex(), len(self.files), len(self.files) + len(new) - 1)
        self.files.extend(new)
        self._rows = None
        self.endInsertRows()
        self._resort()

    def replace(self, old: AudioFile, new: AudioFile):
        """파일 하나를 새로 읽은 것으로 바꾼다. 행 위치는 그대로 둔다 (편집한 줄이 다른 곳으로 튀지 않게)."""
        row = self.row_of(old)
        self.files[row] = new
        if new is not old:
            del self._rows[id(old)]
            self._rows[id(new)] = row
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def remove(self, files):
        drop = {id(f) for f in files}
        rows = [i for i, f in enumerate(self.files) if id(f) in drop]
        # 이어진 행은 한 번에 지운다 (뒤에서부터 지워야 앞쪽 행 번호가 그대로다)
        end = len(rows)
        while end:
            start = end - 1
            while start and rows[start - 1] == rows[start] - 1:
                start -= 1
            first, last = rows[start], rows[end - 1]
            self.beginRemoveRows(QModelIndex(), first, last)
            del self.files[first:last + 1]
            self._rows = None
            self.endRemoveRows()
            end = start


class SortProxy(QSortFilterProxyModel):
    """필터만 한다. 정렬은 원본 모델(FileModel.sort)에 맡겨 표의 행 순서 = 원본 순서가 되게 한다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_fn = None

    def setSourceModel(self, model):
        super().setSourceModel(model)
        self._src = model

    def sort(self, column, order=Qt.AscendingOrder):
        self._src.sort(column, order)

    def set_filter(self, fn):
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+
            self.beginFilterChange()
            self.filter_fn = fn
            self.endFilterChange()
        else:
            self.filter_fn = fn
            self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if self.filter_fn is None:
            return True
        return self.filter_fn(self._src.files[row])
