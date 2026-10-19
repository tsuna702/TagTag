"""도구 막대 사용자화 — macOS 터치 바·Finder 도구 막대 사용자화처럼 단추 모음(팔레트)에서
실제 도구 막대 위로 끌어다 놓아 넣고, 도구 막대 안에서 끌어 순서를 바꾸고, 밖으로 끌어내 뺀다.

단추 목록(ITEMS)과 기본 배치(DEFAULT)만 여기 있고, 실제 동작은 MainWindow 의 메서드를 부른다.
설정 `toolbar_items` 에 키 목록으로 저장한다 ('|' 구분선, '~' 늘어나는 공간).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .osutil import IS_MAC, REVEAL_LABEL, keys

SEP = "|"
SPACE = "~"  # 늘어나는 공간 (뒤의 단추를 오른쪽 끝으로 민다)
DEFAULT_SET = "@default"  # 팔레트의 '기본 세트' — 놓으면 전부 기본 배치로
MIME = "application/x-localtag-toolbar"


def _conv(i):
    return lambda w: w.run_converter(__import__("localtag.converters", fromlist=["CONVERTERS"]).CONVERTERS[i][2])


# 키 → (단추 글자, 동작(MainWindow → None), 풍선 도움말, 켜지는 조건)
# 조건: None 늘 · "files" 목록에 곡이 있을 때 · "sel" 고른 곡이 있을 때 · "dirty" · "undo" · "redo" (MainWindow 가 따로)
ITEMS = {
    "save": ("저장", lambda w: w.save(), keys("바꾼 태그 저장 (⌘S)"), "dirty"),
    "undo": ("되돌리기", lambda w: w.undo(), keys("마지막 작업 되돌리기 (⌘Z)"), "undo"),
    "redo": ("다시 실행", lambda w: w.redo(), keys("되돌린 작업 다시 하기 (⇧⌘Z)"), "redo"),
    "open": ("폴더 열기…", lambda w: w.open_folder(), keys("폴더 열기 (⌘O)"), None),
    "add_folder": ("폴더 추가…", lambda w: w.add_folder(), keys("목록에 폴더 더하기 (⇧⌘O)"), None),
    "refresh": ("새로고침", lambda w: w.refresh(), "목록의 파일을 다시 읽기 (F5)", "files"),
    "clear": ("목록 지우기", lambda w: w.clear_list(), "목록 비우기 — 파일은 지우지 않습니다", "files"),
    "remove": ("목록에서 제거", lambda w: w.remove_from_list(), "고른 곡을 목록에서 빼기 — 파일은 그대로", "sel"),
    "reveal": (REVEAL_LABEL, lambda w: w.reveal(), keys(f"선택한 파일을 {REVEAL_LABEL} (⇧⌘R)"), "sel"),
    "play": ("재생", lambda w: w.play(), keys("고른 곡 재생 (⌘P)"), "sel"),
    "up": ("↑ 위로", lambda w: w.move_selection(-1), keys("선택한 곡을 한 칸 위로 (⌥⌘↑)"), "sel"),
    "down": ("↓ 아래로", lambda w: w.move_selection(1), keys("선택한 곡을 한 칸 아래로 (⌥⌘↓)"), "sel"),
    "rename": ("일괄 변경…", lambda w: w.batch_rename(),
               keys("곡명·파일명을 규칙으로 한꺼번에 바꾸기 (⌥9) — 선택한 곡, 선택이 없으면 목록 전체"), "files"),
    "number": ("트랙 번호…", lambda w: w.number_tracks(),
               "목록에 보이는 순서대로 트랙 번호 매기기 — 선택한 곡, 선택이 없으면 목록 전체", "files"),
    "autonumber": ("자동 번호…", lambda w: w.run_converter(
        __import__("localtag.converters", fromlist=["AutoNumber"]).AutoNumber), keys("자동 번호 매기기 (⌘K)"), "sel"),
    "tag2file": ("태그 → 파일명", _conv(0), keys("태그로 파일명 만들기 (⌥1)"), "sel"),
    "file2tag": ("파일명 → 태그", _conv(1), keys("파일명에서 태그 읽기 (⌥2)"), "sel"),
    "file2file": ("파일명 → 파일명", _conv(2), keys("파일명 고치기 (⌥3)"), "sel"),
    "text2tag": ("텍스트 → 태그", _conv(3), keys("텍스트를 줄마다 태그로 (⌥4)"), "sel"),
    "tag2tag": ("태그 → 태그", _conv(4), keys("태그로 다른 태그 만들기 (⌥5)"), "sel"),
    "tag_source": ("태그 가져오기…", lambda w: w.tag_source(), keys("Apple Music · MusicBrainz 에서 태그 가져오기 (⇧⌘G)"), "sel"),
    "exttags": ("확장 태그…", lambda w: w.extended_tags(), keys("모든 필드 보기·고치기 (⌘T)"), "sel"),
    "remove_tags": ("태그 제거…", lambda w: w.remove_tags(), "고른 곡의 태그 지우기", "sel"),
    "compare": ("태그 비교…", lambda w: w.compare_tags(), keys("고른 곡 나란히 비교 (⇧⌘D)"), "sel"),
    "dupes": ("같은 곡 찾기…", lambda w: w.find_duplicates(), keys("같은 곡 · 여러 버전 찾기 (⇧⌘U)"), "files"),
    "albumcheck": ("앨범 점검…", lambda w: w.check_albums(), keys("Music 에서 갈라지는 앨범 찾기 (⇧⌘L)"), "files"),
    "artwork": ("앨범아트 찾기…", lambda w: w.find_artwork(), keys("앨범아트 찾기 · 가져오기 (⇧⌘A)"), None),
    "match_covers": ("커버 짝짓기…", lambda w: w.match_covers(), keys("그림 파일을 곡마다 (⇧⌘J)"), None),
    "resize_covers": ("커버 크기…", lambda w: w.resize_covers(), keys("커버 크기 조정 (⇧⌘K)"), "sel"),
    "extract": ("앨범아트 추출…", lambda w: w.extract_artwork(), keys("링크 · 보관함 · 목록 · YouTube (⇧⌘X)"), None),
    "actions": ("액션 그룹…", lambda w: w.run_action_groups(), keys("액션 그룹 실행 (⌥6)"), "sel"),
    "quick_action": ("빠른 액션…", lambda w: w.quick_action(), keys("액션 하나 바로 실행 (⌥7)"), "sel"),
    "youtube": ("YouTube 제목 정리…", lambda w: w.youtube_clean(), keys("YouTube 꼬리말 떼기 (⌥8)"), "files"),
    "convert": ("형식 변환…", lambda w: w.convert_formats(), "FLAC 등을 ALAC · AAC 로", "sel"),
    "export": ("내보내기…", lambda w: w.export_list(), keys("CSV · HTML · 텍스트 (⌘E)"), "files"),
    "playlist": ("재생목록…", lambda w: w.make_playlist(), keys("M3U8 재생목록 만들기 (⌘L)"), "files"),
    "filter": ("필터", lambda w: w.focus_filter(), keys("필터 입력칸으로 (⌘F)"), None),
    "backups": ("백업…", lambda w: w.show_backups(), keys("원본 백업 보기 (⇧⌘B)"), None),
}
if IS_MAC:
    ITEMS.update({
        "music_refresh": ("Music 반영", lambda w: w.music_refresh(), keys("고른 곡을 Music 이 새로 읽게 (⇧⌘M)"), "sel"),
        "music_add": ("Music에서 추가", lambda w: w.music_add_selection(), keys("Music 에서 고른 곡을 목록에 (⇧⌘I)"), None),
        "library_check": ("보관함 점검…", lambda w: w.library_check(), "Music 보관함의 커버 없음 · 갈라진 앨범 · 파일 없음", None),
    })

# 단추 모음 창의 분류 (여기 없는 단추는 '기타'로)
CATEGORIES = [
    ("기본", ["save", "undo", "redo", "refresh", "clear", "open", "add_folder", "remove"]),
    ("목록 · 재생", ["reveal", "play", "up", "down", "filter"]),
    ("태그 고치기", ["rename", "number", "autonumber", "tag_source", "exttags", "remove_tags"]),
    ("변환", ["tag2file", "file2tag", "file2file", "text2tag", "tag2tag", "convert"]),
    ("커버", ["artwork", "match_covers", "resize_covers", "extract"]),
    ("점검 · 정리", ["compare", "dupes", "albumcheck", "youtube"]),
    ("액션", ["actions", "quick_action"]),
    ("내보내기 · 백업", ["export", "playlist", "backups"]),
    ("Apple Music", ["music_refresh", "music_add", "library_check"]),
]


def categories() -> list:
    """(분류 이름, [이 OS 에 있는 단추 키]) — 빈 분류는 뺀다."""
    seen, out = set(), []
    for name, ks in CATEGORIES:
        ks = [k for k in ks if k in ITEMS]
        seen.update(ks)
        if ks:
            out.append((name, ks))
    rest = [k for k in ITEMS if k not in seen]
    if rest:
        out.append(("기타", rest))
    return out


DEFAULT = ["save", "undo", "redo", SEP, "refresh", "clear", SEP, "reveal", SEP, "up", "down", "rename", "number"]


def load(settings) -> list[str]:
    v = settings.value("toolbar_items")
    if v is None:
        return list(DEFAULT)
    if isinstance(v, str):
        v = [v] if v else []
    return [k for k in v if k in (SEP, SPACE) or k in ITEMS]


def label_of(key: str) -> str:
    return {SEP: "구분선", SPACE: "늘어나는 공간", DEFAULT_SET: "기본 세트"}.get(key) or ITEMS[key][0]


# ------------------------------------------------------------------ 배치 바꾸기 (순수 함수)

def place(items, key, src, dst) -> list:
    """key 를 dst 자리(그 앞)에 넣는다. src 는 도구 막대 안에서 옮길 때의 원래 자리 (팔레트에서 오면 None).
    같은 단추가 이미 있으면 옮긴다 (구분선·공간은 여러 개 둘 수 있다)."""
    items = list(items)
    if key == DEFAULT_SET:
        return list(DEFAULT)
    if src is not None and 0 <= src < len(items):
        key = items.pop(src)
        if dst > src:
            dst -= 1
    elif key not in (SEP, SPACE) and key in items:
        i = items.index(key)
        items.pop(i)
        if dst > i:
            dst -= 1
    items.insert(max(0, min(dst, len(items))), key)
    return items


def tidy(items) -> list:
    """앞뒤·연달아 있는 구분선을 정리한다."""
    out = []
    for k in items:
        if k == SEP and (not out or out[-1] == SEP):
            continue
        out.append(k)
    while out and out[-1] == SEP:
        out.pop()
    return out


def _mime(key, src=None) -> QMimeData:
    m = QMimeData()
    m.setData(MIME, f"{key}\n{'' if src is None else src}".encode())
    return m


def _read(mime):
    if not mime.hasFormat(MIME):
        return None
    key, _, src = bytes(mime.data(MIME)).decode().partition("\n")
    return key, (int(src) if src else None)


def _start_drag(source, key, src=None):
    drag = QDrag(source)
    drag.setMimeData(_mime(key, src))
    pm = source.grab()
    drag.setPixmap(pm)
    drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
    return drag.exec(Qt.MoveAction | Qt.CopyAction, Qt.MoveAction)




class Customizer(QObject):
    """사용자화 모드: 팔레트 창을 띄우고, 도구 막대를 끌어다 놓기 받는 상태로 바꾼다."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.orig = load(win.settings)
        self.items = list(self.orig)
        self.handled = False
        self.press = None
        self.indicator = None
        self.palette = Palette(self)

    # ---- 시작 · 끝
    def start(self):
        self.win._customizer = self
        self.win._fill_toolbar(self.items)
        tb = self.win.toolbar
        self.palette.show()
        g = tb.mapToGlobal(QPoint(0, tb.height()))
        self.palette.move(g.x() + 40, g.y() + 20)
        self.palette.raise_()

    def finish(self, save=True):
        if self.win._customizer is not self:
            return
        self.win._customizer = None
        if save:
            self.win.settings.setValue("toolbar_items", tidy(self.items))
        self.win._fill_toolbar()
        self.palette.hide()
        self.palette.deleteLater()

    def rebuild(self):
        if self.win._customizer is self:
            self.win._fill_toolbar(self.items)

    # ---- 도구 막대
    def attach(self, tb):
        """새로 만든 도구 막대를 끌어다 놓기 받게 한다 (단추 누르기는 막고 끌기로)."""
        tb.setAcceptDrops(True)
        tb.installEventFilter(self)
        self.indicator = QFrame(tb)
        self.indicator.setFixedWidth(3)
        self.indicator.setStyleSheet("background: palette(highlight); border-radius: 1px;")
        self.indicator.hide()
        for i, w in enumerate(self.slot_widgets(tb)):
            w.setProperty("tb_index", i)
            w.installEventFilter(self)

    def slot_widgets(self, tb):
        return [tb.widgetForAction(a) for a in tb.actions() if tb.widgetForAction(a) is not None]

    def index_at(self, x) -> int:
        """도구 막대의 x 위치 → 넣을 자리 (보이는 칸의 가운데를 기준으로)."""
        ws = self.slot_widgets(self.win.toolbar)
        for i, w in enumerate(ws):
            if w.isVisible() and x < w.geometry().center().x():
                return i
        return len(ws)

    def _show_indicator(self, i):
        tb = self.win.toolbar
        ws = self.slot_widgets(tb)
        if i < len(ws) and ws[i].isVisible():
            x = ws[i].geometry().left() - 3
        elif ws:
            x = max((w.geometry().right() for w in ws if w.isVisible()), default=4) + 2
        else:
            x = 4
        self.indicator.setGeometry(max(0, x), 4, 3, max(8, tb.height() - 8))
        self.indicator.show()
        self.indicator.raise_()

    def eventFilter(self, obj, ev):
        t = ev.type()
        tb = self.win.toolbar
        if obj is tb:
            if t in (QEvent.DragEnter, QEvent.DragMove) and _read(ev.mimeData()):
                ev.setDropAction(Qt.MoveAction)
                ev.accept()
                self._show_indicator(self.index_at(ev.position().toPoint().x()))
                return True
            if t == QEvent.DragLeave:
                if self.indicator:
                    self.indicator.hide()
                return True
            if t == QEvent.Drop and _read(ev.mimeData()):
                key, src = _read(ev.mimeData())
                self.items = place(self.items, key, src, self.index_at(ev.position().toPoint().x()))
                self.handled = True
                ev.setDropAction(Qt.MoveAction)
                ev.accept()
                if self.indicator:
                    self.indicator.hide()
                QTimer.singleShot(0, self.rebuild)  # 끄는 중인 단추를 지금 지우지 않게
                return True
            return False
        idx = obj.property("tb_index") if isinstance(obj, QWidget) else None
        if idx is None:
            return False
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            self.press = (obj, ev.position().toPoint())
            return True
        if t == QEvent.MouseMove and self.press and self.press[0] is obj:
            if (ev.position().toPoint() - self.press[1]).manhattanLength() >= QApplication.startDragDistance():
                self.press = None
                self.drag_from_bar(obj, int(idx))
            return True
        if t in (QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            self.press = None
            return True  # 사용자화하는 동안 단추는 눌리지 않는다
        return False

    def drag_from_bar(self, widget, idx):
        if not 0 <= idx < len(self.items):
            return
        self.handled = False
        _start_drag(widget, self.items[idx], idx)
        if not self.handled:  # 도구 막대 밖(팔레트 포함)에 놓음 → 뺀다
            self.items = self.items[:idx] + self.items[idx + 1:]
        QTimer.singleShot(0, self.rebuild)


class _PaletteItem(QPushButton):
    """팔레트의 단추 — 누르면 맨 끝에 넣고, 끌면 원하는 자리에."""

    def __init__(self, cust, key):
        super().__init__(label_of(key))
        self.cust, self.key = cust, key
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(ITEMS[key][2] if key in ITEMS else {
            SEP: "단추 사이에 세로선", SPACE: "뒤의 단추를 오른쪽 끝으로 미는 빈칸",
            DEFAULT_SET: "도구 막대에 놓으면 처음 배치로 돌아갑니다"}[key])
        self._press = None

    def mousePressEvent(self, e):
        self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.position().toPoint() - self._press).manhattanLength() >= \
                QApplication.startDragDistance():
            self._press = None
            self.setDown(False)
            _start_drag(self, self.key)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        clicked = self._press is not None and self.rect().contains(e.position().toPoint())
        self._press = None
        super().mouseReleaseEvent(e)
        if clicked:  # 그냥 누르면 맨 끝에
            c = self.cust
            c.items = place(c.items, self.key, None, len(c.items))
            c.rebuild()


class Palette(QDialog):
    """단추 모음. 도구 막대에서 끌어와 여기에 놓아도 빠진다."""

    COLUMNS = 4

    def __init__(self, cust):
        super().__init__(cust.win)
        self.cust = cust
        self.setWindowTitle("도구 막대 사용자화")
        self.setModal(False)
        self.setAcceptDrops(True)
        self.resize(720, 620)
        v = QVBoxLayout(self)
        hint = QLabel("원하는 단추를 <b>도구 막대로 끌어다 놓으세요</b>. 도구 막대의 단추를 끌어 순서를 바꾸고, "
                      "도구 막대 밖으로 끌어내면 빠집니다. 단추를 그냥 누르면 맨 끝에 들어갑니다.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        inner = QWidget()
        g = QGridLayout(inner)
        g.setSpacing(6)
        row = 0
        for name, ks in categories():
            head = QLabel(f"<b>{name}</b>")
            head.setContentsMargins(0, 8 if row else 0, 0, 0)
            g.addWidget(head, row, 0, 1, self.COLUMNS)
            row += 1
            for i, key in enumerate(ks):
                g.addWidget(_PaletteItem(cust, key), row + i // self.COLUMNS, i % self.COLUMNS)
            row += (len(ks) + self.COLUMNS - 1) // self.COLUMNS
        g.setRowStretch(row, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        extra = QHBoxLayout()
        for key in (SEP, SPACE):
            extra.addWidget(_PaletteItem(cust, key))
        extra.addStretch(1)
        extra.addWidget(QLabel("…또는 기본 세트를 끌어다 놓기:"))
        extra.addWidget(_PaletteItem(cust, DEFAULT_SET))
        v.addLayout(extra)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(lambda: cust.finish(save=False))
        done = QPushButton("완료")
        done.setDefault(True)
        done.clicked.connect(lambda: cust.finish(save=True))
        row.addWidget(cancel)
        row.addWidget(done)
        v.addLayout(row)

    def dragEnterEvent(self, e):
        r = _read(e.mimeData())
        if r and r[1] is not None:  # 도구 막대에서 끌어온 것만 (빼기)
            e.acceptProposedAction()

    def dropEvent(self, e):
        e.acceptProposedAction()  # 빼기는 끈 쪽(drag_from_bar)이 한다

    def closeEvent(self, e):
        self.cust.finish(save=True)  # 창을 닫으면 '완료'와 같다
        super().closeEvent(e)
