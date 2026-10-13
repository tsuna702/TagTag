"""앨범아트 찾기·가져오기·크기 조정 창."""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from . import artwork as art
from .model import fmt_size

PREVIEW = 300
THUMB = 110


# ------------------------------------------------------------------ 백그라운드 작업

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # 네트워크·이미지 오류를 화면에 보여 준다
            self.signals.failed.emit(str(e))
        else:
            self.signals.done.emit(result)


class AsyncRunner(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self._alive = set()
        self.closed = False

    def run(self, fn, on_done, on_fail=None):
        job = _Job(fn)
        self._alive.add(job.signals)

        def finish(handler, value):
            self._alive.discard(job.signals)
            if not self.closed and handler:
                handler(value)
        job.signals.done.connect(lambda v: finish(on_done, v))
        job.signals.failed.connect(lambda m: finish(on_fail, m))
        self.pool.start(job)


# ------------------------------------------------------------------ 창

class PreviewLabel(QLabel):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFixedSize(PREVIEW, PREVIEW)
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setObjectName("preview")
        self.setStyleSheet("#preview { border: 1px solid palette(mid); background: palette(base); }")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.isLocalFile():
                self.dropped.emit(u.toLocalFile())
                return


class ArtworkDialog(QDialog):
    """선택한 파일에 넣을 커버를 고른다. 확인을 누르면 self.picture 에 결과가 들어 있다."""

    def __init__(self, parent, files, settings, recent_dir):
        super().__init__(parent)
        self.files, self.settings = files, settings
        self.recent = art.RecentImages(recent_dir)
        self.runner = AsyncRunner(self)
        self.source_data = None      # 고른 원본 이미지
        self.source_desc = ""
        self.picture = None          # 크기 조정까지 마친 결과
        self.setWindowTitle(f"앨범아트 — 파일 {len(files)}개")
        self.resize(980, 640)

        root = QHBoxLayout(self)
        # ---- 왼쪽: 가져올 곳
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_search(), "Apple Music 검색")
        self.tabs.addTab(self._build_address(), "YouTube · 주소")
        self.tabs.addTab(self._build_file(), "파일")
        self.tabs.addTab(self._build_recent(), "최근")
        self.tabs.setCurrentIndex(settings.value("art/tab", 0, int))
        self.tabs.currentChanged.connect(lambda i: settings.setValue("art/tab", i))
        root.addWidget(self.tabs, 1)

        # ---- 오른쪽: 미리보기와 크기
        right = QVBoxLayout()
        self.preview = PreviewLabel()
        self.preview.setText("왼쪽에서 이미지를 고르거나\n여기에 끌어다 놓으세요")
        self.preview.dropped.connect(self.load_file)
        right.addWidget(self.preview, alignment=Qt.AlignHCenter)
        self.info = QLabel()
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setWordWrap(True)
        right.addWidget(self.info)
        form = QFormLayout()
        self.size = QComboBox()
        for s in art.SIZES:
            self.size.addItem("원본 그대로" if s == 0 else f"{s} × {s}", s)
        self.size.setCurrentIndex(max(0, self.size.findData(settings.value("art/size", 500, int))))
        self.fit = QComboBox()
        for k, label in art.FITS.items():
            self.fit.addItem(label, k)
        self.fit.setCurrentIndex(max(0, self.fit.findData(settings.value("art/fit", "crop", str))))
        self.trim = QCheckBox("가장자리 검은 띠 없애기")
        self.trim.setChecked(settings.value("art/trim", True, bool))
        for w in (self.size, self.fit):
            w.currentIndexChanged.connect(self.update_preview)
        self.trim.toggled.connect(self.update_preview)
        form.addRow("크기:", self.size)
        form.addRow("맞춤:", self.fit)
        form.addRow("", self.trim)
        right.addLayout(form)
        right.addStretch(1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: gray;")
        right.addWidget(self.status)
        self.btn_apply = QPushButton(f"선택한 {len(files)}개 파일의 커버로 저장")
        self.btn_apply.setDefault(True)
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self.apply)
        self.btn_save = QPushButton("이미지 파일로 저장…")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_image)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        right.addWidget(self.btn_apply)
        right.addWidget(self.btn_save)
        right.addWidget(close)
        root.addLayout(right)

    # ------------------------------------------------------------ 탭
    def _build_search(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.query = QLineEdit(self._default_query())
        self.query.setPlaceholderText("앨범 · 아티스트 이름 또는 Apple Music 앨범 주소")
        self.query.returnPressed.connect(self.search)
        self.country = QComboBox()
        self.country.addItems(art.COUNTRIES)
        self.country.setCurrentText(self.settings.value("art/country", "KR", str))
        b = QPushButton("검색")
        b.clicked.connect(self.search)
        row.addWidget(self.query, 1)
        row.addWidget(self.country)
        row.addWidget(b)
        v.addLayout(row)
        self.results = QListWidget()
        self._icon_list(self.results)
        self.results.itemClicked.connect(self._pick_result)
        v.addWidget(self.results, 1)
        v.addWidget(QLabel("결과를 누르면 가장 큰 크기로 받아 옵니다. 검색할 때만 인터넷에 연결합니다."))
        return w

    def _build_address(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("YouTube 영상 주소, Apple Music 앨범 주소, 또는 https 이미지 주소:"))
        row = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.address.returnPressed.connect(self.load_address)
        paste = QPushButton("붙여넣기")
        paste.clicked.connect(lambda: self.address.setText(QGuiApplication.clipboard().text().strip()))
        go = QPushButton("가져오기")
        go.clicked.connect(self.load_address)
        row.addWidget(self.address, 1)
        row.addWidget(paste)
        row.addWidget(go)
        v.addLayout(row)
        note = QLabel("YouTube는 공개 영상의 썸네일(정지 이미지)만 가져오고 영상이나 음원은 받지 않습니다.\n"
                      "썸네일은 16:9이므로 '중앙 자르기'와 '검은 띠 없애기'를 함께 쓰면 정사각형 커버가 됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        v.addStretch(1)
        return w

    def _build_file(self):
        w = QWidget()
        v = QVBoxLayout(w)
        b = QPushButton("이미지 파일 선택…")
        b.clicked.connect(self.choose_file)
        v.addWidget(b)
        b2 = QPushButton("클립보드의 이미지 붙여넣기")
        b2.clicked.connect(self.paste_image)
        v.addWidget(b2)
        b3 = QPushButton("선택한 파일의 현재 커버 쓰기 (크기만 바꿀 때)")
        b3.clicked.connect(self.use_current)
        v.addWidget(b3)
        v.addWidget(QLabel("오른쪽 미리보기 칸에 이미지를 끌어다 놓아도 됩니다."))
        v.addStretch(1)
        return w

    def _build_recent(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.recent_list = QListWidget()
        self._icon_list(self.recent_list)
        self.recent_list.itemClicked.connect(
            lambda it: self.load_file(it.data(Qt.UserRole), it.toolTip()))
        v.addWidget(self.recent_list, 1)
        self._reload_recent()
        return w

    def _icon_list(self, lw):
        lw.setViewMode(QListWidget.IconMode)
        lw.setIconSize(QSize(THUMB, THUMB))
        lw.setGridSize(QSize(THUMB + 46, THUMB + 54))
        lw.setResizeMode(QListWidget.Adjust)
        lw.setMovement(QListWidget.Static)
        lw.setWordWrap(True)

    def _default_query(self):
        def common(name):
            vals = {f.get(name) for f in self.files}
            return vals.pop() if len(vals) == 1 else ""
        artist = common("albumartist") or common("artist")
        return " ".join(x for x in (artist, common("album")) if x)

    def _reload_recent(self):
        self.recent_list.clear()
        for item in self.recent.items():
            pm = QPixmap(item["path"])
            it = QListWidgetItem(QIcon(pm.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)),
                                 f"{pm.width()}×{pm.height()}")
            it.setData(Qt.UserRole, item["path"])
            it.setToolTip(item.get("source", ""))
            self.recent_list.addItem(it)

    # ------------------------------------------------------------ 가져오기
    def _busy(self, text):
        self.status.setText(text)

    def _error(self, msg):
        self.status.setText("")
        QMessageBox.warning(self, "앨범아트", msg)

    def search(self):
        q = self.query.text().strip()
        if not q:
            return
        country = self.country.currentText()
        self.settings.setValue("art/country", country)
        self.results.clear()
        self._busy("검색하는 중…")
        self.runner.run(lambda: art.search_albums(q, country), self._show_results, self._error)

    def _show_results(self, results):
        self.status.setText(f"{len(results)}개 찾음" if results else "찾은 앨범이 없습니다. 검색어나 국가를 바꿔 보세요.")
        for r in results:
            it = QListWidgetItem(self._placeholder(),
                                 f"{r['album']}\n{r['artist']}" + (f" · {r['year']}" if r.get("year") else ""))
            it.setData(Qt.UserRole, r)
            it.setToolTip(f"{r['album']} — {r['artist']} ({r['source']})")
            self.results.addItem(it)
            url = r["image"]
            self.runner.run(lambda url=url: art.fetch(url, 2 * 1024 * 1024),
                            lambda data, it=it: self._set_thumb(it, data))

    def _placeholder(self):
        if not hasattr(self, "_blank"):
            pm = QPixmap(THUMB, THUMB)
            pm.fill(Qt.lightGray)
            self._blank = QIcon(pm)
        return self._blank

    def _set_thumb(self, item, data):
        pm = QPixmap()
        if pm.loadFromData(data):
            try:
                item.setIcon(QIcon(pm))
            except RuntimeError:  # 목록이 이미 지워짐
                pass

    def _pick_result(self, item):
        r = item.data(Qt.UserRole)
        self._busy(f"'{r['album']}' 커버를 큰 크기로 받는 중…")
        url = art.high_res(r["image"])
        src = f"{r['source']}: {r['album']} — {r['artist']}"
        self.runner.run(lambda: art.fetch(url, art.MAX_IMAGE_BYTES),
                        lambda data: self.set_source(data, src, remember=True), self._error)

    def load_address(self):
        url = self.address.text().strip()
        if not url:
            return
        self._busy("가져오는 중…")
        self.runner.run(lambda: art.download(url),
                        lambda res: self.set_source(res[0], res[1], remember=True), self._error)

    def choose_file(self):
        start = os.path.dirname(self.files[0].path)
        path, _ = QFileDialog.getOpenFileName(self, "이미지 선택", start,
                                              "이미지 (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.heic)")
        if path:
            self.load_file(path)

    def load_file(self, path, source=None):
        try:
            with open(path, "rb") as f:
                data = f.read(art.MAX_IMAGE_BYTES + 1)
        except OSError as e:
            self._error(str(e))
            return
        self.set_source(data, source or os.path.basename(path))

    def paste_image(self):
        img = QGuiApplication.clipboard().image()
        if img.isNull():
            self._error("클립보드에 이미지가 없습니다.")
            return
        from .panel import image_bytes_from_qimage
        data, _ = image_bytes_from_qimage(img)
        self.set_source(data, "클립보드", remember=True)

    def use_current(self):
        covers = [f.cover for f in self.files if f.cover]
        if not covers:
            self._error("선택한 파일에 커버가 없습니다.")
            return
        if len({c.data for c in covers}) > 1:
            self._error("선택한 파일들의 커버가 서로 다릅니다. 파일마다 크기만 바꾸려면 "
                        "액션 → 빠른 액션 → '커버 크기 조정'을 쓰세요.")
            return
        self.set_source(covers[0].data, "현재 커버")

    # ------------------------------------------------------------ 미리보기
    def set_source(self, data, desc, remember=False):
        try:
            art.load_image(data)
        except art.ArtworkError as e:
            self._error(str(e))
            return
        self.source_data, self.source_desc = data, desc
        if remember:
            self.recent.add(data, desc)
            self._reload_recent()
        self.status.setText(desc)
        self.update_preview()

    def update_preview(self, *_):
        s = self.settings
        s.setValue("art/size", self.size.currentData())
        s.setValue("art/fit", self.fit.currentData())
        s.setValue("art/trim", self.trim.isChecked())
        self.fit.setEnabled(self.size.currentData() != 0)
        if self.source_data is None:
            return
        try:
            self.picture = art.process(self.source_data, self.size.currentData(),
                                       self.fit.currentData(), self.trim.isChecked())
        except art.ArtworkError as e:
            self.picture = None
            self._error(str(e))
            return
        pm = QPixmap()
        pm.loadFromData(self.picture.data)
        self.preview.setPixmap(pm.scaled(PREVIEW - 2, PREVIEW - 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.info.setText(f"원본 {art.describe(self.source_data)} → 결과 {pm.width()}×{pm.height()} "
                          f"{self.picture.mime.split('/')[-1].upper()} {fmt_size(len(self.picture.data))}")
        self.btn_apply.setEnabled(True)
        self.btn_save.setEnabled(True)

    # ------------------------------------------------------------ 결과
    def apply(self):
        if self.picture is not None:
            self.accept()

    def save_image(self):
        if self.picture is None:
            return
        ext = ".png" if self.picture.mime == "image/png" else ".jpg"
        start = os.path.join(os.path.dirname(self.files[0].path), "cover" + ext)
        path, _ = QFileDialog.getSaveFileName(self, "이미지로 저장", start, f"이미지 (*{ext})")
        if path:
            with open(path, "wb") as f:
                f.write(self.picture.data)
            self.status.setText(f"저장했습니다: {os.path.basename(path)}")

    def done(self, r):
        self.runner.closed = True
        super().done(r)
