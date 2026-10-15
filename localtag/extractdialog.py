"""앨범아트 추출 창 (앨범아트 추출기 앱 이식): 링크 · 내 보관함 · 목록의 곡 · YouTube."""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QSize, QStandardPaths, Qt, QThread, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import extract as ex
from .osutil import IS_MAC, REVEAL_LABEL, reveal

THUMB = 110
WORKERS = 6
MARK = {"done": "✅", "failed": "❌", "skipped": "➖", "duplicate": "🔁"}


def default_folder() -> str:
    """앨범아트 추출기 앱에서 쓰던 저장 폴더가 있으면 그대로, 없으면 사진 폴더 아래."""
    if IS_MAC:
        try:
            r = subprocess.run(["/usr/bin/defaults", "read", "local.albumart-extractor", "saveDir"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and os.path.isdir(r.stdout.strip()):
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation) or os.path.expanduser("~/Pictures")
    return os.path.join(pictures, "앨범아트")


class Job(QThread):
    """추출 작업. fn(job) 이 작업 스레드에서 돈다."""
    items = Signal(str, list)          # 묶음 제목, [이름]  — 목록을 새로 채운다
    item = Signal(int, str, QImage)    # 순번, 상태, 섬네일
    message = Signal(str)
    progress = Signal(int, int)
    finished_ok = Signal(str, str)     # 요약, 결과 폴더(또는 파일)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel = False

    def run(self):
        try:
            summary, out = self.fn(self)
        except Exception as e:  # 작업 스레드 밖으로 예외를 내보내지 않는다
            self.failed.emit(str(e))
        else:
            self.finished_ok.emit(summary, out or "")

    def thumb(self, data):
        img = QImage.fromData(data) if data else QImage()
        return img.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation) if not img.isNull() else QImage()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _summary(statuses, extra=""):
    ok = statuses.count("done")
    parts = [f"{ok}개 저장"]
    if statuses.count("failed"):
        parts.append(f"{statuses.count('failed')}개 실패")
    if statuses.count("skipped"):
        parts.append(f"앨범아트 없는 곡 {statuses.count('skipped')}개")
    if statuses.count("duplicate"):
        parts.append(f"같은 이미지 {statuses.count('duplicate')}개 건너뜀")
    return "완료 · " + " · ".join(parts) + extra


class ExtractDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.settings = main.settings
        self.setWindowTitle("앨범아트 추출")
        self.resize(900, 680)
        self.job = None
        self.lib_albums = []
        self.file_albums = []
        self.last_out = ""
        self._cancel_flag = False

        v = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._link_tab(), "링크로 추출")
        self.tabs.addTab(self._library_tab(), "내 보관함")
        self.tabs.addTab(self._files_tab(), "목록의 곡")
        self.tabs.addTab(self._youtube_tab(), "YouTube")
        if not IS_MAC:  # Music 보관함은 macOS 에서만 — 탭 번호는 그대로 두고 숨긴다
            self.tabs.setTabVisible(1, False)
        tab = self.settings.value("extract/tab", 0, int)
        self.tabs.setCurrentIndex(0 if tab == 1 and not IS_MAC else tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        v.addWidget(self.tabs, 2)

        # 공통: 저장 폴더 · 진행 · 결과
        row = QHBoxLayout()
        row.addWidget(QLabel("저장 폴더:"))
        self.folder = QLabel(self.settings.value("extract/dir", "", str) or default_folder())
        self.folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.folder.setStyleSheet("color: gray;")
        row.addWidget(self.folder, 1)
        b = QPushButton("변경…")
        b.clicked.connect(self._choose_folder)
        row.addWidget(b)
        v.addLayout(row)

        row = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        row.addWidget(self.title, 1)
        self.reveal_btn = QPushButton(REVEAL_LABEL)
        self.reveal_btn.clicked.connect(self._reveal)
        self.reveal_btn.setEnabled(False)
        row.addWidget(self.reveal_btn)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.cancel_btn)
        self.go_btn = QPushButton("추출하기")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self.start)
        row.addWidget(self.go_btn)
        v.addLayout(row)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        v.addWidget(self.bar)
        self.results = QListWidget()
        self.results.setViewMode(QListWidget.IconMode)
        self.results.setIconSize(QSize(THUMB, THUMB))
        self.results.setGridSize(QSize(THUMB + 26, THUMB + 44))
        self.results.setResizeMode(QListWidget.Adjust)
        self.results.setMovement(QListWidget.Static)
        self.results.setWordWrap(True)
        self.results.setSelectionMode(QAbstractItemView.NoSelection)
        v.addWidget(self.results, 3)
        self._placeholder = QPixmap(THUMB, THUMB)
        self._placeholder.fill(Qt.lightGray)
        self._tab_changed(self.tabs.currentIndex())

    # ------------------------------------------------------------ 탭
    def _check(self, text, key, default):
        cb = QCheckBox(text)
        cb.setChecked(self.settings.value(key, default, bool))
        cb.toggled.connect(lambda on: self.settings.setValue(key, on))
        return cb

    def _combo(self, items, key, default):
        cb = QComboBox()
        for label, value in items:
            cb.addItem(label, value)
        i = cb.findData(self.settings.value(key, default, int))
        cb.setCurrentIndex(max(0, i))
        cb.currentIndexChanged.connect(lambda _: self.settings.setValue(key, cb.currentData()))
        return cb

    def _link_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("Apple Music 앨범 또는 플레이리스트 링크를 붙여넣으세요")
        self.link.returnPressed.connect(self.start)
        row.addWidget(self.link, 1)
        b = QPushButton("붙여넣기")
        b.clicked.connect(lambda: self.link.setText(QApplication.clipboard().text().strip()))
        row.addWidget(b)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("크기:"))
        self.size = self._combo([(f"{s}px", s) for s in ex.LINK_SIZES], "extract/size", 3000)
        row.addWidget(self.size)
        self.link_unique = self._check("중복 이미지 제외", "extract/unique", False)
        row.addWidget(self.link_unique)
        row.addStretch(1)
        v.addLayout(row)
        note = QLabel("곡 순서대로 '번호 곡명.jpg' 로 저장합니다. 원본이 고른 크기보다 작으면 원본 크기로 받습니다.")
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        v.addStretch(1)
        return w

    def _library_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        b = QPushButton("보관함 불러오기")
        b.clicked.connect(self.load_library)
        row.addWidget(b)
        self.lib_search = QLineEdit()
        self.lib_search.setPlaceholderText("앨범·아티스트 찾기")
        self.lib_search.textChanged.connect(self._filter_library)
        row.addWidget(self.lib_search, 1)
        b = QPushButton("보이는 앨범 모두 선택")
        b.clicked.connect(lambda: self.lib_table.selectAll())
        row.addWidget(b)
        b = QPushButton("선택 해제")
        b.clicked.connect(lambda: self.lib_table.clearSelection())
        row.addWidget(b)
        v.addLayout(row)
        self.lib_table = self._album_table()
        v.addWidget(self.lib_table, 1)
        row = QHBoxLayout()
        self.lib_unique = self._check("중복 이미지 제외", "extract/unique", False)
        row.addWidget(self.lib_unique)
        self.lib_note = QLabel("Music 앱 보관함의 앨범을 골라, Music이 가진 앨범아트를 곡 순서대로 저장합니다.")
        self.lib_note.setStyleSheet("color: gray;")
        row.addWidget(self.lib_note, 1)
        v.addLayout(row)
        return w

    def _files_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.files_note = QLabel()
        row.addWidget(self.files_note, 1)
        b = QPushButton("목록에서 다시 가져오기")
        b.clicked.connect(self.load_files)
        row.addWidget(b)
        v.addLayout(row)
        self.files_table = self._album_table(cover_col=True)
        v.addWidget(self.files_table, 1)
        row = QHBoxLayout()
        self.files_unique = self._check("중복 이미지 제외", "extract/unique", False)
        row.addWidget(self.files_unique)
        note = QLabel("LocalTag 목록에서 선택한 곡(선택이 없으면 목록 전체)의 파일 속 앨범아트를 앨범별로 저장합니다. "
                      "곡 파일을 이 창이나 목록에 끌어다 놓아도 됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        row.addWidget(note, 1)
        v.addLayout(row)
        w.setAcceptDrops(True)
        return w

    def _youtube_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.yt = QLineEdit()
        self.yt.setPlaceholderText("YouTube 영상 또는 재생목록 링크를 붙여넣으세요")
        self.yt.returnPressed.connect(self.start)
        self.yt.textChanged.connect(self._yt_changed)
        row.addWidget(self.yt, 1)
        b = QPushButton("붙여넣기")
        b.clicked.connect(lambda: self.yt.setText(QApplication.clipboard().text().strip()))
        row.addWidget(b)
        v.addLayout(row)
        row = QHBoxLayout()
        self.yt_which = QComboBox()
        self.yt_which.addItems(["이 영상만", "재생목록 전체"])
        self.yt_which_label = QLabel("영상과 재생목록이 함께 든 링크:")
        row.addWidget(self.yt_which_label)
        row.addWidget(self.yt_which)
        row.addStretch(1)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("화질:"))
        self.yt_quality = self._combo([(label, i) for i, (_, label) in enumerate(ex.YT_QUALITIES)], "extract/yt_quality", 0)
        row.addWidget(self.yt_quality)
        self.yt_crop = self._check("가운데 기준 정사각형으로 자르기", "extract/yt_crop", True)
        row.addWidget(self.yt_crop)
        self.yt_side = self._combo([(f"{s}×{s}", s) for s in ex.YT_SIDES], "extract/yt_side", 720)
        self.yt_crop.toggled.connect(self.yt_side.setEnabled)
        self.yt_side.setEnabled(self.yt_crop.isChecked())
        row.addWidget(self.yt_side)
        row.addStretch(1)
        v.addLayout(row)
        note = QLabel("없는 화질이면 한 단계씩 낮춰 받습니다. 자르기는 위아래 검은 띠를 걷어낸 뒤 가운데를 정사각형으로 자릅니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        v.addStretch(1)
        self._yt_changed("")
        return w

    def _album_table(self, cover_col=False):
        t = QTableWidget(0, 4 if cover_col else 3)
        t.setHorizontalHeaderLabels(["아티스트", "앨범", "곡 수"] + (["커버 있는 곡"] if cover_col else []))
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(22)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.setWordWrap(False)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        t.setColumnWidth(0, 220)
        return t

    def _fill(self, table, albums, cover_col=False):
        table.setRowCount(len(albums))
        for r, a in enumerate(albums):
            cells = [a.artist, a.title, str(len(a.items))]
            if cover_col:
                cells.append(str(sum(1 for it in a.items if it.data)))
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 2:
                    item.setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
                table.setItem(r, c, item)

    def _tab_changed(self, i):
        self.settings.setValue("extract/tab", i)
        if i == 2:
            self.load_files()

    def _yt_changed(self, text):
        vid, lst = ex.parse_youtube(text)
        both = bool(vid and lst)
        self.yt_which.setVisible(both)
        self.yt_which_label.setVisible(both)

    # ------------------------------------------------------------ 목록 불러오기
    def load_library(self):
        if self.job:
            return

        def fn(job):
            job.message.emit("Music 보관함을 읽는 중…")
            return ex.library_albums(), None
        self._run(fn, on_done=self._library_loaded)

    def _library_loaded(self, albums):
        self.lib_albums = albums
        self._filter_library()
        n = sum(len(a.items) for a in albums)
        self.msg.setText(f"앨범 {len(albums)}개 · 곡 {n}개")

    def _filter_library(self, *_):
        q = self.lib_search.text().strip().casefold()
        self._lib_shown = [a for a in self.lib_albums if not q or q in a.title.casefold() or q in a.artist.casefold()]
        self._fill(self.lib_table, self._lib_shown)

    def load_files(self):
        files = self.main.selected_files() or self.main.visible_files()
        self.file_albums = ex.file_albums(files)
        self._fill(self.files_table, self.file_albums, cover_col=True)
        self.files_table.selectAll()
        which = "선택한 곡" if self.main.selected_files() else "목록 전체"
        self.files_note.setText(f"{which} {len(files)}곡 · 앨범 {len(self.file_albums)}개 — 저장할 앨범을 고르세요")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if self.tabs.currentIndex() == 2 and md.hasUrls() and all(u.isLocalFile() for u in md.urls()):
            self.main.load_paths([u.toLocalFile() for u in md.urls()], replace=False)
            self.load_files()
            return
        text = md.urls()[0].toString() if md.hasUrls() else md.text()
        if self.tabs.currentIndex() == 3:
            self.yt.setText(text.strip())
        else:
            self.tabs.setCurrentIndex(0)
            self.link.setText(text.strip())

    # ------------------------------------------------------------ 추출
    def _base(self):
        return self.folder.text()

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더", self._base())
        if d:
            self.folder.setText(d)
            self.settings.setValue("extract/dir", d)

    def start(self):
        if self.job:
            return
        i = self.tabs.currentIndex()
        base = self._base()
        if i == 0:
            text = self.link.text().strip()
            if not text:
                return
            fn = self._link_job(text, base, self.size.currentData(), self.link_unique.isChecked())
        elif i == 1:
            rows = sorted({ix.row() for ix in self.lib_table.selectionModel().selectedRows()})
            albums = [self._lib_shown[r] for r in rows] if self.lib_albums else []
            if not albums:
                self.msg.setText("보관함을 불러온 뒤 앨범을 골라 주세요." if not self.lib_albums else "저장할 앨범을 골라 주세요.")
                return
            fn = self._library_job(albums, base, self.lib_unique.isChecked())
        elif i == 2:
            rows = sorted({ix.row() for ix in self.files_table.selectionModel().selectedRows()})
            albums = [self.file_albums[r] for r in rows]
            if not albums:
                self.msg.setText("저장할 앨범을 골라 주세요. LocalTag 목록에 곡이 있어야 합니다.")
                return
            fn = self._files_job(albums, base, self.files_unique.isChecked())
        else:
            text = self.yt.text().strip()
            if not text:
                return
            fn = self._youtube_job(text, base, self.yt_which.currentIndex() == 1 or not self.yt_which.isVisible(),
                                   self.yt_quality.currentData(), self.yt_crop.isChecked(), self.yt_side.currentData())
        self._run(fn)

    def _download_all(self, job, items, fetch, path_for):
        """items 를 동시에 WORKERS 개씩 받아 저장한다. fetch(item) → bytes."""
        statuses = ["waiting"] * len(items)
        done = 0

        def one(i):
            it = items[i]
            if job.cancel:
                return i, "failed", None
            try:
                data = fetch(it)
            except Exception:
                return i, "failed", None
            if data is None:
                return i, "skipped", None
            try:
                _write(path_for(it, data), data)
            except OSError:
                return i, "failed", None
            return i, "done", data

        with ThreadPoolExecutor(WORKERS) as pool:
            for i, status, data in pool.map(one, range(len(items))):
                statuses[i] = status
                done += 1
                job.item.emit(i, status, job.thumb(data))
                job.progress.emit(done, len(items))
        return statuses

    def _link_job(self, text, base, size, unique):
        def fn(job):
            job.message.emit("정보를 불러오는 중…")
            a = ex.link_album(text)
            if unique:
                seen, kept = set(), []
                for it in a.items:
                    key = ex.hi_res(it.url, size) if it.url else None
                    if key is None or key not in seen:
                        seen.add(key)
                        kept.append(it)
                a.items = kept
            folder = os.path.join(base, a.folder)
            job.items.emit(f"{a.artist} - {a.title}" if a.artist else a.title, [f"{it.num} {it.name}" for it in a.items])
            job.message.emit(f"{len(a.items)}개 받는 중…")
            st = self._download_all(job, a.items, lambda it: ex.download_item(it, size) if it.url else None,
                                    lambda it, d: os.path.join(folder, it.filename("jpg")))
            return _summary(st), folder
        return fn

    def _youtube_job(self, text, base, whole, quality, crop, side):
        def fn(job):
            job.message.emit("정보를 불러오는 중…")
            a, single = ex.youtube_items(text, whole)
            folder = base if single else os.path.join(base, ex.safe_name(a.title))
            job.items.emit(a.title, [f"{it.num} {it.name}".strip() for it in a.items])
            job.message.emit("섬네일 받는 중…" if single else f"재생목록 · {len(a.items)}개 받는 중…")

            def fetch(it):
                d = ex.youtube_thumbnail(it.url, quality)
                return ex.center_square(d, side) if crop else d
            st = self._download_all(job, a.items, fetch, lambda it, d: os.path.join(folder, it.filename("jpg")))
            out = os.path.join(folder, a.items[0].filename("jpg")) if single else folder
            return _summary(st, " (삭제·비공개 영상 등)" if st.count("failed") else ""), out
        return fn

    def _save_albums(self, job, albums, base, unique, get_datas):
        """앨범마다 폴더를 만들어 곡 순서대로 저장. get_datas(album) → [bytes|None]."""
        names = [f"{a.title} · {it.num} {it.name}" for a in albums for it in a.items]
        job.items.emit(f"앨범 {len(albums)}개", names)
        statuses, k, first = [], 0, None
        for ai, a in enumerate(albums):
            if job.cancel:
                break
            job.message.emit(f"({ai + 1}/{len(albums)}) {a.artist} - {a.title}")
            folder = os.path.join(base, a.folder)
            first = first or folder
            seen = []
            for it, data in zip(a.items, get_datas(a)):
                status = "skipped"
                if data:
                    if unique and any(d is data or d == data for d in seen):
                        status = "duplicate"
                    else:
                        seen.append(data)
                        try:
                            _write(os.path.join(folder, it.filename(ex.image_ext(data))), data)
                            status = "done"
                        except OSError:
                            status = "failed"
                statuses.append(status)
                job.item.emit(k, status, job.thumb(data))
                k += 1
                job.progress.emit(k, len(names))
        out = first if len(albums) == 1 else base
        return _summary(statuses, f" · 앨범 {len(albums)}개"), out

    def _library_job(self, albums, base, unique):
        def fn(job):
            return self._save_albums(job, albums, base, unique, lambda a: ex.library_artworks(
                [it.pid for it in a.items], cancelled=lambda: job.cancel))
        return fn

    def _files_job(self, albums, base, unique):
        def fn(job):
            return self._save_albums(job, albums, base, unique, lambda a: [it.data for it in a.items])
        return fn

    # ------------------------------------------------------------ 작업 실행
    def _run(self, fn, on_done=None):
        """fn(job) → (요약, 결과 경로). on_done 이 있으면 fn 의 첫 값을 그 함수에 넘긴다 (목록 불러오기)."""
        result = {}
        if on_done:
            def work(job):
                result["value"], _ = fn(job)
                return "", None
        else:
            work = fn
        self._cancel_flag = False
        self.job = job = Job(work, self)
        self.go_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.tabs.setEnabled(False)
        self.reveal_btn.setEnabled(False)
        self.bar.setRange(0, 0)
        self.bar.show()
        job.message.connect(self.msg.setText)
        job.items.connect(self._show_items)
        job.item.connect(self._set_item)
        job.progress.connect(lambda n, total: (self.bar.setRange(0, total), self.bar.setValue(n)))
        job.finished_ok.connect(lambda summary, out: self._finish(
            summary, out, (lambda: on_done(result.get("value"))) if on_done else None))
        job.failed.connect(self._fail)
        job.start()

    def _show_items(self, title, names):
        self.title.setText(title)
        self.results.clear()
        icon = QIcon(self._placeholder)
        for n in names:
            it = QListWidgetItem(icon, n)
            it.setToolTip(n)
            self.results.addItem(it)

    def _set_item(self, i, status, img):
        it = self.results.item(i)
        if it is None:
            return
        if not img.isNull():
            it.setIcon(QIcon(QPixmap.fromImage(img)))
        name = it.toolTip()
        it.setText(f"{MARK.get(status, '')} {name}")

    def _end(self):
        self.job = None
        self.go_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.tabs.setEnabled(True)
        self.bar.hide()

    def _finish(self, summary, out, then=None):
        self._end()
        if then:
            then()
            return
        self.msg.setText(summary + (" (취소함)" if self._cancel_flag else ""))
        self.last_out = out
        self.reveal_btn.setEnabled(bool(out) and os.path.exists(out))

    def _fail(self, msg):
        self._end()
        self.msg.setText(f"<span style='color:#c82828'>⚠ {msg}</span>")

    def _cancel(self):
        if self.job:
            self.job.cancel = True
            self._cancel_flag = True
            self.msg.setText("취소하는 중…")

    def _reveal(self):
        if self.last_out:
            reveal(self.last_out)

    def closeEvent(self, e):
        if self.job:
            self.job.cancel = True
            self.job.wait(5000)
        super().closeEvent(e)
