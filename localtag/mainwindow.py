"""메인 창."""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QEvent, QItemSelectionModel, QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHeaderView, QLabel, QMainWindow,
    QMenu, QMessageBox, QProgressDialog, QScrollArea, QSplitter, QTableView,
)

from .converters import CONVERTERS
from .model import COLUMN_WIDTHS, COLUMNS, FileModel, SortProxy, fmt_length, fmt_size
from .panel import TagPanel
from .tagio import AUDIO_EXTS, AudioFile, Options, UnsupportedFile, scan

APP_NAME = "LocalTag"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.opts = Options(
            id3_v23=self.settings.value("id3_v23", True, bool),
            write_id3v1=self.settings.value("write_id3v1", False, bool),
            cp949_fallback=self.settings.value("cp949_fallback", True, bool),
            keep_mtime=self.settings.value("keep_mtime", False, bool),
        )
        self.recursive = self.settings.value("recursive", True, bool)
        self.last_dir = self.settings.value("last_dir", os.path.expanduser("~/Music"), str)

        # ---- 파일 목록
        self.model = FileModel(self)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        hh.setSectionsMovable(True)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        hh.setDefaultSectionSize(130)
        for i, col in enumerate(COLUMNS):
            hh.setSectionHidden(i, not col[4])
            hh.resizeSection(i, COLUMN_WIDTHS.get(col[0], 120))
        state = self.settings.value("header_state")
        if state is not None:
            hh.restoreState(state)
        else:
            self.table.sortByColumn(COLUMNS.index(next(c for c in COLUMNS if c[0] == "path")), Qt.AscendingOrder)
        self.table.selectionModel().selectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(lambda _: self.play())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.viewport().setAcceptDrops(True)
        self.table.viewport().installEventFilter(self)

        # ---- 태그 패널
        self.panel = TagPanel()
        self.panel.saveRequested.connect(self.save)
        scroll = QScrollArea()
        scroll.setWidget(self.panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(280)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.table)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 900])
        sp = self.settings.value("splitter")
        if sp is not None:
            self.splitter.restoreState(sp)
        self.setCentralWidget(self.splitter)

        self.status = QLabel()
        self.statusBar().addPermanentWidget(self.status)
        self.setAcceptDrops(True)

        self._build_menus()
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 760)
        self._update_status()

    # ------------------------------------------------------------ 메뉴
    def _act(self, menu, text, slot, shortcut=None, checkable=False, checked=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
            a.toggled.connect(slot)
        else:
            a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("파일")
        self._act(m, "폴더 열기…", self.open_folder, "Ctrl+O")
        self._act(m, "폴더 추가…", self.add_folder, "Ctrl+Shift+O")
        self._act(m, "파일 추가…", self.add_files_dialog, "Ctrl+Alt+O")
        m.addSeparator()
        self._act(m, "하위 폴더 포함", self._set_recursive, checkable=True, checked=self.recursive)
        m.addSeparator()
        self._act(m, "저장", lambda: self.save(), QKeySequence.Save)
        self._act(m, "목록 새로 고침", self.refresh, "F5")
        self._act(m, "목록 비우기", self.clear_list)
        m.addSeparator()
        quit_act = self._act(m, "종료", self.close, QKeySequence.Quit)
        quit_act.setMenuRole(QAction.QuitRole)

        m = mb.addMenu("편집")
        self._act(m, "모두 선택", self.table.selectAll, QKeySequence.SelectAll)
        self._act(m, "선택 반전", self.invert_selection, "Ctrl+I")
        m.addSeparator()
        for a in (self._act(m, "목록에서 제거", self.remove_from_list, "Backspace"),
                  self._act(m, "태그 제거…", self.remove_tags, "Ctrl+Shift+Backspace")):
            # 태그 입력 중에 Backspace가 가로채이지 않도록 목록에서만 동작
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.table.addAction(a)
        m.addSeparator()
        self._act(m, "재생", self.play, "Ctrl+P")
        self._act(m, "Finder에서 보기", self.reveal, "Ctrl+Shift+R")

        m = mb.addMenu("변환")
        for text, key, cls in CONVERTERS:
            self._act(m, text, lambda _=False, cls=cls: self.run_converter(cls), key)

        m = mb.addMenu("설정")
        sub = m.addMenu("ID3 버전")
        grp = QActionGroup(self)
        a23 = self._act(sub, "ID3v2.3 (UTF-16) — 호환성 좋음", lambda on: on and self._set_opt("id3_v23", True),
                        checkable=True, checked=self.opts.id3_v23)
        a24 = self._act(sub, "ID3v2.4 (UTF-8)", lambda on: on and self._set_opt("id3_v23", False),
                        checkable=True, checked=not self.opts.id3_v23)
        grp.addAction(a23)
        grp.addAction(a24)
        self._act(m, "ID3v1 태그도 함께 쓰기", lambda on: self._set_opt("write_id3v1", on),
                  checkable=True, checked=self.opts.write_id3v1)
        self._act(m, "옛 한글 태그(CP949) 자동 인식", self._set_cp949,
                  checkable=True, checked=self.opts.cp949_fallback)
        self._act(m, "저장할 때 파일 수정 시각 유지", lambda on: self._set_opt("keep_mtime", on),
                  checkable=True, checked=self.opts.keep_mtime)

    def _set_opt(self, name, value):
        setattr(self.opts, name, value)
        self.settings.setValue(name, value)

    def _set_cp949(self, on):
        self._set_opt("cp949_fallback", on)
        self.refresh()

    def _set_recursive(self, on):
        self.recursive = on
        self.settings.setValue("recursive", on)

    def _header_menu(self, pos):
        hh = self.table.horizontalHeader()
        m = QMenu(self)
        for i, col in enumerate(COLUMNS):
            a = m.addAction(col[1])
            a.setCheckable(True)
            a.setChecked(not hh.isSectionHidden(i))
            a.toggled.connect(lambda on, i=i: hh.setSectionHidden(i, not on))
        m.exec(hh.mapToGlobal(pos))

    def _table_menu(self, pos):
        if not self.selected_files():
            return
        m = QMenu(self)
        m.addAction("재생", self.play)
        m.addAction("Finder에서 보기", self.reveal)
        m.addSeparator()
        conv = m.addMenu("변환")
        for text, _, cls in CONVERTERS:
            conv.addAction(text, lambda cls=cls: self.run_converter(cls))
        m.addSeparator()
        m.addAction("목록에서 제거", self.remove_from_list)
        m.addAction("태그 제거…", self.remove_tags)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ 선택
    def selected_files(self) -> list[AudioFile]:
        # 화면에 보이는 순서대로 (텍스트→태그, %_counter% 등이 이 순서를 따른다)
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        return [self.model.files[self.proxy.mapToSource(self.proxy.index(r, 0)).row()] for r in rows]

    def visible_files(self) -> list[AudioFile]:
        return [self.model.files[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _on_selection(self, *_):
        self._confirm_pending()
        self.panel.set_files(self.selected_files())
        self._update_status()

    def invert_selection(self):
        sm = self.table.selectionModel()
        for r in range(self.proxy.rowCount()):
            sm.select(self.proxy.index(r, 0), QItemSelectionModel.Toggle | QItemSelectionModel.Rows)

    def _confirm_pending(self) -> bool:
        """패널에 저장하지 않은 변경이 있으면 물어본다. False면 취소."""
        if not self.panel.is_dirty():
            return True
        r = QMessageBox.question(
            self, APP_NAME, "저장하지 않은 태그 변경이 있습니다. 저장할까요?",
            QMessageBox.Save | QMessageBox.Discard, QMessageBox.Save)
        if r == QMessageBox.Save:
            self.save(refresh_panel=False)
        return True

    def _update_status(self):
        sel = self.selected_files()
        files = sel or self.model.files
        total = sum(f.length for f in files)
        size = sum(f.size for f in files)
        prefix = f"{len(sel)}개 선택 / 전체 {len(self.model.files)}개" if sel else f"전체 {len(self.model.files)}개"
        self.status.setText(f"{prefix}  |  {fmt_length(total)}  |  {fmt_size(size)}")

    # ------------------------------------------------------------ 불러오기
    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "폴더 열기", self.last_dir)
        if d:
            self.load_paths([d], replace=True)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "폴더 추가", self.last_dir)
        if d:
            self.load_paths([d], replace=False)

    def add_files_dialog(self):
        exts = " ".join("*" + e for e in sorted(AUDIO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(self, "파일 추가", self.last_dir, f"오디오 파일 ({exts})")
        if paths:
            self.load_paths(paths, replace=False)

    def load_paths(self, paths, replace: bool):
        self._confirm_pending()
        first = paths[0]
        self.last_dir = first if os.path.isdir(first) else os.path.dirname(first)
        self.settings.setValue("last_dir", self.last_dir)

        found = scan(paths, self.recursive)
        loaded, failed = self._load_files(found, "파일을 읽는 중…")
        if loaded is None:
            return
        if replace:
            self.model.set_files(loaded)
            self.setWindowTitle(f"{APP_NAME} — {os.path.basename(self.last_dir) or self.last_dir}")
        else:
            self.model.add_files(loaded)
        self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "읽지 못한 파일")
        self.statusBar().showMessage(f"{len(loaded)}개 파일을 불러왔습니다.", 5000)

    def _load_files(self, paths, label):
        loaded, failed = [], []
        prog = None
        if len(paths) > 30:
            prog = QProgressDialog(label, "취소", 0, len(paths), self)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(300)
        for i, p in enumerate(paths):
            if prog:
                if prog.wasCanceled():
                    break
                prog.setValue(i)
            try:
                loaded.append(AudioFile.load(p, self.opts))
            except (UnsupportedFile, OSError) as e:
                failed.append((p, str(e)))
        if prog:
            prog.close()
        return loaded, failed

    def refresh(self):
        self._confirm_pending()
        paths = [f.path for f in self.model.files if os.path.exists(f.path)]
        selected = {f.path for f in self.selected_files()}
        loaded, failed = self._load_files(paths, "다시 읽는 중…")
        self.model.set_files(loaded)
        sm = self.table.selectionModel()
        for r, f in enumerate(self.model.files):
            if f.path in selected:
                sm.select(self.proxy.mapFromSource(self.model.index(r, 0)),
                          QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "읽지 못한 파일")

    def clear_list(self):
        self._confirm_pending()
        self.model.set_files([])
        self.panel.set_files([])
        self._update_status()

    def _report_failures(self, failed, title):
        if not failed:
            return
        lines = "\n".join(f"{os.path.basename(p)}: {e}" for p, e in failed[:30])
        more = f"\n…외 {len(failed) - 30}개" if len(failed) > 30 else ""
        QMessageBox.warning(self, title, f"{len(failed)}개 파일을 처리하지 못했습니다.\n\n{lines}{more}")

    # ------------------------------------------------------------ 저장
    def save(self, refresh_panel=True):
        files = self.panel.files
        if not files or not self.panel.is_dirty():
            return
        changes, cover_op, new_stem = self.panel.collect()
        if new_stem is not None and len(files) > 1:
            QMessageBox.warning(self, "파일명",
                                "여러 파일을 선택했을 때는 파일명을 한꺼번에 바꿀 수 없습니다.\n"
                                "변환 메뉴의 '태그 → 파일명'을 쓰세요.")
            return

        failed = []
        prog = None
        if len(files) > 10:
            prog = QProgressDialog("저장하는 중…", None, 0, len(files), self)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(300)
        for i, f in enumerate(files):
            if prog:
                prog.setValue(i)
            pics = None
            if cover_op:
                pics = list(f.pictures)
                idx = f.primary_index()
                if cover_op[0] == "set":
                    if idx >= 0:
                        pics[idx] = cover_op[1]
                    else:
                        pics.insert(0, cover_op[1])
                elif idx >= 0:
                    del pics[idx]
            try:
                new = f.save(changes, pics, self.opts) if (changes or pics is not None) else f
                if new_stem is not None:
                    new.rename(new_stem)
                    new = AudioFile.load(new.path, self.opts)
            except Exception as e:
                failed.append((f.path, str(e)))
                continue
            self.model.replace(f, new)
        if prog:
            prog.close()
        if refresh_panel:
            self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "저장하지 못한 파일")
        ok = len(files) - len(failed)
        self.statusBar().showMessage(f"{ok}개 파일을 저장했습니다.", 5000)

    # ------------------------------------------------------------ 변환기
    def run_converter(self, cls):
        self._confirm_pending()
        files = self.selected_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "변환할 파일을 먼저 선택하세요.")
            return
        dlg = cls(self, files, self.settings)
        if dlg.exec():
            self.apply_plans(dlg.result_plans())

    def apply_plans(self, plans):
        failed = []
        prog = None
        if len(plans) > 10:
            prog = QProgressDialog("적용하는 중…", None, 0, len(plans), self)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(300)
        for i, p in enumerate(plans):
            if prog:
                prog.setValue(i)
            f = p.file
            try:
                new = f.save(p.changes, None, self.opts) if p.changes else f
                if p.new_path:
                    new.move_to(p.new_path)
                    new = AudioFile.load(new.path, self.opts)
            except Exception as e:
                failed.append((f.path, str(e)))
                # 이름 바꾸기 도중 실패해도 목록은 실제 파일 상태에 맞춘다
                try:
                    self.model.replace(f, AudioFile.load(f.path, self.opts))
                except Exception:
                    pass
                continue
            self.model.replace(f, new)
        if prog:
            prog.close()
        self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "처리하지 못한 파일")
        self.statusBar().showMessage(f"{len(plans) - len(failed)}개 파일에 적용했습니다.", 5000)

    def remove_tags(self):
        files = self.selected_files()
        if not files:
            return
        r = QMessageBox.warning(
            self, "태그 제거", f"선택한 {len(files)}개 파일의 태그와 커버를 모두 지웁니다.\n"
            "되돌릴 수 없습니다. 계속할까요?", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r != QMessageBox.Yes:
            return
        failed = []
        for f in files:
            try:
                self.model.replace(f, f.remove_tags(self.opts))
            except Exception as e:
                failed.append((f.path, str(e)))
        self.panel.set_files(self.selected_files())
        self._report_failures(failed, "태그를 지우지 못한 파일")

    # ------------------------------------------------------------ 기타 동작
    def remove_from_list(self):
        files = self.selected_files()
        if files:
            self.panel.set_files([])
            self.model.remove(files)
            self._update_status()

    def play(self):
        files = self.selected_files()
        if files:
            QDesktopServices.openUrl(QUrl.fromLocalFile(files[0].path))

    def reveal(self):
        files = self.selected_files()
        if files:
            subprocess.run(["open", "-R", files[0].path], check=False)

    # ------------------------------------------------------------ 드래그 앤 드롭
    def _drop_paths(self, e):
        return [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = self._drop_paths(e)
        if paths:
            self.load_paths(paths, replace=False)

    def eventFilter(self, obj, e):
        if obj is self.table.viewport():
            if e.type() in (QEvent.DragEnter, QEvent.DragMove) and e.mimeData().hasUrls():
                e.acceptProposedAction()
                return True
            if e.type() == QEvent.Drop:
                paths = self._drop_paths(e)
                if paths:
                    self.load_paths(paths, replace=False)
                return True
        return super().eventFilter(obj, e)

    # ------------------------------------------------------------ 종료
    def closeEvent(self, e):
        self._confirm_pending()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("header_state", self.table.horizontalHeader().saveState())
        self.settings.setValue("splitter", self.splitter.saveState())
        super().closeEvent(e)


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    w.show()
    paths = [p for p in argv[1:] if os.path.exists(p)]
    if paths:
        w.load_paths(paths, replace=True)
    return app.exec()
