"""파일 목록 표 모델."""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from .tagio import FIELD_LABELS, AudioFile

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
]
COLUMN_KEYS = [c[0] for c in COLUMNS]
COLUMN_WIDTHS = {
    "filename": 240, "folder": 200, "title": 200, "artist": 150, "album": 170,
    "albumartist": 150, "year": 55, "track": 50, "disc": 50, "genre": 100,
    "composer": 130, "comment": 160, "cover": 45, "length": 60, "bitrate": 80,
    "samplerate": 85, "codec": 55, "tagtype": 120, "size": 75, "mtime": 120, "path": 300,
}
RIGHT_ALIGNED = {"length", "bitrate", "samplerate", "size", "cover"}


class FileModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: list[AudioFile] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        f = self.files[index.row()]
        col = COLUMNS[index.column()]
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return col[2](f)
        if role == SORT_ROLE:
            return col[3](f)
        if role == Qt.TextAlignmentRole and col[0] in RIGHT_ALIGNED:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    # ---- 조작
    def set_files(self, files):
        self.beginResetModel()
        self.files = list(files)
        self.endResetModel()

    def add_files(self, files):
        known = {f.path for f in self.files}
        new = [f for f in files if f.path not in known]
        if not new:
            return
        self.beginInsertRows(QModelIndex(), len(self.files), len(self.files) + len(new) - 1)
        self.files.extend(new)
        self.endInsertRows()

    def replace(self, old: AudioFile, new: AudioFile):
        row = next(i for i, f in enumerate(self.files) if f is old)
        self.files[row] = new
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def remove(self, files):
        drop = {id(f) for f in files}
        rows = sorted((i for i, f in enumerate(self.files) if id(f) in drop), reverse=True)
        for r in rows:
            self.beginRemoveRows(QModelIndex(), r, r)
            del self.files[r]
            self.endRemoveRows()


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.filter_fn = None

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
        return self.filter_fn(self.sourceModel().files[row])

    def lessThan(self, left, right):
        a = self.sourceModel().data(left, SORT_ROLE)
        b = self.sourceModel().data(right, SORT_ROLE)
        try:
            if a == b:
                # 값이 같으면 전체 경로 순으로
                fa = self.sourceModel().files[left.row()].path.lower()
                fb = self.sourceModel().files[right.row()].path.lower()
                return fa < fb
            return a < b
        except TypeError:
            return str(a) < str(b)
