"""도구 막대 사용자화 — 넣을 단추와 순서를 사용자가 고른다 (Mac 터치 바 사용자화처럼 끌어다 놓기).

단추 목록(ITEMS)과 기본 배치(DEFAULT)만 여기 있고, 실제 동작은 MainWindow 의 메서드를 부른다.
설정 `toolbar_items` 에 키 목록으로 저장한다 ('|' 는 구분선).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QGridLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout,
)

from .osutil import IS_MAC, REVEAL_LABEL, keys

SEP = "|"


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

DEFAULT = ["save", "undo", "redo", SEP, "refresh", "clear", SEP, "reveal", SEP, "up", "down", "rename", "number"]


def load(settings) -> list[str]:
    v = settings.value("toolbar_items")
    if v is None:
        return list(DEFAULT)
    if isinstance(v, str):
        v = [v] if v else []
    return [k for k in v if k == SEP or k in ITEMS]


def label_of(key: str) -> str:
    return "──── 구분선 ────" if key == SEP else ITEMS[key][0]


class ToolbarDialog(QDialog):
    """결과: self.items (키 목록)"""

    def __init__(self, parent, current: list[str]):
        super().__init__(parent)
        self.setWindowTitle("도구 막대 사용자화")
        self.resize(640, 520)
        v = QVBoxLayout(self)
        hint = QLabel("왼쪽 단추를 오른쪽으로 끌어다 놓거나 더블클릭해 넣고, 오른쪽에서 끌어 순서를 바꿉니다. "
                      "오른쪽에서 더블클릭하거나 왼쪽으로 끌어내면 뺍니다.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        g = QGridLayout()
        g.addWidget(QLabel("넣을 수 있는 단추"), 0, 0)
        g.addWidget(QLabel("도구 막대 (위에서부터 왼쪽 → 오른쪽)"), 0, 2)
        self.pool = _List(self, source=True)
        self.bar = _List(self, source=False)
        g.addWidget(self.pool, 1, 0)
        mid = QVBoxLayout()
        mid.addStretch(1)
        for text, slot in (("넣기 →", self.add_selected), ("← 빼기", self.remove_selected),
                           ("구분선 넣기", self.add_separator), ("위로", lambda: self.move(-1)),
                           ("아래로", lambda: self.move(1))):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            mid.addWidget(b)
        mid.addStretch(1)
        g.addLayout(mid, 1, 1)
        g.addWidget(self.bar, 1, 2)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(2, 1)
        v.addLayout(g, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.button(QDialogButtonBox.Ok).setText("확인")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.button(QDialogButtonBox.RestoreDefaults).setText("기본값으로")
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(lambda: self.set_items(DEFAULT))
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.pool.itemDoubleClicked.connect(lambda it: self.add_key(it.data(Qt.UserRole)))
        self.bar.itemDoubleClicked.connect(lambda it: self._take(self.bar.row(it)))
        self.set_items(current)

    # ---- 목록
    def _item(self, key):
        it = QListWidgetItem(label_of(key))
        it.setData(Qt.UserRole, key)
        if key != SEP:
            it.setToolTip(ITEMS[key][2])
        return it

    def set_items(self, keys_):
        self.bar.clear()
        for k in keys_:
            self.bar.addItem(self._item(k))
        self._refresh_pool()

    def items(self) -> list[str]:
        return [self.bar.item(i).data(Qt.UserRole) for i in range(self.bar.count())]

    def _refresh_pool(self):
        used = set(self.items())
        self.pool.clear()
        for k in ITEMS:
            if k not in used:
                self.pool.addItem(self._item(k))

    def add_key(self, key, row=None):
        if key != SEP and key in self.items():
            return
        cur = self.bar.currentRow()
        row = row if row is not None else (cur + 1 if cur >= 0 else self.bar.count())
        self.bar.insertItem(row, self._item(key))
        self.bar.setCurrentRow(row)
        self._refresh_pool()

    def add_selected(self):
        for it in self.pool.selectedItems():
            self.add_key(it.data(Qt.UserRole))

    def add_separator(self):
        self.add_key(SEP)

    def _take(self, row):
        if 0 <= row < self.bar.count():
            self.bar.takeItem(row)
            self._refresh_pool()

    def remove_selected(self):
        for it in self.bar.selectedItems():
            self._take(self.bar.row(it))

    def move(self, step):
        row = self.bar.currentRow()
        to = row + step
        if row < 0 or not 0 <= to < self.bar.count():
            return
        it = self.bar.takeItem(row)
        self.bar.insertItem(to, it)
        self.bar.setCurrentRow(to)

    def _accept(self):
        # 앞뒤·연달아 있는 구분선은 정리한다
        out = []
        for k in self.items():
            if k == SEP and (not out or out[-1] == SEP):
                continue
            out.append(k)
        while out and out[-1] == SEP:
            out.pop()
        self.result_items = out
        self.accept()


class _List(QListWidget):
    """끌어다 놓기: 왼쪽(source) ↔ 오른쪽(도구 막대). 오른쪽 안에서는 순서 바꾸기."""

    def __init__(self, dlg, source):
        super().__init__()
        self.dlg, self.source = dlg, source
        self.setSelectionMode(QAbstractItemView.ExtendedSelection if source else QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, e):
        src = e.source()
        e.setDropAction(Qt.IgnoreAction)  # 목록은 직접 고친다 (Qt 의 기본 복사·이동은 쓰지 않음)
        e.accept()
        if not isinstance(src, _List):
            return
        d = self.dlg
        if self.source:  # 도구 막대 → 왼쪽: 빼기
            if src is d.bar:
                d.remove_selected()
            return
        idx = self.indexAt(e.position().toPoint())
        row = idx.row() if idx.isValid() else self.count()
        if src is d.pool:
            for i, it in enumerate(src.selectedItems()):
                d.add_key(it.data(Qt.UserRole), row + i)
        elif src is self:  # 순서 바꾸기
            cur = self.currentRow()
            if cur < 0 or row in (cur, cur + 1):
                return
            it = self.takeItem(cur)
            self.insertItem(row - 1 if row > cur else row, it)
            self.setCurrentRow(row - 1 if row > cur else row)
