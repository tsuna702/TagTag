"""왼쪽 태그 편집 패널 (Mp3tag의 Tag Panel)."""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .model import fmt_size
from .tagio import FIELD_LABELS, FIELDS, SEP, Picture, guess_mime

KEEP = "< 유지 >"
BLANK = "< 비우기 >"
COVER_SIZE = 220


def image_bytes_from_qimage(img: QImage) -> tuple[bytes, str]:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    if img.hasAlphaChannel():
        img.save(buf, "PNG")
        return bytes(ba), "image/png"
    img.save(buf, "JPEG", 92)
    return bytes(ba), "image/jpeg"


def picture_from_file(path: str) -> Picture:
    with open(path, "rb") as f:
        data = f.read()
    mime = guess_mime(data)
    if mime not in ("image/jpeg", "image/png"):
        # MP4 등은 JPEG/PNG만 지원하므로 변환
        img = QImage()
        if not img.loadFromData(data):
            raise ValueError("이미지를 읽을 수 없습니다.")
        data, mime = image_bytes_from_qimage(img)
    return Picture(data, mime, 3, "")


class CoverLabel(QLabel):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.setObjectName("cover")
        self.setStyleSheet("#cover { border: 1px solid palette(mid); background: palette(base); }")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            if url.isLocalFile():
                self.dropped.emit(url.toLocalFile())
                break


class TagPanel(QWidget):
    saveRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files = []
        self.initial: dict[str, str] = {}
        self.cover_op = None  # None | ("set", Picture) | ("remove",)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignRight)

        self.combos: dict[str, QComboBox] = {}
        for name in FIELDS + ["_filename"]:
            cb = QComboBox()
            cb.setEditable(True)
            cb.setInsertPolicy(QComboBox.NoInsert)
            cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            cb.setMinimumContentsLength(12)
            cb.lineEdit().returnPressed.connect(self.saveRequested)
            self.combos[name] = cb
        for name in FIELDS:
            form.addRow(FIELD_LABELS[name] + ":", self.combos[name])
        form.addRow("파일명:", self.combos["_filename"])
        layout.addLayout(form)

        # 커버
        layout.addSpacing(6)
        self.cover = CoverLabel()
        self.cover.dropped.connect(self._set_cover_from_file)
        self.cover.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cover.customContextMenuRequested.connect(self._cover_menu)
        layout.addWidget(self.cover, alignment=Qt.AlignHCenter)
        self.cover_info = QLabel()
        self.cover_info.setAlignment(Qt.AlignCenter)
        self.cover_info.setStyleSheet("color: gray;")
        layout.addWidget(self.cover_info)
        row = QHBoxLayout()
        self.btn_add = QPushButton("커버 추가…")
        self.btn_add.clicked.connect(self.choose_cover)
        self.btn_del = QPushButton("삭제")
        self.btn_del.clicked.connect(self.remove_cover)
        self.btn_export = QPushButton("내보내기…")
        self.btn_export.clicked.connect(self.export_cover)
        for b in (self.btn_add, self.btn_del, self.btn_export):
            row.addWidget(b)
        layout.addLayout(row)
        layout.addStretch(1)

        self.set_files([])

    # ------------------------------------------------------------ 상태 표시
    def set_files(self, files):
        self.files = list(files)
        self.cover_op = None
        self.initial = {}
        enabled = bool(self.files)
        for name, cb in self.combos.items():
            if name == "_filename":
                values = [f.stem for f in self.files]
            else:
                values = [f.get(name) for f in self.files]
            distinct = list(dict.fromkeys(values))
            cb.blockSignals(True)
            cb.clear()
            if enabled:
                cb.addItem(KEEP)
                if name != "_filename":
                    cb.addItem(BLANK)
                for v in distinct[:200]:
                    if v:
                        cb.addItem(v)
                text = distinct[0] if len(distinct) == 1 else KEEP
            else:
                text = ""
            cb.setEditText(text)
            cb.setEnabled(enabled)
            cb.blockSignals(False)
            self.initial[name] = text
        for b in (self.btn_add, self.btn_del, self.btn_export):
            b.setEnabled(enabled)
        self._show_cover()

    def _common_cover(self):
        """선택한 파일들의 대표 커버가 모두 같으면 (True, Picture|None), 다르면 (False, None)."""
        keys = set()
        first = None
        for f in self.files:
            c = f.cover
            keys.add(hashlib.md5(c.data).digest() if c else None)
            if len(keys) > 1:
                return False, None
            first = c
        return True, first

    def _show_cover(self):
        self.cover.setPixmap(QPixmap())
        if not self.files:
            self.cover.setText("")
            self.cover_info.setText("")
            return
        if self.cover_op and self.cover_op[0] == "remove":
            same, pic = True, None
        elif self.cover_op and self.cover_op[0] == "set":
            same, pic = True, self.cover_op[1]
        else:
            same, pic = self._common_cover()
        if not same:
            self.cover.setText(KEEP)
            self.cover_info.setText("파일마다 커버가 다릅니다")
            return
        if pic is None:
            self.cover.setText("커버 없음\n(이미지를 끌어다 놓으세요)")
            self.cover_info.setText("변경됨: 삭제" if self.cover_op else "")
            return
        pm = QPixmap()
        if pm.loadFromData(pic.data):
            self.cover.setPixmap(pm.scaled(COVER_SIZE - 2, COVER_SIZE - 2,
                                           Qt.KeepAspectRatio, Qt.SmoothTransformation))
            fmt = pic.mime.split("/")[-1].upper()
            info = f"{pm.width()}×{pm.height()}  {fmt}  {fmt_size(len(pic.data))}"
        else:
            self.cover.setText("(표시할 수 없는 이미지)")
            info = fmt_size(len(pic.data))
        if self.cover_op:
            info = "변경됨 · " + info
        self.cover_info.setText(info)

    # ------------------------------------------------------------ 커버 조작
    def _cover_menu(self, pos):
        if not self.files:
            return
        m = QMenu(self)
        m.addAction("커버 추가…", self.choose_cover)
        m.addAction("클립보드에서 붙여넣기", self.paste_cover)
        m.addAction("클립보드로 복사", self.copy_cover)
        m.addSeparator()
        m.addAction("이미지 파일로 내보내기…", self.export_cover)
        m.addAction("커버 삭제", self.remove_cover)
        m.exec(self.cover.mapToGlobal(pos))

    def _displayed_picture(self):
        if self.cover_op:
            return self.cover_op[1] if self.cover_op[0] == "set" else None
        same, pic = self._common_cover()
        return pic if same else None

    def choose_cover(self):
        start = os.path.dirname(self.files[0].path) if self.files else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "커버 이미지 선택", start, "이미지 (*.jpg *.jpeg *.png *.gif *.bmp *.webp)")
        if path:
            self._set_cover_from_file(path)

    def _set_cover_from_file(self, path):
        if not self.files:
            return
        try:
            pic = picture_from_file(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "커버", str(e))
            return
        self.cover_op = ("set", pic)
        self._show_cover()

    def paste_cover(self):
        img = QGuiApplication.clipboard().image()
        if img.isNull():
            QMessageBox.information(self, "커버", "클립보드에 이미지가 없습니다.")
            return
        data, mime = image_bytes_from_qimage(img)
        self.cover_op = ("set", Picture(data, mime, 3, ""))
        self._show_cover()

    def copy_cover(self):
        pic = self._displayed_picture()
        if pic:
            img = QImage()
            img.loadFromData(pic.data)
            QGuiApplication.clipboard().setImage(img)

    def remove_cover(self):
        if self.files:
            self.cover_op = ("remove",)
            self._show_cover()

    def export_cover(self):
        pic = self._displayed_picture()
        if pic is None:
            QMessageBox.information(self, "커버", "내보낼 커버가 없습니다.")
            return
        ext = ".png" if pic.mime == "image/png" else ".jpg"
        start = os.path.join(os.path.dirname(self.files[0].path), "cover" + ext)
        path, _ = QFileDialog.getSaveFileName(self, "커버 내보내기", start, f"이미지 (*{ext})")
        if path:
            with open(path, "wb") as f:
                f.write(pic.data)

    # ------------------------------------------------------------ 변경 내용 수집
    def is_dirty(self) -> bool:
        if not self.files:
            return False
        if self.cover_op is not None:
            return True
        return any(cb.currentText() != self.initial.get(n) for n, cb in self.combos.items())

    def collect(self):
        """(태그 변경 dict, 커버 작업, 새 파일명 또는 None)"""
        changes = {}
        for name in FIELDS:
            text = self.combos[name].currentText()
            if text == self.initial.get(name) or text == KEEP:
                continue
            if text == BLANK or text.strip() == "":
                changes[name] = []
            else:
                changes[name] = text.split(SEP)
        new_stem = None
        text = self.combos["_filename"].currentText()
        if text != self.initial.get("_filename") and text != KEEP:
            new_stem = text
        return changes, self.cover_op, new_stem
