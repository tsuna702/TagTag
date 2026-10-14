"""확장 태그 편집기 (Mp3tag의 Extended Tags)."""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .model import fmt_size
from .panel import KEEP, picture_from_file
from .tagio import FIELD_LABELS, FIELDS, PICTURE_TYPES, SEP, Picture, field_key

# 기본 필드를 Mp3tag식 대문자 이름으로 보여 준다
DISPLAY_NAMES = {
    "title": "TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUMARTIST",
    "year": "YEAR", "track": "TRACK", "disc": "DISCNUMBER", "genre": "GENRE",
    "composer": "COMPOSER", "comment": "COMMENT",
}
COMMON_EXT = [
    "LYRICIST", "PUBLISHER", "BPM", "COPYRIGHT", "ENCODEDBY", "ORIGARTIST", "CONDUCTOR",
    "ISRC", "SUBTITLE", "GROUPING", "MOOD", "INITIALKEY", "LANGUAGE", "ORIGYEAR",
    "ALBUMSORT", "ARTISTSORT", "TITLESORT", "ALBUMARTISTSORT", "COMPOSERSORT",
    "COMPILATION", "UNSYNCEDLYRICS", "CATALOGNUMBER", "BARCODE", "LABEL",
]
THUMB = 96


def display_name(key: str) -> str:
    return DISPLAY_NAMES.get(key, key)


def _same_pics(a, b) -> bool:
    # 같은 앨범의 커버는 보통 같은 bytes 를 함께 쓰므로 대부분 내용을 비교하지 않고 끝난다
    return len(a) == len(b) and all(
        x.type == y.type and x.desc == y.desc and (x.data is y.data or x.data == y.data) for x, y in zip(a, b))


class ExtendedTagsDialog(QDialog):
    def __init__(self, parent, files):
        super().__init__(parent)
        self.files = files
        n = len(files)
        self.setWindowTitle(f"확장 태그 — {files[0].filename}" if n == 1 else f"확장 태그 — 파일 {n}개")
        self.resize(760, 640)

        # 필드: 기본 필드 순서 → 나머지는 이름순
        keys = set()
        for f in files:
            keys.update(f.tags)
        order = [k for k in FIELDS if k in keys] + sorted(k for k in keys if k not in FIELDS)
        self.initial = {}
        for k in order:
            vals = {f.get(k) for f in files}
            self.initial[k] = vals.pop() if len(vals) == 1 else KEEP
        self.values = dict(self.initial)

        v = QVBoxLayout(self)
        if n > 1:
            v.addWidget(QLabel("값이 파일마다 다른 필드는 < 유지 > 로 표시됩니다. 바꾼 필드만 모든 파일에 씁니다."))

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["필드", "값"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(lambda r, c: self.edit_value())
        v.addWidget(self.table, 3)

        row = QHBoxLayout()
        for text, slot in (("필드 추가…", self.add_field), ("값 편집…", self.edit_value),
                           ("필드 삭제", self.delete_field)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        # ---- 커버
        v.addWidget(QLabel("<b>커버</b>"))
        self.pics_differ = any(not _same_pics(files[0].pictures, f.pictures) for f in files[1:])
        self.pictures = [] if self.pics_differ else [Picture(p.data, p.mime, p.type, p.desc)
                                                     for p in files[0].pictures]
        self.pics_changed = False
        self.pic_note = QLabel()
        self.pic_note.setWordWrap(True)
        v.addWidget(self.pic_note)
        h = QHBoxLayout()
        self.pic_list = QListWidget()
        self.pic_list.setViewMode(QListWidget.IconMode)
        self.pic_list.setIconSize(QSize(THUMB, THUMB))
        self.pic_list.setGridSize(QSize(THUMB + 40, THUMB + 44))
        self.pic_list.setMovement(QListWidget.Static)
        self.pic_list.setResizeMode(QListWidget.Adjust)
        self.pic_list.setWordWrap(True)
        self.pic_list.setMinimumHeight(THUMB + 60)
        self.pic_list.currentRowChanged.connect(self._pic_selected)
        h.addWidget(self.pic_list, 1)
        side = QVBoxLayout()
        self.type_combo = QComboBox()
        for t, name in PICTURE_TYPES.items():
            self.type_combo.addItem(f"{t}: {name}", t)
        self.type_combo.currentIndexChanged.connect(self._pic_meta_changed)
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("설명 (선택)")
        self.desc_edit.textEdited.connect(self._pic_meta_changed)
        side.addWidget(QLabel("종류:"))
        side.addWidget(self.type_combo)
        side.addWidget(self.desc_edit)
        for text, slot in (("커버 추가…", self.add_picture), ("삭제", self.delete_picture),
                           ("내보내기…", self.export_picture)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            side.addWidget(b)
        side.addStretch(1)
        h.addLayout(side)
        v.addLayout(h, 2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("저장")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.reload_fields()
        self.reload_pictures()

    # ------------------------------------------------------------ 필드
    def reload_fields(self, select_key=None):
        self.table.setRowCount(0)
        # 원래 있던 필드는 지워도 '(삭제됨)'으로 보여 주고, 새로 추가했다가 비운 필드는 숨긴다
        self._keys = [k for k, val in self.values.items() if k in self.initial or val != ""]
        for r, k in enumerate(self._keys):
            self.table.insertRow(r)
            name = QTableWidgetItem(display_name(k))
            if k in FIELD_LABELS:
                name.setToolTip(FIELD_LABELS[k])
            val = self.values[k]
            shown = "(삭제됨)" if val == "" else val.replace("\n", " ⏎ ")
            item = QTableWidgetItem(shown)
            item.setToolTip(val)
            if val != self.initial.get(k):
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                name.setFont(f)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, item)
        if select_key in self._keys:
            self.table.selectRow(self._keys.index(select_key))

    def _current_key(self):
        r = self.table.currentRow()
        return self._keys[r] if 0 <= r < len(self._keys) else None

    def add_field(self):
        name, ok = QInputDialog.getItem(
            self, "필드 추가", "필드 이름 (목록에서 고르거나 직접 입력):",
            [display_name(k) for k in FIELDS] + COMMON_EXT, 0, True)
        if not ok or not name.strip():
            return
        key = field_key(name)
        self._edit_key(key, self.values.get(key, ""))

    def edit_value(self):
        key = self._current_key()
        if key:
            self._edit_key(key, self.values[key])

    def _edit_key(self, key, current):
        hint = "여러 값은 \\\\ 로 구분합니다."
        if current == KEEP:
            hint += "\n지금은 파일마다 값이 다릅니다. 입력한 값이 모든 파일에 들어갑니다."
            current = ""
        text, ok = QInputDialog.getMultiLineText(
            self, f"{display_name(key)} 편집", hint, current)
        if ok:
            self.values[key] = text
            self.reload_fields(key)

    def delete_field(self):
        key = self._current_key()
        if key:
            self.values[key] = ""
            self.reload_fields()

    # ------------------------------------------------------------ 커버
    def reload_pictures(self, select=None):
        self.pic_list.clear()
        if self.pics_differ and not self.pics_changed:
            self.pic_note.setText("파일마다 커버가 다릅니다. 여기서 커버를 추가하거나 바꾸면, "
                                  "선택한 모든 파일의 커버가 아래 목록으로 바뀝니다.")
        else:
            self.pic_note.setText("" if self.pictures else "커버가 없습니다.")
        for p in self.pictures:
            pm = QPixmap()
            pm.loadFromData(p.data)
            label = PICTURE_TYPES.get(p.type, str(p.type))
            info = f"{pm.width()}×{pm.height()}" if not pm.isNull() else ""
            it = QListWidgetItem(QIcon(pm.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
                                 f"{label}\n{info} {fmt_size(len(p.data))}")
            it.setToolTip(p.desc or label)
            self.pic_list.addItem(it)
        if self.pictures:
            self.pic_list.setCurrentRow(select if select is not None and select < len(self.pictures) else 0)
        else:
            self._pic_selected(-1)

    def _pic_selected(self, row):
        ok = 0 <= row < len(self.pictures)
        self.type_combo.setEnabled(ok)
        self.desc_edit.setEnabled(ok)
        if ok:
            p = self.pictures[row]
            self.type_combo.blockSignals(True)
            idx = self.type_combo.findData(p.type)
            self.type_combo.setCurrentIndex(max(idx, 0))
            self.type_combo.blockSignals(False)
            self.desc_edit.setText(p.desc)

    def _pic_meta_changed(self, *_):
        r = self.pic_list.currentRow()
        if 0 <= r < len(self.pictures):
            p = self.pictures[r]
            new_type, new_desc = self.type_combo.currentData(), self.desc_edit.text()
            if (p.type, p.desc) != (new_type, new_desc):
                p.type, p.desc = new_type, new_desc
                self.pics_changed = True
                if self.sender() is self.type_combo:
                    self.reload_pictures(r)

    def add_picture(self):
        start = os.path.dirname(self.files[0].path)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "커버 이미지", start, "이미지 (*.jpg *.jpeg *.png *.gif *.bmp *.webp)")
        for path in paths:
            try:
                pic = picture_from_file(path)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "커버", str(e))
                continue
            # 앞 표지가 이미 있으면 새 그림은 '기타'로
            if any(p.type == 3 for p in self.pictures):
                pic.type = 0
            self.pictures.append(pic)
            self.pics_changed = True
        self.reload_pictures(len(self.pictures) - 1)

    def delete_picture(self):
        r = self.pic_list.currentRow()
        if 0 <= r < len(self.pictures):
            del self.pictures[r]
            self.pics_changed = True
            self.reload_pictures(r)

    def export_picture(self):
        r = self.pic_list.currentRow()
        if not 0 <= r < len(self.pictures):
            return
        p = self.pictures[r]
        ext = ".png" if p.mime == "image/png" else ".jpg"
        start = os.path.join(os.path.dirname(self.files[0].path), "cover" + ext)
        path, _ = QFileDialog.getSaveFileName(self, "커버 내보내기", start, f"이미지 (*{ext})")
        if path:
            with open(path, "wb") as fh:
                fh.write(p.data)

    # ------------------------------------------------------------ 결과
    def result(self):
        """(태그 변경 dict, 새 커버 목록 또는 None)"""
        changes = {}
        for k, val in self.values.items():
            if val == self.initial.get(k) or val == KEEP:
                continue
            changes[k] = val.split(SEP) if val else []
        return changes, (self.pictures if self.pics_changed else None)
