"""메인 창."""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QHBoxLayout, QMenu, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSplitter, QTableView,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from .actiondialogs import ActionEditDialog, ActionsDialog, PlansPreview, choose_action_type
from . import music
from .osutil import IS_MAC, IS_WIN, REVEAL_LABEL, keys, reveal
from .tasks import run_task
from .backup import BackupStore
from .actions import ActionStore, describe, new_action, run_actions
from .converters import CONVERTERS, AutoNumber
from .filetrack import find_moved
from .filterexpr import FilterError, compile_filter
from .model import COLUMN_WIDTHS, COLUMNS, FileModel, SortProxy, fmt_length, fmt_size
from .panel import TagPanel, changes_for
from .rowdrag import RowDragger, move_to
from .plan import Plan, UndoEntry, UndoItem
from .safewrite import error_text
from .tagio import (
    AUDIO_EXTS, FIELD_LABELS, AudioFile, Options, move_file, restore_snapshot, scan,
)

APP_NAME = "LocalTag"
RELEASES_URL = "https://github.com/tsuna702/TagTag/releases"


def app_version() -> str:
    """앱 번들의 버전 (개발 중 실행이면 'dev'). 새 빌드마다 macOS 권한을 다시 묻기 때문에 버전별로 기록한다."""
    if getattr(sys, "frozen", False):
        if not IS_MAC:  # Windows 빌드는 build 때 _version.py 를 만들어 넣는다
            try:
                from ._version import VERSION
                return VERSION
            except ImportError:
                return "app"
        import plistlib
        try:
            info = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Info.plist")
            with open(info, "rb") as f:
                return str(plistlib.load(f).get("CFBundleVersion", "app"))
        except OSError:
            return "app"
    return "dev"


def _app_icon():
    """앱 아이콘 (번들의 LocalTag.icns, 개발 중이면 packaging/LocalTag.icns. Windows 는 .ico)."""
    from PySide6.QtGui import QIcon
    here = os.path.dirname(os.path.abspath(__file__))
    if IS_MAC:
        paths = (os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Resources", "LocalTag.icns"),
                 os.path.join(os.path.dirname(here), "packaging", "LocalTag.icns"))
    else:
        paths = (os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "LocalTag.ico"),
                 os.path.join(os.path.dirname(here), "packaging", "LocalTag.ico"))
    for path in paths:
        if os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return QApplication.windowIcon()


LOAD_WORKERS = 4
UNDO_LEVELS = 30
UNDO_MEMORY = 512 * 1024 * 1024  # 실행 취소 기록이 이보다 커지면 오래된 것부터 버린다
BACKUP_BATCH = 32  # 백업 기록을 이만큼씩 모아 디스크에 확정한다 (곡마다 fsync 하면 Windows 에서 곡당 약 2ms)


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
        QTimer.singleShot(0, self._prune_backups)  # 오래된 백업 정리는 창이 뜬 뒤에
        QTimer.singleShot(300, self._preload)

        # ---- 파일 목록
        self.model = FileModel(self)
        self.proxy = SortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 칸에서 바로 고치기: 더블클릭 · F2 (macOS 는 Return 도). 더블클릭은 eventFilter 에서 직접 처리한다
        self.table.setEditTriggers(QAbstractItemView.EditKeyPressed)
        self.model.on_edit = self._cell_edited
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
            # 저장한 뒤에 새로 생긴 열은 기본 표시 여부·폭으로 (restoreState 는 모두 보이게 둔다)
            known = self.settings.value("header_cols", 21, int)
            for i in range(known, len(COLUMNS)):
                hh.setSectionHidden(i, not COLUMNS[i][4])
                hh.resizeSection(i, COLUMN_WIDTHS.get(COLUMNS[i][0], 120))
        else:
            self.table.sortByColumn(COLUMNS.index(next(c for c in COLUMNS if c[0] == "path")), Qt.AscendingOrder)
        # 선택이 한꺼번에 여러 번 바뀌어도(새로 고침 후 다시 선택 등) 패널은 한 번만 갱신한다
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(0)
        self._sel_timer.timeout.connect(self._on_selection)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._sel_timer.start())
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
            keys("필터 (⌘F) — 예: 아이유 · artist HAS 아이유 · year GREATER 2010 AND genre MISSING"))
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
        frow = QHBoxLayout()
        frow.setSpacing(4)
        frow.addWidget(self.filter_edit, 1)
        quick = QToolButton()
        quick.setText("빠른 필터")
        quick.setToolTip("자주 쓰는 조건을 골라 필터에 넣기 (커버 없는 곡, 낮은 음질 …)")
        quick.setPopupMode(QToolButton.InstantPopup)
        quick.setFocusPolicy(Qt.NoFocus)
        qm = QMenu(quick)
        for label, text in QUICK_FILTERS:
            if label is None:
                qm.addSeparator()
            else:
                qm.addAction(label, lambda text=text: self.set_filter_text(text))
        qm.addSeparator()
        qm.addAction("커버 없는 곡을 모두 골라 커버 짝짓기…", self.match_missing_covers)
        qm.addAction("필터 지우기", lambda: self.set_filter_text(""))
        quick.setMenu(qm)
        frow.addWidget(quick)
        rv.addLayout(frow)
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

        # 고른 곡을 끌어다 다른 위치에 놓아 순서 바꾸기 (필터를 쓰는 중에는 끌 수 없다)
        self._row_drag = RowDragger(self.table, self._drop_rows, can_drag=lambda: self.proxy.filter_fn is None)
        self._build_menus()
        self._build_toolbar()
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 760)
        self._update_status()

    def _preload(self):
        """창이 뜬 직후 나머지 창 모듈을 미리 불러 둔다. 앱이 켜져 있는 동안 앱 파일이 새 버전으로
        바뀌어도(새로 빌드) 나중에 창을 열 때 다른 버전의 모듈을 읽지 않게 한다."""
        from . import artworkdialog, backupdialog, compare, covermatch, dupes, export, extractdialog, exttags, rename, renamedialog, ytclean, ytdialog  # noqa: F401
        from . import albumcheck, tagsource, tagsourcedialog  # noqa: F401

    def _prune_backups(self):
        try:
            self.backups.prune(self.settings.value("backup_days", 30, int),
                               self.settings.value("backup_max_gb", 5, int) * 1024 ** 3)
        except OSError:
            pass

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
        self.recent_menu = m.addMenu("최근 연 폴더")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        self._fill_recent()  # macOS 메뉴 막대는 비어 있는 메뉴를 못 열 수 있어 미리 채워 둔다
        m.addSeparator()
        self._act(m, "하위 폴더 포함", self._set_recursive, checkable=True, checked=self.recursive)
        self._act(m, "시작할 때 마지막 목록 다시 열기", lambda on: self.settings.setValue("reopen_last", on),
                  checkable=True, checked=self.settings.value("reopen_last", False, bool))
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
        # macOS에서는 메뉴 막대의 'LocalTag' 메뉴 맨 위로 옮겨진다 (Windows 는 아래 도움말 메뉴로)
        about = self._act(m, f"{APP_NAME} 정보", self.show_about)
        about.setMenuRole(QAction.AboutRole)
        if not IS_MAC:
            m.removeAction(about)

        m = mb.addMenu("편집")
        self.undo_act = self._act(m, "실행 취소", self.undo, QKeySequence.Undo)
        self.undo_act.setEnabled(False)
        m.addSeparator()
        self._act(m, "확장 태그…", self.extended_tags, "Ctrl+T")
        edit_cell = self._act(m, "이 칸 고치기 (목록에서)", self.edit_current_cell, "F2")
        edit_cell.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.table.addAction(edit_cell)
        self._act(m, "태그 비교 (고른 곡 나란히)…", self.compare_tags, "Ctrl+Shift+D")
        self._act(m, "같은 곡 · 여러 버전 찾기…", self.find_duplicates, "Ctrl+Shift+U")
        self._act(m, "앨범 점검 (Music 에서 갈라지는 앨범)…", self.check_albums, "Ctrl+Shift+L")
        m.addSeparator()
        self._act(m, "모두 선택", self.table.selectAll, QKeySequence.SelectAll)
        self._act(m, "선택 반전", self.invert_selection, "Ctrl+I")
        m.addSeparator()
        self._act(m, "위로 이동", lambda: self.move_selection(-1), "Ctrl+Alt+Up")
        self._act(m, "아래로 이동", lambda: self.move_selection(1), "Ctrl+Alt+Down")
        self._act(m, "목록 순서대로 트랙 번호…", self.number_tracks)
        m.addSeparator()
        # Windows 는 Delete 키가 익숙하다
        rm_key = "Backspace" if IS_MAC else "Delete"
        for a in (self._act(m, "목록에서 제거", self.remove_from_list, rm_key),
                  self._act(m, "태그 제거…", self.remove_tags, f"Ctrl+Shift+{rm_key}")):
            # 태그 입력 중에 Backspace가 가로채이지 않도록 목록에서만 동작
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.table.addAction(a)
        m.addSeparator()
        self._act(m, "재생", self.play, "Ctrl+P")
        self._act(m, REVEAL_LABEL, self.reveal, "Ctrl+Shift+R")
        m.addSeparator()
        self._act(m, "필터", self.focus_filter, QKeySequence.Find)

        m = mb.addMenu("변환")
        for text, key, cls in CONVERTERS:
            self._act(m, text, lambda _=False, cls=cls: self.run_converter(cls), key)
        m.addSeparator()
        self._act(m, "곡명 · 파일명 일괄 변경…", self.batch_rename, "Alt+9")
        m.addSeparator()
        self._act(m, "Apple Music · MusicBrainz 에서 태그 가져오기…", self.tag_source, "Ctrl+Shift+G")

        m = mb.addMenu("커버")
        self._act(m, "앨범아트 찾기 · 가져오기…", self.find_artwork, "Ctrl+Shift+A")
        self._act(m, "커버 짝짓기 (그림 파일을 곡마다)…", self.match_covers, "Ctrl+Shift+J")
        self._act(m, "커버 크기 조정…", self.resize_covers, "Ctrl+Shift+K")
        m.addSeparator()
        self._act(m, "앨범아트 추출 (링크 · 보관함 · 목록 · YouTube)…", self.extract_artwork, "Ctrl+Shift+X")

        if IS_MAC:  # Apple Music 연동은 macOS 에서만
            self._build_music_menu(mb)

        m = mb.addMenu("액션")
        self._act(m, "액션 그룹…", self.run_action_groups, "Alt+6")
        self._act(m, "빠른 액션…", self.quick_action, "Alt+7")
        self._act(m, "YouTube 제목 정리…", self.youtube_clean, "Alt+8")
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
        if not IS_MAC:  # macOS 는 앱 메뉴로 옮겨지므로 Windows 에만 도움말 메뉴
            m = mb.addMenu("도움말")
            m.addAction(about)

    def _build_music_menu(self, mb):
        m = mb.addMenu("Music")
        self._act(m, "Apple Music에 반영 (새로 읽기)", self.music_refresh, "Ctrl+Shift+M")
        self._act(m, "저장한 뒤 자동으로 Music에 반영", lambda on: self.settings.setValue("music_auto", on),
                  checkable=True, checked=self.settings.value("music_auto", False, bool))
        sub2 = m.addMenu("반영한 뒤 Music 다시 켜기 (앨범이 갈라져 보이지 않게)")
        grp2 = QActionGroup(self)
        cur2 = self.settings.value("music_restart", music.DEFAULT_RESTART, str)
        for key, label in music.RESTART_MODES.items():
            a = self._act(sub2, label, lambda on, key=key: on and self.settings.setValue("music_restart", key),
                          checkable=True, checked=key == cur2)
            grp2.addAction(a)
        sub2.addSeparator()
        note = sub2.addAction("앨범·앨범 아티스트·컴필레이션·디스크를 바꿨을 때만")
        note.setEnabled(False)
        sub = m.addMenu("반영 속도")
        grp = QActionGroup(self)
        cur = self.settings.value("music_pace", music.DEFAULT_PACE, str)
        for key, label in music.PACE_LABELS.items():
            a = self._act(sub, label, lambda on, key=key: on and self.settings.setValue("music_pace", key),
                          checkable=True, checked=key == cur)
            grp.addAction(a)
        m.addSeparator()
        self._act(m, "Music에서 선택한 곡 추가", self.music_add_selection, "Ctrl+Shift+I")
        m.addSeparator()
        self._act(m, "Music 권한 확인", self.music_check)
        self._act(m, "Music 권한 설정 열기…", music.open_privacy_settings)

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

        self.tb_save = button("저장", self.save, keys("바꾼 태그 저장 (⌘S)"))
        self.tb_undo = button("되돌리기", self.undo, keys("마지막 작업 되돌리기 (⌘Z)"))
        tb.addSeparator()
        self.tb_refresh = button("새로고침", self.refresh, "목록의 파일을 다시 읽기 (F5)")
        self.tb_clear = button("목록 지우기", self.clear_list, "목록 비우기 — 파일은 지우지 않습니다")
        tb.addSeparator()
        self.tb_finder = button(REVEAL_LABEL, self.reveal, keys(f"선택한 파일을 {REVEAL_LABEL} (⇧⌘R)"))
        tb.addSeparator()
        self.tb_up = button("↑ 위로", lambda: self.move_selection(-1), keys("선택한 곡을 한 칸 위로 (⌥⌘↑)"))
        self.tb_down = button("↓ 아래로", lambda: self.move_selection(1), keys("선택한 곡을 한 칸 아래로 (⌥⌘↓)"))
        self.tb_rename = button("일괄 변경…", self.batch_rename,
                                keys("곡명·파일명을 규칙(바꾸기·붙이기·지우기·번호…)으로 한꺼번에 바꾸기 (⌥9) — 선택한 곡, 선택이 없으면 목록 전체"))
        self.tb_number = button("트랙 번호…", self.number_tracks,
                                "목록에 보이는 순서대로 트랙 번호 매기기 — 선택한 곡, 선택이 없으면 목록 전체")
        self._update_toolbar()

    def _update_toolbar(self):
        if not hasattr(self, "tb_save"):
            return
        has_files = bool(self.model.files)
        self.tb_save.setEnabled(self.panel.is_dirty())
        self.tb_undo.setEnabled(bool(self.undo_stack))
        self.tb_undo.setToolTip(keys(f"되돌리기: {self.undo_stack[-1].label} (⌘Z)") if self.undo_stack
                                else "되돌릴 작업이 없습니다")
        self.tb_refresh.setEnabled(has_files)
        self.tb_clear.setEnabled(has_files)
        has_sel = self.table.selectionModel().hasSelection()
        self.tb_finder.setEnabled(has_sel)
        self.tb_up.setEnabled(has_sel)
        self.tb_down.setEnabled(has_sel)
        self.tb_number.setEnabled(has_files)
        self.tb_rename.setEnabled(has_files)

    def show_about(self):
        ver = app_version()
        shown = "개발 중 실행 (dev)" if ver == "dev" else ver
        box = QMessageBox(self)
        box.setWindowTitle(f"{APP_NAME} 정보")
        icon = _app_icon()
        if not icon.isNull():
            box.setIconPixmap(icon.pixmap(64, 64))
        box.setTextFormat(Qt.RichText)
        box.setText(f"<b style='font-size:15px'>{APP_NAME}</b><br>버전 {shown}")
        box.setInformativeText(
            ("macOS" if IS_MAC else "Windows") + "용 음악 태그 편집기<br><br>"
            f"<a href='{RELEASES_URL}'>새 버전 · 바뀐 점 보기</a>")
        for label in box.findChildren(QLabel):
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        box.exec()

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
        m.addAction(REVEAL_LABEL, self.reveal)
        m.addSeparator()
        m.addAction("확장 태그…", self.extended_tags)
        if len(self.selected_files()) > 1:
            m.addAction("태그 비교…", self.compare_tags)
        m.addAction("앨범아트 찾기…", self.find_artwork)
        if IS_MAC:
            m.addAction("Apple Music에 반영", self.music_refresh)
        m.addAction("재생목록 만들기…", self.make_playlist)
        m.addAction("내보내기…", self.export_list)
        m.addSeparator()
        conv = m.addMenu("변환")
        for text, _, cls in CONVERTERS:
            conv.addAction(text, lambda cls=cls: self.run_converter(cls))
        conv.addSeparator()
        conv.addAction("곡명 · 파일명 일괄 변경…", self.batch_rename)
        conv.addAction("Apple Music · MusicBrainz 에서 태그 가져오기…", self.tag_source)
        act = m.addMenu("액션")
        act.addAction("액션 그룹…", self.run_action_groups)
        act.addAction("빠른 액션…", self.quick_action)
        act.addAction("YouTube 제목 정리…", self.youtube_clean)
        act.addAction("자동 번호 매기기…", lambda: self.run_converter(AutoNumber))
        m.addSeparator()
        m.addAction("목록에서 제거", self.remove_from_list)
        m.addAction("태그 제거…", self.remove_tags)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ 선택
    def _source_files(self, proxy_rows) -> list[AudioFile]:
        files = self.model.files
        if self.proxy.filter_fn is None:
            # 필터가 없으면 표의 행 = 원본 행 (정렬은 원본 모델이 한다)
            return [files[r] for r in proxy_rows]
        return [files[self.proxy.mapToSource(self.proxy.index(r, 0)).row()] for r in proxy_rows]

    def selected_files(self) -> list[AudioFile]:
        # 화면에 보이는 순서대로 (텍스트→태그, %_counter% 등이 이 순서를 따른다)
        rows = set()
        for rng in self.table.selectionModel().selection():
            rows.update(range(rng.top(), rng.bottom() + 1))
        return self._source_files(sorted(rows))

    def visible_files(self) -> list[AudioFile]:
        if self.proxy.filter_fn is None:
            return list(self.model.files)
        return self._source_files(range(self.proxy.rowCount()))

    def _select_rows(self, rows, flags=QItemSelectionModel.Select):
        """표의 행 번호들을 한 번에 선택한다 (행마다 선택하면 그때마다 패널이 갱신된다)."""
        sel = QItemSelection()
        last_col = self.proxy.columnCount() - 1
        rows = sorted(rows)
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1] == rows[j] + 1:
                j += 1
            sel.select(self.proxy.index(rows[i], 0), self.proxy.index(rows[j], last_col))
            i = j + 1
        self.table.selectionModel().select(sel, flags | QItemSelectionModel.Rows)

    def _on_selection(self, *_):
        self._confirm_pending()
        sel = self.selected_files()
        self.panel.set_files(sel)
        self._update_status(sel)

    def invert_selection(self):
        n = self.proxy.rowCount()
        if n:
            self._select_rows(range(n), QItemSelectionModel.Toggle)

    def move_selection(self, delta):
        """선택한 곡을 목록에서 한 칸 위·아래로 옮긴다 (트랙 번호를 목록 순서대로 매기기 전에)."""
        files = self.selected_files()
        if not files:
            return
        if self.proxy.filter_fn is not None:
            self.statusBar().showMessage("필터를 지운 뒤 순서를 바꿔 주세요.", 5000)
            return
        if self.model.sort_column >= 0:
            # 열 정렬을 풀고 지금 보이는 순서에서 직접 정한 순서로 바꾼다
            self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        if self.model.move(files, delta):
            self.table.scrollTo(self.proxy.index(self.model.row_of(files[0 if delta < 0 else -1]), 0))
            self.statusBar().showMessage("목록 순서를 바꿨습니다. '트랙 번호…'로 이 순서대로 번호를 매길 수 있습니다.", 5000)

    def _drop_rows(self, target):
        """목록에서 끌어다 놓기: 고른 곡을 target 줄 앞으로 (필터가 없을 때 표의 행 = 원본 행)."""
        files = self.selected_files()
        if not files:
            return
        if self.model.sort_column >= 0:
            self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        if self.model.set_order(move_to(self.model.files, {id(f) for f in files}, target)):
            self.statusBar().showMessage("목록 순서를 바꿨습니다. '트랙 번호…'로 이 순서대로 번호를 매길 수 있습니다.", 5000)

    def number_tracks(self):
        """목록에 보이는 순서대로 트랙 번호 (선택한 곡, 선택이 없으면 보이는 곡 전체)."""
        self._confirm_pending()
        files = self.selected_files() or self.visible_files()
        if not files:
            return
        dlg = AutoNumber(self, files, self.settings)
        if dlg.exec():
            self._take_order(dlg)
            self.apply_plans(dlg.result_plans(), "자동 번호 매기기")

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

    def _update_status(self, sel=None):
        if sel is None:
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
        self._remember_paths(paths, replace)
        self._report_failures(failed, "읽지 못한 파일")
        self.statusBar().showMessage(f"{len(loaded)}개 파일을 불러왔습니다.", 5000)

    # ------------------------------------------------------------ 최근 연 폴더
    def _settings_list(self, key):
        v = self.settings.value(key, [])
        return [v] if isinstance(v, str) else list(v or [])

    def _remember_paths(self, paths, replace):
        """연 폴더는 '최근 연 폴더'에, 목록을 이룬 경로는 '마지막 목록'에 적어 둔다."""
        dirs = [os.path.abspath(p) for p in paths if os.path.isdir(p)]
        if dirs:
            dirs = list(dict.fromkeys(dirs))
            recent = dirs + [d for d in self._settings_list("recent_folders") if d not in dirs]
            self.settings.setValue("recent_folders", recent[:RECENT_MAX])
            # 단축키(⌥⌘1~9)가 새 목록을 따르게. 메뉴 항목을 누른 처리 중에 그 메뉴를 비우지 않도록 한 박자 뒤에
            QTimer.singleShot(0, self._fill_recent)
        roots = [] if replace else self._settings_list("last_list")
        roots += [os.path.abspath(p) for p in paths if os.path.abspath(p) not in roots]
        self.settings.setValue("last_list", roots)

    def _fill_recent(self):
        m = self.recent_menu
        m.clear()
        recent = self._settings_list("recent_folders")
        if not recent:
            a = m.addAction("(아직 없음)")
            a.setEnabled(False)
            return
        for i, d in enumerate(recent):
            name = os.path.basename(d.rstrip(os.sep)) or d
            parent = os.path.dirname(d.rstrip(os.sep)).replace(os.path.expanduser("~"), "~", 1)
            a = m.addAction(f"{name}  —  {parent}")
            a.setToolTip(d)
            if i < 9:
                a.setShortcut(QKeySequence(f"Ctrl+Alt+{i + 1}"))
            if not os.path.isdir(d):
                a.setText(f"{name}  —  찾을 수 없음")
                a.setEnabled(False)
            a.triggered.connect(lambda _=False, d=d: self.open_recent(d))
        m.addSeparator()
        hint = m.addAction(keys("⇧ 를 누른 채 고르면 지금 목록에 추가"))
        hint.setEnabled(False)
        m.addAction("목록 지우기", self._clear_recent)

    def _clear_recent(self):
        self.settings.setValue("recent_folders", [])
        QTimer.singleShot(0, self._fill_recent)

    def open_recent(self, folder):
        if not os.path.isdir(folder):
            QMessageBox.information(self, APP_NAME, f"폴더를 찾을 수 없습니다.\n{folder}")
            return
        add = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        self.load_paths([folder], replace=not add)

    def reopen_last(self):
        """'시작할 때 마지막 목록 다시 열기'가 켜져 있으면 지난번 목록을 다시 불러온다."""
        if not self.settings.value("reopen_last", False, bool):
            return
        roots = [p for p in self._settings_list("last_list") if os.path.exists(p)]
        if roots:
            self.load_paths(roots, replace=True)

    def _load_files(self, paths, label):
        loaded, failed = [], []
        opts = self.opts

        def load(p):
            try:
                return AudioFile.load(p, opts), None
            except Exception as e:  # 손상된 파일 하나 때문에 전체 불러오기가 멈추지 않게
                return None, (p, str(e))

        if len(paths) <= 8:
            results = map(load, paths)
            pool = None
        else:
            # 디스크에서 읽는 동안 기다리지 않도록 여러 파일을 동시에 읽는다 (외장 디스크·NAS에서 특히 빠르다)
            pool = ThreadPoolExecutor(max_workers=LOAD_WORKERS)
            results = pool.map(load, paths)
        prog = None
        if len(paths) > 30:
            prog = QProgressDialog(label, "취소", 0, len(paths), self)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(300)
        try:
            for i, (af, err) in enumerate(results):
                if prog and i % 16 == 0:
                    if prog.wasCanceled():
                        break
                    prog.setValue(i)
                if af is not None:
                    loaded.append(af)
                else:
                    failed.append(err)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
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
                except KeyError:
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
        rows = [r for r, f in enumerate(self.model.files) if f.path in selected]
        if self.proxy.filter_fn is not None:
            rows = [self.proxy.mapFromSource(self.model.index(r, 0)).row() for r in rows]
        self._select_rows(r for r in rows if r >= 0)
        sel = self.selected_files()
        self.panel.set_files(sel)
        self._update_status(sel)
        self._report_failures(failed, "읽지 못한 파일")

    def clear_list(self):
        self._confirm_pending()
        self.settings.setValue("last_list", [])
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
            p = Plan(f, changes=changes_for(changes, f), pictures=pics)
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

    @staticmethod
    def _prepare_backups(job, plans, start):
        """plans[start:start+BACKUP_BATCH] 의 백업 사본을 먼저 만들고 기록을 한 번에 디스크에 확정한다.
        {번호: 백업 항목 또는 그 곡에서 난 오류}. 원본은 이 뒤에야 바꾸므로 '바꾸기 전에 기록' 은 그대로 지켜진다
        (손대기 전에 앱이 꺼지면 남는 항목은 내용이 같은 원본을 가리킬 뿐이다)."""
        out = {}
        for j in range(start, min(start + BACKUP_BATCH, len(plans))):
            p = plans[j]
            if p.touches_tags or p.new_path:
                try:
                    out[j] = job.before(p.file.path, copy_file=p.touches_tags, sync=False)
                except Exception as e:
                    out[j] = e
        try:
            job.sync()
        except OSError as e:  # 기록을 확정하지 못했으면 이 묶음의 곡은 바꾸지 않는다
            out = {j: v if isinstance(v, Exception) else e for j, v in out.items()}
        return out

    def apply_plans(self, plans, label, refresh_panel=True):
        """작업 목록을 실제 파일에 적용하고 실행 취소 기록을 남긴다."""
        # Music이 그사이 파일을 옮겼을 수 있으므로 먼저 새 위치를 찾는다
        self._relink_missing([p.file for p in plans])
        if not plans:
            if refresh_panel:
                self.panel.set_files(self.selected_files())
            return
        entry = UndoEntry(label)
        # 앨범을 이루는 값이 바뀌면 Music 앨범 화면이 갈라져 보일 수 있다 → 반영 뒤 Music 다시 켜기
        entry.album_change = any(p.remove_tags or set(p.changes) & ALBUM_KEYS for p in plans)
        # Music 에 반영할 거라면 파일을 바꾸기 전에 곡 ID 를 찾아 둔다 (바꾼 뒤 경로로 찾으면 Music 이 몰아서 다시 읽는다)
        music_on = IS_MAC and self.settings.value("music_auto", False, bool)
        pids = self._music_prelookup([p.file for p in plans if p.touches_tags or p.new_path]) if music_on else {}
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
        prepared = {}
        for i, p in enumerate(plans):
            if prog:
                prog.setValue(i)
            if job is not None and i % BACKUP_BATCH == 0:
                prepared = self._prepare_backups(job, plans, i)
            f = p.file
            old_path = f.path
            held = []  # 실행 취소용 스냅샷 — 첫 저장 때 파일을 읽으면서 함께 만든다 (따로 읽지 않는다)
            bentry = None
            try:
                if job is not None and (p.touches_tags or p.new_path):
                    bentry = prepared.pop(i)
                    if isinstance(bentry, Exception):
                        err, bentry = bentry, None
                        raise err
                new = f
                if p.remove_tags:
                    new = new.remove_tags(self.opts, snapshot=held)
                if p.changes or p.pictures is not None:
                    new = new.save(p.changes, p.pictures, self.opts, snapshot=None if held else held)
                cur_path = new.path
                if p.new_path:
                    move_file(cur_path, p.new_path)
                    cur_path = p.new_path
                entry.items.append(UndoItem(old_path, cur_path, held[0] if held else None))
                entry.items[-1].pid = pids.get(os.path.realpath(old_path))
                held.clear()
                if bentry is not None:
                    job.after(bentry, cur_path)
                    bentry = None
                if new is f:
                    new = AudioFile.load(cur_path, self.opts)
                else:
                    new.path = cur_path  # 저장 직후 읽은 정보를 그대로 쓴다 (이름 바꾸기는 내용을 바꾸지 않는다)
                self.model.replace(f, new)
                for path, data in p.exports:
                    with open(path, "wb") as fh:
                        fh.write(data)
            except Exception as e:
                failed.append((old_path, error_text(e)))
                if bentry is not None:
                    job.failed(bentry, str(e))
                if held and os.path.exists(old_path):
                    # 일부만 바뀐 채로 남지 않게 되돌려 둔다
                    entry.items.append(UndoItem(old_path, old_path, held[0]))
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
            if music_on:
                changed = [i.new_path for i in entry.items]
                known = {i.new_path: i.pid for i in entry.items if getattr(i, "pid", None)}
                self._music_refresh_paths(changed, modal=False, album_change=entry.album_change, known_pids=known)
        if refresh_panel:
            self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "처리하지 못한 파일")
        self.statusBar().showMessage(f"{label}: {len(plans) - len(failed)}개 파일에 적용했습니다.", 5000)

    # ------------------------------------------------------------ 백업
    def show_backups(self):
        self._confirm_pending()
        from .backupdialog import BackupDialog  # 필요할 때만 불러와 앱이 빨리 켜지게
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
        while len(self.undo_stack) > UNDO_LEVELS or (len(self.undo_stack) > 1 and self._undo_size() > UNDO_MEMORY):
            self.undo_stack.pop(0)
        self._update_undo_action()

    def _undo_size(self):
        seen = set()  # 여러 기록이 함께 쓰는 커버는 한 번만 센다
        return sum(e.size_estimate(seen) for e in self.undo_stack)

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
        if getattr(self, "_music_busy", False):
            # Music 이 아직 파일을 옮기는 중이면 경로가 바뀔 수 있다 — 반영이 끝난 뒤 되돌린다
            self._undo_waiting = True
            self.statusBar().showMessage("Music 반영이 끝나면 바로 되돌립니다…", 10000)
            return
        entry = self.undo_stack.pop()
        by_path = {f.path: f for f in self.model.files}
        failed = []
        for item in reversed(entry.items):
            try:
                if item.new_path != item.old_path:
                    move_file(item.new_path, item.old_path)
                if item.snapshot is not None:
                    restored = restore_snapshot(item.old_path, item.snapshot, opts=self.opts)
                else:
                    restored = AudioFile.load(item.old_path, self.opts)
                f = by_path.get(item.new_path)
                if f is not None:
                    self.model.replace(f, restored)
            except Exception as e:
                failed.append((item.new_path, error_text(e)))
        self._update_undo_action()
        self.panel.set_files(self.selected_files())
        self._update_status()
        self._report_failures(failed, "되돌리지 못한 파일")
        self.statusBar().showMessage(f"실행 취소: {entry.label}", 5000)
        # 되돌린 태그도 Music 에 알린다 (저장할 때처럼 자동 반영을 켰을 때)
        bad = {p for p, _ in failed}
        restored_paths = [i.old_path for i in entry.items if i.new_path not in bad and os.path.exists(i.old_path)]
        if IS_MAC and restored_paths and self.settings.value("music_auto", False, bool):
            known = {i.old_path: i.pid for i in entry.items if getattr(i, "pid", None)}
            self._music_refresh_paths(restored_paths, modal=False,
                                      album_change=getattr(entry, "album_change", True), known_pids=known)

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
            self._take_order(dlg)
            self.apply_plans(dlg.result_plans(), cls.title.replace("변환: ", ""))

    def _take_order(self, dlg):
        """자동 번호 매기기 창에서 바꾼 곡 순서를 목록에도 반영한다."""
        if not getattr(dlg, "moved", False):
            return
        if self.model.sort_column >= 0:
            self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.model.arrange(dlg.files)

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

    def quick_action(self, kind=None):
        files = self._files_for_command()
        if not files:
            return
        if not kind:
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

    def batch_rename(self):
        """파일 이름 일괄 변경 프로그램처럼 규칙을 쌓아 곡명·파일명을 한꺼번에 바꾼다."""
        self._confirm_pending()
        files = self.selected_files() or self.visible_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "바꿀 곡을 목록에 먼저 불러오세요.")
            return
        from .renamedialog import RenameDialog
        dlg = RenameDialog(self, files, self.settings)
        if dlg.exec():
            self.apply_plans(dlg.result_plans(), "일괄 변경")

    def tag_source(self):
        """Apple Music 앨범 정보로 고른 곡의 태그 채우기 (짝은 자동, 곡마다 직접 고칠 수 있음)."""
        files = self._files_for_command()
        if not files:
            return
        from .tagsourcedialog import TagSourceDialog
        dlg = TagSourceDialog(self, files, self.settings)
        if dlg.exec() and dlg.plans:
            self.apply_plans(dlg.plans, "Apple Music 에서 태그 가져오기")

    def check_albums(self):
        """같은 앨범 안의 태그 차이(앨범 아티스트·컴필레이션·총수·커버 등)를 찾아 맞춘다."""
        self._confirm_pending()
        from .albumcheck import AlbumCheckDialog, album_key
        files = self.selected_files()
        if files:
            # 고른 곡이 속한 앨범은 목록에 있는 곡 전체로 점검한다 (한 곡만 골라도)
            keys = {album_key(f.get("album")) for f in files if f.get("album").strip()}
            files = [f for f in self.model.files if album_key(f.get("album")) in keys] or files
        else:
            files = self.visible_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "점검할 곡을 목록에 먼저 불러오세요.")
            return
        dlg = AlbumCheckDialog(self, files)
        if not dlg.exec():
            return
        if dlg.picked:
            self.select_files(dlg.picked)
        elif dlg.plans:
            self.apply_plans(dlg.plans, "앨범 점검")

    def edit_current_cell(self):
        ix = self.table.currentIndex()
        if ix.isValid() and ix.flags() & Qt.ItemIsEditable:
            self.table.edit(ix)

    def _cell_edited(self, f, key, value):
        """목록 칸에서 고친 값 저장 — 패널에서 고친 것과 같이 안전 저장 · 실행 취소 · Music 반영."""
        self._confirm_pending()
        if key == "filename":
            from .converters import build_target_path
            stem = value.strip()
            ext = os.path.splitext(f.path)[1]
            if stem.lower().endswith(ext.lower()):
                stem = stem[:-len(ext)]  # 확장자까지 쳤으면 한 번만
            try:
                path = build_target_path(f, stem.replace("/", "_").replace("\\", "_"), False)
            except ValueError as e:
                QMessageBox.warning(self, "파일명", str(e))
                return
            if path == f.path:
                return
            if os.path.exists(path) and not os.path.samefile(path, f.path):
                QMessageBox.warning(self, "파일명", f"같은 이름의 파일이 이미 있습니다.\n{os.path.basename(path)}")
                return
            plan = Plan(f, new_path=path)
            label = "파일명 바꾸기"
        else:
            from .script import split_values
            vals = [v.strip() for v in split_values(value.strip()) if v.strip()]
            plan = Plan(f, changes={key: vals})
            label = f"{FIELD_LABELS.get(key, key)} 고치기"
        # 칸 편집기가 닫힌 뒤에 저장한다 (저장하면 행이 새 파일로 바뀐다)
        QTimer.singleShot(0, lambda: self.apply_plans([plan], label))

    def youtube_clean(self):
        """YouTube에서 받은 곡의 제목에서 꼬리말을 떼고 '아티스트 - 제목'을 나눈다.
        고른 곡, 고른 곡이 없으면 목록에 보이는 곡 전체를 한 번에 (미리 보고 적용)."""
        self._confirm_pending()
        files = self.selected_files() or self.visible_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "정리할 곡을 목록에 먼저 불러오세요.")
            return
        from .ytdialog import YtCleanDialog
        dlg = YtCleanDialog(self, files, self.settings)
        if dlg.exec():
            self.apply_plans(dlg.result_plans(), "YouTube 제목 정리")

    def compare_tags(self):
        files = self.selected_files()
        if len(files) < 2:
            QMessageBox.information(self, APP_NAME, "비교할 곡을 두 개 이상 고르세요.")
            return
        from .compare import CompareDialog
        CompareDialog(self, files).exec()

    def find_duplicates(self):
        self._confirm_pending()
        files = list(self.model.files)
        if len(files) < 2:
            QMessageBox.information(self, APP_NAME, "곡을 목록에 먼저 불러오세요.")
            return
        from .dupes import DuplicatesDialog
        dlg = DuplicatesDialog(self, files, self.settings)
        if not dlg.exec():
            return
        if dlg.mode == "remove":
            self.table.selectionModel().clearSelection()
            self.panel.set_files([])
            self.model.remove(dlg.picked)
            self._update_status([])
            self.statusBar().showMessage(f"{len(dlg.picked)}곡을 목록에서 뺐습니다 (파일은 그대로).", 5000)
        else:
            self.select_files(dlg.picked)

    def select_files(self, files):
        """이 파일들만 목록에서 고른다. 필터에 가려진 곡이 있으면 필터를 지운다."""
        ids = {id(f) for f in files}
        rows = [r for r, f in enumerate(self.model.files) if id(f) in ids]
        if self.proxy.filter_fn is not None:
            prow = [self.proxy.mapFromSource(self.model.index(r, 0)).row() for r in rows]
            if any(r < 0 for r in prow):
                self.set_filter_text("")
            else:
                rows = prow
        self.table.selectionModel().clearSelection()
        self._select_rows(rows)
        if rows:
            self.table.scrollTo(self.proxy.index(min(rows), 0))

    def set_filter_text(self, text):
        """빠른 필터: 입력칸에 넣고 바로 적용한다."""
        self.filter_edit.setText(text)
        self.filter_timer.stop()
        self.apply_filter()

    def match_missing_covers(self):
        """커버 없는 곡만 보이게 한 뒤 모두 골라 커버 짝짓기 창을 연다."""
        self.set_filter_text("cover MISSING")
        n = self.proxy.rowCount()
        if not n:
            QMessageBox.information(self, APP_NAME, "커버 없는 곡이 없습니다.")
            return
        self.table.selectAll()
        self.match_covers()

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
            self._music_refresh_paths([f.path for f in files], modal=True, album_change=True)

    def _music_prelookup(self, files):
        """파일을 바꾸기 전에 Music 곡 ID 를 찾는다 (지금 제목·앨범으로). {실제 경로: pid}"""
        items = [(f.path, f.get("title").split("\\\\")[0] or f.stem, f.get("album").split("\\\\")[0]) for f in files]
        if not items:
            return {}
        prog = None
        if len(items) > 10:
            prog = QProgressDialog("Music에서 곡 찾는 중…", None, 0, len(items), self)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(500)

        def report(cur, total, msg):
            if prog:
                prog.setValue(min(cur, total))
                QApplication.processEvents()
        try:
            return music.prelookup(items, report)
        except Exception:
            return {}  # 못 찾으면 반영할 때 예전 방식으로 찾는다
        finally:
            if prog:
                prog.close()

    def _music_refresh_paths(self, paths, modal=True, album_change=False, known_pids=None):
        if getattr(self, "_music_busy", False):
            self.statusBar().showMessage("Music 작업이 이미 진행 중입니다.", 4000)
            return
        self._music_busy = True
        pace = self.settings.value("music_pace", music.DEFAULT_PACE, str)
        restart_mode = self.settings.value("music_restart", music.DEFAULT_RESTART, str) if album_change else "never"
        # Music에 보여야 할 값 (제목·앨범) — 반영됐는지 확인하는 데 쓴다
        by_path = {f.path: f for f in self.model.files}
        expected = {p: (by_path[p].get("title").split("\\\\")[0], by_path[p].get("album").split("\\\\")[0])
                    for p in paths if p in by_path}

        def done(res):
            self._music_busy = False
            moved = self._relink(res.relocated)
            self._run_waiting_undo()
            parts = [f"반영 확인됨 {res.confirmed}곡"]
            if res.unconfirmed:
                parts.append(f"Music에 아직 옛 정보가 보임 {len(res.unconfirmed)}곡")
            if res.notfound:
                parts.append(f"Music 보관함에 없음 {len(res.notfound)}곡")
            if res.failed:
                parts.append(f"실패 {len(res.failed)}곡")
            if moved:
                parts.append(f"옮겨진 파일 {moved}개 다시 연결")
            if res.restart == "restarted":
                parts.append("Music 다시 켬 (앨범 화면 정리)")
            elif res.restart == "playing":
                parts.append("재생 중이라 Music 을 다시 켜지 않음 — 앨범이 갈라져 보이면 Music 을 다시 켜 주세요")
            elif res.restart and res.restart.startswith("실패"):
                parts.append(f"Music 다시 켜기 {res.restart}")
            if res.changed:
                # 반영하는 사이 Music 에서 직접 고친 곡 — 그 값을 따르고 목록을 다시 읽는다
                parts.append(f"Music에서 고친 값을 따름 {self._reload_paths(res.changed, res.relocated)}곡")
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
            self._run_waiting_undo()
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

        opts = self.opts

        def file_values(path):
            f = AudioFile.load(path, opts)
            return f.get("title").split("\\\\")[0], f.get("album").split("\\\\")[0]

        def job(prog, cancelled):
            prog(0, len(paths), "옮겨진 파일이 있는지 확인하는 중")
            moved = find_moved(entries, cancelled)          # 반영 전에 이미 옮겨진 파일
            now = [moved.get(p, p) for p in paths]
            for p in paths:
                if p in moved:
                    inode[os.path.realpath(moved[p])] = inode.get(os.path.realpath(p), (0, 0))
            exp = {moved.get(p, p): v for p, v in expected.items()}
            known = {moved.get(p, p): pid for p, pid in (known_pids or {}).items()}
            res = music.refresh(now, prog, cancelled, exp, relocate=relocate, pace=pace,
                                file_values=file_values, known_pids=known)
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
            res.changed = [orig.get(k, k) for k in res.changed]
            res.restart = None
            if res.confirmed and restart_mode != "never" and not cancelled():
                try:
                    res.restart = music.restart(restart_mode, prog)
                except music.MusicError as e:
                    res.restart = f"실패: {e}"
            return res

        run_task(self, "Apple Music에 반영", job,
                 done, fail, modal=modal, status=lambda m: self.statusBar().showMessage(m, 3000))

    def _reload_paths(self, paths, relocated):
        """목록의 이 파일들을 지금 파일 내용으로 다시 읽는다 (옮겨졌으면 새 위치에서)."""
        by_path = {os.path.realpath(f.path): f for f in self.model.files}
        n = 0
        for p in paths:
            now = relocated.get(os.path.realpath(p), p)
            # 옮겨진 곡은 목록이 이미 새 경로로 바뀌어 있다
            f = by_path.get(os.path.realpath(now)) or by_path.get(os.path.realpath(p))
            if f is None:
                continue
            try:
                self.model.replace(f, AudioFile.load(now, self.opts))
                n += 1
            except Exception:
                pass
        if n:
            self.panel.set_files(self.selected_files())
        return n

    def _run_waiting_undo(self):
        if getattr(self, "_undo_waiting", False):
            self._undo_waiting = False
            QTimer.singleShot(0, self.undo)

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
        self._relink_undo(relocated)
        if moved:
            self.panel.set_files(self.selected_files())
        return moved

    def _relink_undo(self, relocated):
        """Music 이 옮긴 파일은 실행 취소 기록의 경로도 새 위치로 고친다 (앨범을 바꾸면 Music 이 앨범 폴더를 옮긴다).
        이름을 바꾸지 않은 곡은 원래 자리도 새 위치로 본다 — 되돌릴 때 파일을 옮기지 않고 그 자리에서 태그만 되돌리고,
        폴더 정리는 다시 Music 에 맡긴다."""
        moves = {os.path.realpath(o): n for o, n in relocated.items() if os.path.exists(n)}
        if not moves:
            return
        for entry in self.undo_stack:
            for item in entry.items:
                new = moves.get(os.path.realpath(item.new_path))
                if new is None:
                    continue
                if item.old_path == item.new_path:
                    item.old_path = new
                item.new_path = new

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
        auto = music.automation_status()           # Music 제어 (자동화)
        media = music.media_library_status()      # Apple Music 보관함 접근 (미디어 및 Apple Music)
        need_auto, need_media = auto != "granted", media in ("ask", "denied")
        if not need_auto and not need_media:
            self.settings.setValue("perm/asked_version", ver)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Apple Music 권한")
        if auto == "denied" or media == "denied":
            off = []
            if auto == "denied":
                off.append("• Music 제어 — 시스템 설정 → 개인정보 보호 및 보안 → 자동화 → LocalTag 아래의 'Music'")
            if media == "denied":
                off.append("• Apple Music 보관함 접근 — 시스템 설정 → 개인정보 보호 및 보안 → 미디어 및 Apple Music → LocalTag")
            box.setText("Apple Music 권한이 꺼져 있습니다.")
            box.setInformativeText("고친 곡을 Apple Music에 반영하려면 다음을 켜 주세요.\n\n" + "\n".join(off))
            settings_btn = box.addButton("설정 열기", QMessageBox.AcceptRole)
            box.addButton("나중에", QMessageBox.RejectRole)
            never = box.addButton("다시 묻지 않기", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is settings_btn:
                music.open_privacy_settings("automation" if auto == "denied" else "media")
                self.settings.setValue("perm/asked_version", ver)
            elif box.clickedButton() is never:
                self.settings.setValue("perm/never", True)
            return
        items = []
        if need_media:
            items.append("• Apple Music 보관함 접근 — Music에서 곡을 찾고 새로 읽게 합니다")
        if need_auto:
            items.append("• Music 제어 — 고친 곡을 Music에서 새로 읽게 합니다")
        box.setText("Apple Music과 함께 쓰려면 권한이 필요합니다.")
        box.setInformativeText(
            "\n".join(items) + "\n\n"
            "새 버전은 macOS가 다른 앱으로 보기 때문에 버전마다 한 번씩 다시 받아야 합니다. "
            "지금 허용해 두면 작업 도중에 권한 창이 뜨지 않습니다. "
            "'지금 허용하기'를 누르고 뜨는 macOS 권한 창에서 모두 '허용'을 눌러 주세요.")
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

        def job(prog, cancelled):
            if music.media_library_status() == "ask":
                music.request_media_library(cancelled)
            if need_auto:
                music.ensure_access(launch=True)
            return media  # 허용 여부는 앱 안에서 바로 갱신되지 않으므로 처음 상태를 돌려준다

        def done(media_now):
            self.settings.setValue("perm/asked_version", ver)
            if media_now == "denied":
                box = QMessageBox(QMessageBox.Warning, "Apple Music 권한",
                                  "Apple Music 보관함 접근을 허용하지 않았습니다.\n"
                                  "Music에 반영하려면 시스템 설정 → 개인정보 보호 및 보안 → 미디어 및 Apple Music에서 "
                                  "LocalTag를 켜 주세요.", QMessageBox.Ok, self)
                b = box.addButton("설정 열기", QMessageBox.ActionRole)
                box.exec()
                if box.clickedButton() is b:
                    music.open_privacy_settings("media")
                return
            QMessageBox.information(self, "Apple Music 권한", "준비됐습니다. 이제 Music에 바로 반영할 수 있습니다.")

        def fail(msg):
            self.settings.setValue("perm/asked_version", ver)
            self._music_error(msg)
        run_task(self, "Apple Music 권한 확인 · 권한 창이 뜨면 '허용'을 눌러 주세요", job, done, fail)

    # ------------------------------------------------------------ 앨범아트
    def find_artwork(self):
        files = self._files_for_command()
        if not files:
            return
        cache = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        from .artworkdialog import ArtworkDialog  # 인터넷 모듈(ssl 등)은 이때 처음 불러온다
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

    def match_covers(self):
        """곡마다 그림 파일을 직접 골라 커버로 넣는다 (JDownloader로 받은 섬네일 등)."""
        self._confirm_pending()
        files = self.selected_files() or self.visible_files()
        if not files:
            QMessageBox.information(self, APP_NAME, "커버를 넣을 곡을 목록에 먼저 불러오세요.")
            return
        from .covermatch import CoverMatchDialog
        dlg = CoverMatchDialog(self, files, self.settings)
        if not dlg.exec():
            return
        plans = []
        for f, pic in dlg._result:
            pics = list(f.pictures)
            idx = f.primary_index()
            if idx >= 0:
                pics[idx] = pic
            else:
                pics.insert(0, pic)
            plans.append(Plan(f, pictures=pics))
        self.apply_plans(plans, "커버 짝짓기")

    def extract_artwork(self):
        """앨범아트를 이미지 파일로 저장하는 창 (앨범아트 추출기). 창은 하나만 열어 둔다."""
        from .extractdialog import ExtractDialog
        dlg = getattr(self, "_extract_dlg", None)
        if dlg is None:
            dlg = self._extract_dlg = ExtractDialog(self)
        elif dlg.tabs.currentIndex() == 2 and not dlg.job:
            dlg.load_files()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

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
        from .exttags import ExtendedTagsDialog
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
        from .export import ExportDialog
        ExportDialog(self, self.selected_files(), self.visible_files(), self.settings, self.last_dir).exec()

    def make_playlist(self):
        files = self._files_for_command()
        if not files:
            return
        from .export import PlaylistDialog
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
            self.table.selectionModel().clearSelection()  # 지우는 행마다 선택이 바뀌지 않게 먼저 비운다
            self.panel.set_files([])
            self.model.remove(files)
            self._update_status([])

    def play(self):
        files = self.selected_files()
        if files:
            QDesktopServices.openUrl(QUrl.fromLocalFile(files[0].path))

    def reveal(self):
        files = self.selected_files()
        if files:
            reveal(files[0].path)

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
            if e.type() == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
                # 고칠 수 있는 칸이면 그 자리에서 고치고, 나머지 칸(길이·비트레이트 등)은 재생
                ix = self.table.indexAt(e.position().toPoint())
                if ix.isValid() and ix.flags() & Qt.ItemIsEditable:
                    self.table.setCurrentIndex(ix)
                    self.table.edit(ix)
                elif ix.isValid():
                    self.play()
                return True
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
        self.settings.setValue("header_cols", len(COLUMNS))
        self.settings.setValue("splitter", self.splitter.saveState())
        super().closeEvent(e)


RECENT_MAX = 10
ALBUM_KEYS = {"album", "albumartist", "COMPILATION", "disc"}  # Music 이 앨범을 묶는 데 쓰는 값

QUICK_FILTERS = [
    ("커버 없는 곡", "cover MISSING"),
    ("낮은 음질 (192 kbps 미만, 무손실 빼고)", "bitrate LESS 192 AND lossless MISSING"),
    ("손실 압축 (AAC · MP3 · Opus …)", "lossless MISSING"),
    ("무손실 (FLAC · ALAC · WAV …)", "lossless PRESENT"),
    ("AAC 만", "codec IS AAC"),
    (None, None),
    ("제목 없음", "title MISSING"),
    ("아티스트 없음", "artist MISSING"),
    ("앨범 없음", "album MISSING"),
    ("트랙 번호 없음", "track MISSING"),
]


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
    if IS_WIN:
        # 한글·일본어가 섞인 곡명이 고르게 보이도록
        from PySide6.QtGui import QFont
        font = QFont("Malgun Gothic", 9)
        font.setFamilies(["Malgun Gothic", "Yu Gothic UI", "Segoe UI"])
        app.setFont(font)
        app.setWindowIcon(_app_icon())
    w = MainWindow()
    app.window = w
    w.show()
    paths = [p for p in argv[1:] if os.path.exists(p)]
    if paths:
        w.load_paths(paths, replace=True)
    else:
        QTimer.singleShot(0, w.reopen_last)
    return app.exec()
