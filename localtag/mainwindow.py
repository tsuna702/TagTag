"""메인 창."""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import QEvent, QItemSelectionModel, QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter, QTableView, QToolBar,
    QVBoxLayout, QWidget,
)

from .actiondialogs import ActionEditDialog, ActionsDialog, PlansPreview, choose_action_type
from . import music
from .artworkdialog import ArtworkDialog
from .tasks import run_task
from .backup import BackupStore
from .backupdialog import BackupDialog
from .actions import ACTION_TYPES, ActionStore, describe, new_action, run_actions
from .converters import CONVERTERS, AutoNumber
from .export import ExportDialog, PlaylistDialog
from .filetrack import find_moved
from .exttags import ExtendedTagsDialog
from .filterexpr import FilterError, compile_filter
from .model import COLUMN_WIDTHS, COLUMNS, FileModel, SortProxy, fmt_length, fmt_size
from .panel import TagPanel
from .plan import Plan, UndoEntry, UndoItem
from .tagio import (
    AUDIO_EXTS, AudioFile, Options, UnsupportedFile, move_file, restore_snapshot, scan, take_snapshot,
)

APP_NAME = "LocalTag"


def app_version() -> str:
    """앱 번들의 버전 (개발 중 실행이면 'dev'). 새 빌드마다 macOS 권한을 다시 묻기 때문에 버전별로 기록한다."""
    import sys
    if getattr(sys, "frozen", False):
        import plistlib
        try:
            info = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Info.plist")
            with open(info, "rb") as f:
                return str(plistlib.load(f).get("CFBundleVersion", "app"))
        except OSError:
            return "app"
    return "dev"
UNDO_LEVELS = 30
UNDO_MEMORY = 512 * 1024 * 1024  # 실행 취소 기록이 이보다 커지면 오래된 것부터 버린다


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
            safe_save=self.settings.value("safe_save", True, bool),
        )
        self.recursive = self.settings.value("recursive", True, bool)
        self.last_dir = self.settings.value("last_dir", os.path.expanduser("~/Music"), str)
        self.undo_stack: list[UndoEntry] = []
        data_dir = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        self.actions = ActionStore(os.path.join(data_dir, "actions.json"))
        self.backups = BackupStore(os.path.join(data_dir, "Backups"))
        self.backup_enabled = self.settings.value("backup_enabled", True, bool)
        try:
            self.backups.prune(self.settings.value("backup_days", 30, int),
                               self.settings.value("backup_max_gb", 5, int) * 1024 ** 3)
        except OSError:
            pass

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
        self.panel.artworkRequested.connect(self.find_artwork)
        self.panel.changed.connect(self._update_toolbar)
        scroll = QScrollArea()
        scroll.setWidget(self.panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(280)

        # ---- 필터
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setPlaceholderText(
            "필터 (⌘F) — 예: 아이유 · artist HAS 아이유 · year GREATER 2010 AND genre MISSING")
        self.filter_edit.textChanged.connect(lambda _: self.filter_timer.start())
        self.filter_edit.installEventFilter(self)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(250)
        self.filter_timer.timeout.connect(self.apply_filter)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        rv.addWidget(self.filter_edit)
        rv.addWidget(self.table)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(right)
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
        self._build_toolbar()
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
        self._act(m, "내보내기 (CSV·HTML·텍스트)…", self.export_list, "Ctrl+E")
        self._act(m, "재생목록 만들기…", self.make_playlist, "Ctrl+L")
        m.addSeparator()
        quit_act = self._act(m, "종료", self.close, QKeySequence.Quit)
        quit_act.setMenuRole(QAction.QuitRole)

        m = mb.addMenu("편집")
        self.undo_act = self._act(m, "실행 취소", self.undo, QKeySequence.Undo)
        self.undo_act.setEnabled(False)
        m.addSeparator()
        self._act(m, "확장 태그…", self.extended_tags, "Ctrl+T")
        m.addSeparator()
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
        m.addSeparator()
        self._act(m, "필터", self.focus_filter, QKeySequence.Find)

        m = mb.addMenu("변환")
        for text, key, cls in CONVERTERS:
            self._act(m, text, lambda _=False, cls=cls: self.run_converter(cls), key)

        m = mb.addMenu("커버")
        self._act(m, "앨범아트 찾기 · 가져오기…", self.find_artwork, "Ctrl+Shift+A")
        self._act(m, "커버 크기 조정…", self.resize_covers, "Ctrl+Shift+K")

        m = mb.addMenu("Music")
        self._act(m, "Apple Music에 반영 (새로 읽기)", self.music_refresh, "Ctrl+Shift+M")
        self._act(m, "저장한 뒤 자동으로 Music에 반영", lambda on: self.settings.setValue("music_auto", on),
                  checkable=True, checked=self.settings.value("music_auto", False, bool))
        m.addSeparator()
        self._act(m, "Music에서 선택한 곡 추가", self.music_add_selection, "Ctrl+Shift+I")
        m.addSeparator()
        self._act(m, "Music 권한 확인", self.music_check)
        self._act(m, "Music 권한 설정 열기…", music.open_privacy_settings)

        m = mb.addMenu("액션")
        self._act(m, "액션 그룹…", self.run_action_groups, "Alt+6")
        self._act(m, "빠른 액션…", self.quick_action, "Alt+7")
        m.addSeparator()
        self._act(m, "자동 번호 매기기…", lambda: self.run_converter(AutoNumber), "Ctrl+K")

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
        self._act(m, "안전하게 저장 (사본에 쓰고 오디오 검증 후 교체)", lambda on: self._set_opt("safe_save", on),
                  checkable=True, checked=self.opts.safe_save)
        m.addSeparator()
        self._act(m, "백업…", self.show_backups, "Ctrl+Shift+B")

    def _build_toolbar(self):
        tb = QToolBar("도구 막대")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, tb)

        def button(text, slot, tip):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)  # 입력칸에서 쓰던 포커스를 빼앗지 않도록
            b.clicked.connect(lambda _=False: slot())
            tb.addWidget(b)
            return b

        self.tb_save = button("저장", self.save, "바꾼 태그 저장 (⌘S)")
        self.tb_undo = button("되돌리기", self.undo, "마지막 작업 되돌리기 (⌘Z)")
        tb.addSeparator()
        self.tb_refresh = button("새로고침", self.refresh, "목록의 파일을 다시 읽기 (F5)")
        self.tb_clear = button("목록 지우기", self.clear_list, "목록 비우기 — 파일은 지우지 않습니다")
        tb.addSeparator()
        self.tb_finder = button("Finder에서 보기", self.reveal, "선택한 파일을 Finder에서 보기 (⇧⌘R)")
        self._update_toolbar()

    def _update_toolbar(self):
        if not hasattr(self, "tb_save"):
            return
        has_files = bool(self.model.files)
        self.tb_save.setEnabled(self.panel.is_dirty())
        self.tb_undo.setEnabled(bool(self.undo_stack))
        self.tb_undo.setToolTip(f"되돌리기: {self.undo_stack[-1].label} (⌘Z)" if self.undo_stack
                                else "되돌릴 작업이 없습니다")
        self.tb_refresh.setEnabled(has_files)
        self.tb_clear.setEnabled(has_files)
        self.tb_finder.setEnabled(bool(self.table.selectionModel().selectedRows()))

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
        m.addAction("확장 태그…", self.extended_tags)
        m.addAction("앨범아트 찾기…", self.find_artwork)
        m.addAction("Apple Music에 반영", self.music_refresh)
        m.addAction("재생목록 만들기…", self.make_playlist)
        m.addAction("내보내기…", self.export_list)
        m.addSeparator()
        conv = m.addMenu("변환")
        for text, _, cls in CONVERTERS:
            conv.addAction(text, lambda cls=cls: self.run_converter(cls))
        act = m.addMenu("액션")
        act.addAction("액션 그룹…", self.run_action_groups)
        act.addAction("빠른 액션…", self.quick_action)
        act.addAction("자동 번호 매기기…", lambda: self.run_converter(AutoNumber))
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
        shown = self.proxy.rowCount()
        files = sel or (self.visible_files() if self.proxy.filter_fn else self.model.files)
        total = sum(f.length for f in files)
        size = sum(f.size for f in files)
        count = f"표시 {shown} / 전체 {len(self.model.files)}개" if self.proxy.filter_fn else f"전체 {len(self.model.files)}개"
        prefix = f"{len(sel)}개 선택 · {count}" if sel else count
        self.status.setText(f"{prefix}  |  {fmt_length(total)}  |  {fmt_size(size)}")
        self._update_toolbar()

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

    def _find_moved(self, files):
        """사라진 파일을 같은 디스크에서 파일 번호(inode)로 찾는다 (Music이 폴더를 정리한 경우 등)."""
        return find_moved([(f.path, f.dev, f.ino) for f in files])

    def _relink_missing(self, files):
        """목록의 파일 중 옮겨진 것을 새 경로로 고친다. 고친 개수를 돌려준다."""
        moved = self._find_moved(files)
        for f in files:
            if f.path in moved:
                f.path = moved[f.path]
                try:
                    self.model.replace(f, f)  # 표의 경로 칸 갱신
                except StopIteration:
                    pass
        if moved:
            self.statusBar().showMessage(f"옮겨진 파일 {len(moved)}개를 새 위치에서 다시 찾았습니다.", 6000)
        return len(moved)

    def refresh(self):
        self._confirm_pending()
        self._relink_missing(self.model.files)
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
        plans = []
        for f in files:
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
            p = Plan(f, changes=dict(changes), pictures=pics)
            if new_stem is not None:
                try:
                    new_path = f.path_for_stem(new_stem)
                except ValueError as e:
                    QMessageBox.warning(self, "파일명", str(e))
                    return
                if new_path != f.path:
                    p.new_path = new_path
            if p.actionable:
                plans.append(p)
        self.apply_plans(plans, "태그 저장", refresh_panel=refresh_panel)

    def apply_plans(self, plans, label, refresh_panel=True):
        """작업 목록을 실제 파일에 적용하고 실행 취소 기록을 남긴다."""
        # Music이 그사이 파일을 옮겼을 수 있으므로 먼저 새 위치를 찾는다
        self._relink_missing([p.file for p in plans])
        if not plans:
            if refresh_panel:
                self.panel.set_files(self.selected_files())
            return
        entry = UndoEntry(label)
        job = None
        if self.backup_enabled:
            try:
                job = self.backups.begin(label)
            except OSError as e:
                QMessageBox.warning(self, "백업", f"백업 폴더를 만들지 못해 백업 없이 저장합니다.\n{e}")
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
            old_path = f.path
            snap = None
            bentry = None
            try:
                if job is not None and (p.touches_tags or p.new_path):
                    bentry = job.before(old_path, copy_file=p.touches_tags)
                if p.touches_tags:
                    snap = take_snapshot(old_path)
                new = f
                if p.remove_tags:
                    new = new.remove_tags(self.opts)
                if p.changes or p.pictures is not None:
                    new = new.save(p.changes, p.pictures, self.opts)
                cur_path = new.path
                if p.new_path:
                    move_file(cur_path, p.new_path)
                    cur_path = p.new_path
                entry.items.append(UndoItem(old_path, cur_path, snap))
                snap = None
                if bentry is not None:
                    job.after(bentry, cur_path)
                    bentry = None
                self.model.replace(f, AudioFile.load(cur_path, self.opts))
                for path, data in p.exports:
                    with open(path, "wb") as fh:
                        fh.write(data)
            except Exception as e:
                failed.append((old_path, str(e)))
                if bentry is not None:
                    job.failed(bentry, str(e))
                if snap is not None and os.path.exists(old_path):
                    # 일부만 바뀐 채로 남지 않게 되돌려 둔다
                    entry.items.append(UndoItem(old_path, old_path, snap))
                try:
                    self.model.replace(f, AudioFile.load(f.path, self.opts))
                except Exception:
                    pass
        if prog:
            prog.close()
        if job is not None:
            try:
                job.finish()
            except OSError:
                pass
        if entry.items:
            self._push_undo(entry)
            if self.settings.value("music_auto", False, bool):
                changed = [i.new_path for i in entry.items]
                self._music_refresh_paths(changed, modal=False)
        if refresh_panel:
            self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "처리하지 못한 파일")
        self.statusBar().showMessage(f"{label}: {len(plans) - len(failed)}개 파일에 적용했습니다.", 5000)

    # ------------------------------------------------------------ 백업
    def show_backups(self):
        self._confirm_pending()
        BackupDialog(self).exec()

    def after_backup_restore(self, done):
        """백업에서 되돌린 파일을 목록에 다시 읽어 들인다."""
        by_path = {f.path: f for f in self.model.files}
        for cur, orig in done:
            f = by_path.get(cur)
            if f is not None:
                try:
                    self.model.replace(f, AudioFile.load(orig, self.opts))
                except Exception:
                    pass
        # 파일이 메모리 속 실행 취소 기록과 달라졌으므로 기록을 비운다
        self.undo_stack.clear()
        self._update_undo_action()
        self.panel.set_files(self.selected_files())
        self._update_status()

    # ------------------------------------------------------------ 실행 취소
    def _push_undo(self, entry):
        self.undo_stack.append(entry)
        while len(self.undo_stack) > UNDO_LEVELS or (
                len(self.undo_stack) > 1 and sum(e.size_estimate() for e in self.undo_stack) > UNDO_MEMORY):
            self.undo_stack.pop(0)
        self._update_undo_action()

    def _update_undo_action(self):
        if self.undo_stack:
            self.undo_act.setText(f"실행 취소: {self.undo_stack[-1].label}")
            self.undo_act.setEnabled(True)
        else:
            self.undo_act.setText("실행 취소")
            self.undo_act.setEnabled(False)
        self._update_toolbar()

    def undo(self):
        if not self.undo_stack:
            return
        entry = self.undo_stack.pop()
        by_path = {f.path: f for f in self.model.files}
        failed = []
        for item in reversed(entry.items):
            try:
                if item.new_path != item.old_path:
                    move_file(item.new_path, item.old_path)
                if item.snapshot is not None:
                    restore_snapshot(item.old_path, item.snapshot)
                restored = AudioFile.load(item.old_path, self.opts)
                f = by_path.get(item.new_path)
                if f is not None:
                    self.model.replace(f, restored)
            except Exception as e:
                failed.append((item.new_path, str(e)))
        self._update_undo_action()
        self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "되돌리지 못한 파일")
        self.statusBar().showMessage(f"실행 취소: {entry.label}", 5000)

    # ------------------------------------------------------------ 변환기 · 액션
    def _files_for_command(self):
        self._confirm_pending()
        files = self.selected_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "파일을 먼저 선택하세요.")
        return files

    def run_converter(self, cls):
        files = self._files_for_command()
        if not files:
            return
        dlg = cls(self, files, self.settings)
        if dlg.exec():
            self.apply_plans(dlg.result_plans(), cls.title.replace("변환: ", ""))

    def _run_actions_with_preview(self, label, actions, files):
        dlg = PlansPreview(self, files, self.settings, f"액션: {label}",
                           lambda: run_actions(actions, files))
        if dlg.exec():
            self.apply_plans(dlg.result_plans(), label)

    def run_action_groups(self):
        files = self._files_for_command()
        if not files:
            return
        dlg = ActionsDialog(self, self.actions)
        if dlg.exec():
            label, actions = dlg.selected_actions()
            self._run_actions_with_preview(label, actions, files)

    def quick_action(self):
        files = self._files_for_command()
        if not files:
            return
        from PySide6.QtGui import QCursor
        kind = choose_action_type(self, QCursor.pos())
        if not kind:
            return
        last = self.settings.value(f"quick/{kind}")
        action = new_action(kind)
        if isinstance(last, dict):
            action.update(last)
        dlg = ActionEditDialog(self, action)
        if dlg.exec():
            a = dlg.result_action()
            self.settings.setValue(f"quick/{kind}", a)
            self._run_actions_with_preview(describe(a), [a], files)

    def remove_tags(self):
        files = self.selected_files()
        if not files:
            return
        r = QMessageBox.warning(
            self, "태그 제거", f"선택한 {len(files)}개 파일의 태그와 커버를 모두 지웁니다.\n"
            "편집 → 실행 취소로 되돌릴 수 있습니다. 계속할까요?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r != QMessageBox.Yes:
            return
        self.apply_plans([Plan(f, remove_tags=True) for f in files], "태그 제거")

    # ------------------------------------------------------------ Apple Music
    def music_refresh(self):
        files = self._files_for_command()
        if files:
            self._music_refresh_paths([f.path for f in files], modal=True)

    def _music_refresh_paths(self, paths, modal=True):
        if getattr(self, "_music_busy", False):
            self.statusBar().showMessage("Music 작업이 이미 진행 중입니다.", 4000)
            return
        self._music_busy = True
        # Music에 보여야 할 값 (제목·앨범) — 반영됐는지 확인하는 데 쓴다
        by_path = {f.path: f for f in self.model.files}
        expected = {p: (by_path[p].get("title").split("\\\\")[0], by_path[p].get("album").split("\\\\")[0])
                    for p in paths if p in by_path}

        def done(res):
            self._music_busy = False
            moved = self._relink(res.relocated)
            parts = [f"반영 확인됨 {res.confirmed}곡"]
            if res.unconfirmed:
                parts.append(f"Music에 아직 옛 정보가 보임 {len(res.unconfirmed)}곡")
            if res.notfound:
                parts.append(f"Music 보관함에 없음 {len(res.notfound)}곡")
            if res.failed:
                parts.append(f"실패 {len(res.failed)}곡")
            if moved:
                parts.append(f"옮겨진 파일 {moved}개 다시 연결")
            self.statusBar().showMessage("Music: " + " · ".join(parts), 10000)
            if not modal and not (res.unconfirmed or res.failed):
                return
            text = "\n".join(parts)
            if res.retried:
                text += f"\n\n(늦게 반영되는 곡이 있어 {res.retried}번 다시 시도했습니다)"
            details = []
            if res.unconfirmed:
                details.append("아직 옛 정보가 보이는 곡 — Music이 늦게 반영하는 중일 수 있습니다. "
                               "잠시 뒤 다시 'Music에 반영'을 눌러 보세요:")
                details += ["  " + os.path.basename(p) for p in res.unconfirmed[:10]]
            if res.notfound:
                details.append("Music 보관함에 없는 파일 (Music에 추가된 파일이 아니거나 다른 사본):")
                details += ["  " + os.path.basename(p) for p in res.notfound[:10]]
            if res.failed:
                details.append("실패:")
                details += [f"  {os.path.basename(p)}: {why}" for p, why in res.failed[:10]]
            box = QMessageBox(QMessageBox.Warning if (res.unconfirmed or res.failed) else QMessageBox.Information,
                              "Apple Music", text, QMessageBox.Ok, self)
            if details:
                box.setDetailedText("\n".join(details))
            box.exec()

        def fail(msg):
            self._music_busy = False
            if modal or "권한" in msg:
                self._music_error(msg)
            else:
                self.statusBar().showMessage(f"Music 자동 반영 실패: {msg}", 8000)

        entries = [(p, by_path[p].dev, by_path[p].ino) for p in paths if p in by_path]

        inode = {os.path.realpath(p): (dev, ino) for p, dev, ino in entries}

        def relocate(ps):
            # 처음 경로의 파일 번호로 지금 위치를 찾는다
            found = find_moved([(p, *inode.get(os.path.realpath(p), (0, 0))) for p in ps])
            return found

        def job(prog, cancelled):
            prog(0, len(paths), "옮겨진 파일이 있는지 확인하는 중")
            moved = find_moved(entries, cancelled)          # 반영 전에 이미 옮겨진 파일
            now = [moved.get(p, p) for p in paths]
            for p in paths:
                if p in moved:
                    inode[os.path.realpath(moved[p])] = inode.get(os.path.realpath(p), (0, 0))
            exp = {moved.get(p, p): v for p, v in expected.items()}
            res = music.refresh(now, prog, cancelled, exp, relocate=relocate)
            # music.refresh 의 키는 '반영 시작 때 경로'이므로 목록의 원래 경로로 되돌린다
            orig = {os.path.realpath(moved.get(p, p)): os.path.realpath(p) for p in paths}
            final = {}
            for p in paths:
                start = os.path.realpath(moved.get(p, p))
                end = res.relocated.get(start, moved.get(p))
                if end:
                    final[os.path.realpath(p)] = end
            res.relocated = final
            res.failed = [(orig.get(k, k), why) for k, why in res.failed]
            res.unconfirmed = [orig.get(k, k) for k in res.unconfirmed]
            res.notfound = [orig.get(k, k) for k in res.notfound]
            return res

        run_task(self, "Apple Music에 반영", job,
                 done, fail, modal=modal, status=lambda m: self.statusBar().showMessage(m, 3000))

    def _music_error(self, msg):
        box = QMessageBox(QMessageBox.Warning, "Apple Music", msg, QMessageBox.Ok, self)
        if "권한" in msg:
            b = box.addButton("권한 설정 열기", QMessageBox.ActionRole)
            box.exec()
            if box.clickedButton() is b:
                music.open_privacy_settings()
        else:
            box.exec()

    def _relink(self, relocated):
        """Music이 파일을 옮긴 경우 목록의 경로를 새 위치로 바꾼다."""
        by_path = {os.path.realpath(f.path): f for f in self.model.files}
        moved = 0
        for old, new in relocated.items():
            f = by_path.get(old)
            if f is None or os.path.realpath(new) == os.path.realpath(f.path) or not os.path.exists(new):
                continue
            try:
                self.model.replace(f, AudioFile.load(new, self.opts))
                moved += 1
            except Exception:
                pass
        if moved:
            self.panel.set_files(self.selected_files())
        return moved

    def music_check(self):
        run_task(self, "Music 권한 확인", lambda prog, cancelled: music.ensure_access(launch=True),
                 lambda _: QMessageBox.information(self, "Apple Music", "Music을 제어할 수 있습니다."),
                 self._music_error)

    def music_add_selection(self):
        run_task(self, "Music에서 선택한 곡 읽기", lambda prog, cancelled: music.selected_paths(),
                 lambda paths: self.load_paths(paths, replace=False), self._music_error)

    def _drop_from_music(self):
        try:
            paths, albums = music.read_music_drag()
        except Exception as e:
            QMessageBox.warning(self, "Apple Music", f"Music에서 끌어온 항목을 읽지 못했습니다.\n{e}")
            return
        if not albums:
            if paths:
                self.load_paths(paths, replace=False)
            else:
                QMessageBox.information(self, "Apple Music", "이 Mac에 파일이 있는 곡이 없습니다 (클라우드 곡일 수 있습니다).")
            return

        def done(res):
            found, errors = res
            if paths + found:
                self.load_paths(paths + found, replace=False)
            if errors:
                QMessageBox.information(self, "Apple Music", "\n".join(errors[:15]))
        run_task(self, "Music 앨범의 곡 찾기",
                 lambda prog, cancelled: music.album_paths(albums, cancelled), done, self._music_error)

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_drop_registered", False):
            self._drop_registered = music.register_drop_types(self)
        if not getattr(self, "_perm_scheduled", False):
            self._perm_scheduled = True
            QTimer.singleShot(700, self._maybe_ask_permissions)

    def _maybe_ask_permissions(self):
        """새 버전을 처음 켰을 때 Music 권한을 미리 받아 둔다 (작업 도중 권한 창이 뜨지 않게)."""
        if QApplication.platformName() != "cocoa" or os.environ.get("LOCALTAG_NO_PERMISSION_PROMPT"):
            return
        ver = app_version()
        if self.settings.value("perm/never", False, bool) or self.settings.value("perm/asked_version", "") == ver:
            return
        status = music.automation_status()
        if status == "granted":
            self.settings.setValue("perm/asked_version", ver)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Apple Music 권한")
        if status == "denied":
            box.setText("Music 제어 권한이 꺼져 있습니다.")
            box.setInformativeText("고친 곡을 Apple Music에 반영하려면 시스템 설정 → 개인정보 보호 및 보안 → "
                                   "자동화에서 LocalTag 아래의 'Music'을 켜 주세요.")
            settings_btn = box.addButton("설정 열기", QMessageBox.AcceptRole)
            box.addButton("나중에", QMessageBox.RejectRole)
            never = box.addButton("다시 묻지 않기", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is settings_btn:
                music.open_privacy_settings()
                self.settings.setValue("perm/asked_version", ver)
            elif box.clickedButton() is never:
                self.settings.setValue("perm/never", True)
            return
        box.setText("Apple Music과 함께 쓰려면 권한 두 가지가 필요합니다.")
        box.setInformativeText(
            "• Music 제어 — 고친 곡을 Music에서 새로 읽게 합니다\n"
            "• Apple Music 보관함 접근 — Music에서 곡의 파일 위치를 찾습니다\n\n"
            "지금 허용해 두면 작업 도중에 권한 창이 뜨지 않습니다. "
            "'지금 허용하기'를 누르면 Music이 열리고 macOS 권한 창이 뜹니다. 모두 '허용'을 눌러 주세요.")
        allow = box.addButton("지금 허용하기", QMessageBox.AcceptRole)
        box.addButton("나중에", QMessageBox.RejectRole)
        never = box.addButton("다시 묻지 않기", QMessageBox.DestructiveRole)
        box.setDefaultButton(allow)
        box.exec()
        if box.clickedButton() is never:
            self.settings.setValue("perm/never", True)
            return
        if box.clickedButton() is not allow:
            return  # 다음에 켤 때 다시 묻는다

        def done(_):
            self.settings.setValue("perm/asked_version", ver)
            QMessageBox.information(self, "Apple Music 권한", "준비됐습니다. 이제 Music에 바로 반영할 수 있습니다.")

        def fail(msg):
            self.settings.setValue("perm/asked_version", ver)
            self._music_error(msg)
        run_task(self, "Apple Music 권한 확인 · 권한 창이 뜨면 '허용'을 눌러 주세요",
                 lambda prog, cancelled: music.ensure_access(launch=True), done, fail)

    # ------------------------------------------------------------ 앨범아트
    def find_artwork(self):
        files = self._files_for_command()
        if not files:
            return
        cache = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        dlg = ArtworkDialog(self, files, self.settings, os.path.join(cache, "recent-artwork"))
        if dlg.exec() and dlg.picture is not None:
            plans = []
            for f in files:
                pics = list(f.pictures)
                idx = f.primary_index()
                pic = dlg.picture
                if idx >= 0:
                    pics[idx] = pic
                else:
                    pics.insert(0, pic)
                plans.append(Plan(f, pictures=pics))
            self.apply_plans(plans, "커버 바꾸기")

    def resize_covers(self):
        files = self._files_for_command()
        if not files:
            return
        last = self.settings.value("quick/resize_cover")
        action = new_action("resize_cover")
        if isinstance(last, dict):
            action.update(last)
        dlg = ActionEditDialog(self, action)
        if dlg.exec():
            a = dlg.result_action()
            self.settings.setValue("quick/resize_cover", a)
            self._run_actions_with_preview(describe(a), [a], files)

    # ------------------------------------------------------------ 확장 태그 · 내보내기
    def extended_tags(self):
        files = self._files_for_command()
        if not files:
            return
        dlg = ExtendedTagsDialog(self, files)
        if not dlg.exec():
            return
        changes, pictures = dlg.result()
        plans = [Plan(f, changes=dict(changes), pictures=pictures) for f in files]
        self.apply_plans([p for p in plans if p.actionable], "확장 태그")

    def export_list(self):
        self._confirm_pending()
        if not self.model.files:
            QMessageBox.information(self, APP_NAME, "내보낼 파일이 없습니다.")
            return
        ExportDialog(self, self.selected_files(), self.visible_files(), self.settings, self.last_dir).exec()

    def make_playlist(self):
        files = self._files_for_command()
        if not files:
            return
        dlg = PlaylistDialog(self, files, self.settings)
        if dlg.exec():
            self.statusBar().showMessage(f"재생목록을 만들었습니다: {os.path.basename(dlg.saved_path)}", 5000)

    # ------------------------------------------------------------ 필터
    def focus_filter(self):
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def apply_filter(self):
        text = self.filter_edit.text()
        try:
            fn = compile_filter(text)
            self.filter_edit.setStyleSheet("")
            self.filter_edit.setToolTip("")
        except FilterError as e:
            self.filter_edit.setStyleSheet("QLineEdit { color: #c82828; }")
            self.filter_edit.setToolTip(f"필터 오류: {e}")
            self.statusBar().showMessage(f"필터 오류: {e}", 5000)
            return
        self.proxy.set_filter(fn)
        self._update_status()

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
        if e.mimeData().hasUrls() or music.drag_has_music():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() or music.drag_has_music():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if music.drag_has_music():
            e.acceptProposedAction()
            self._drop_from_music()
            return
        paths = self._drop_paths(e)
        if paths:
            self.load_paths(paths, replace=False)

    def eventFilter(self, obj, e):
        if obj is self.filter_edit and e.type() == QEvent.KeyPress and e.key() == Qt.Key_Escape:
            self.filter_edit.clear()
            self.table.setFocus()
            return True
        if obj is self.table.viewport():
            if e.type() in (QEvent.DragEnter, QEvent.DragMove) and (e.mimeData().hasUrls() or music.drag_has_music()):
                e.acceptProposedAction()
                return True
            if e.type() == QEvent.Drop:
                if music.drag_has_music():
                    e.acceptProposedAction()
                    self._drop_from_music()
                    return True
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


class App(QApplication):
    """Dock 아이콘에 끌어다 놓거나 Finder의 '다음으로 열기'로 연 파일을 받는다."""

    def __init__(self, argv):
        super().__init__(argv)
        self.window = None
        self.pending = []
        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.setInterval(200)
        self.open_timer.timeout.connect(self._open_pending)

    def event(self, e):
        if e.type() == QEvent.FileOpen:
            path = e.file()
            if path:
                self.pending.append(path)
                self.open_timer.start()  # 여러 파일이 한꺼번에 오면 모아서 연다
            return True
        return super().event(e)

    def _open_pending(self):
        paths, self.pending = self.pending, []
        if self.window and paths:
            self.window.load_paths(paths, replace=False)


def main(argv=None):
    import sys
    argv = argv if argv is not None else sys.argv
    app = App(argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    app.window = w
    w.show()
    paths = [p for p in argv[1:] if os.path.exists(p)]
    if paths:
        w.load_paths(paths, replace=True)
    return app.exec()
