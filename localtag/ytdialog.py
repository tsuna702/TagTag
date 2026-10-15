"""YouTube 제목 정리 창: 옵션을 바꾸면 바로 미리 보고, 여러 곡을 한 번에 적용한다.

나누기가 거꾸로 된 줄은 골라서 아티스트·제목을 뒤바꾸고, 건드리지 않을 줄은 빼 둔다.
"""
from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from .actions import DEFAULTS, YT_ORDERS
from .converters import ConverterDialog
from .osutil import keys
from .plan import Plan
from .ytclean import clean, compile_rx

SPLIT_EXAMPLES = [
    "",
    r"^(?<title>.+?) / (?<artist>.+)$",
    r"^(?<artist>.+?)\s*[「『](?<title>.+?)[」』]",
    r"^(?<title>.+?)\s*[-–]\s*(?<artist>.+?)\s*(?:cover|歌ってみた)",
]
REMOVE_EXAMPLES = [
    "",
    r"\(Ateam Arrange\)",
    r"【[^】]*(?:切り抜き|ショート|short)[^】]*】",
    r"#\S+",
]


class YtCleanDialog(ConverterDialog):
    title = "YouTube 제목 정리"
    description = ("제목(없으면 파일명)에서 【MV】, (Official Music Video), [4K], (128kbit_AAC) 같은 꼬리말을 떼고 "
                   "'아티스트 - 제목'을 나눕니다. 옵션을 바꾸면 아래 미리 보기가 바로 바뀝니다. "
                   "거꾸로 나뉜 줄은 골라서 ↔ 뒤바꾸기, 건드리지 않을 줄은 빼 두기.")
    ok_text = "모두 적용"

    def build(self):
        self.flipped, self.skipped = set(), set()
        self.split = self.add_check("제목에서 아티스트를 떼어 아티스트 칸으로", "yt_split", True)
        self.order = QComboBox()
        for k, t in YT_ORDERS.items():
            self.order.addItem(t, k)
        self.order.setCurrentIndex(max(0, self.order.findData(self.settings.value("conv/yt_order", "artist_title", str))))
        self.order.currentIndexChanged.connect(lambda _: self.settings.setValue("conv/yt_order", self.order.currentData()))
        self.order.currentIndexChanged.connect(self.schedule)
        self.form.addRow("제목 모양:", self.order)
        self.overwrite = self.add_check("이미 있는 아티스트(채널 이름 등)도 바꾸기", "yt_overwrite", True)
        self.keep_cover = self.add_check("'歌ってみた'·'Cover' 같은 커버곡 표시는 남기기", "yt_keep_cover", True)
        self.remove = self.add_format("더 지울 말 (정규식):", "yt_remove", REMOVE_EXAMPLES)
        self.remove.lineEdit().setPlaceholderText(r"예: \(Ateam Arrange\)|#\S+   — 여러 개는 | 로 잇기, 대소문자 무시")
        self.split_rx = self.add_format("나누기 정규식:", "yt_split_rx", SPLIT_EXAMPLES)
        self.split_rx.lineEdit().setPlaceholderText(
            "예: ^(?<title>.+?) / (?<artist>.+)$   — 맞는 곡은 기본 규칙보다 먼저, 이름 없는 그룹은 1·2번을 '제목 모양' 순서로")
        row = QHBoxLayout()
        flip = QPushButton("↔ 고른 줄 아티스트·제목 뒤바꾸기")
        flip.setShortcut(QKeySequence("Ctrl+R"))
        flip.setToolTip(keys("표에서 고른 줄의 새 아티스트와 새 제목을 맞바꿉니다 (⌘R)"))
        flip.clicked.connect(lambda: self._toggle(self.flipped))
        skip = QPushButton("고른 줄 빼기 / 다시 넣기")
        skip.setShortcut(QKeySequence("Ctrl+D"))
        skip.setToolTip(keys("고른 줄은 적용하지 않습니다. 한 번 더 누르면 다시 넣습니다 (⌘D)"))
        skip.clicked.connect(lambda: self._toggle(self.skipped))
        for b in (flip, skip):
            b.setAutoDefault(False)
            row.addWidget(b)
        row.addStretch(1)
        self.form.addRow("고른 줄:", row)
        hint = QLabel("정규식 문법은 파이썬 re — (?<이름>…) 과 (?P<이름>…) 모두 됩니다.")
        hint.setStyleSheet("color: gray;")
        self.form.addRow("", hint)

    def action(self) -> dict:
        """지금 옵션을 액션으로 (액션 그룹에 넣는 'YouTube 제목 정리' 와 같은 모양)."""
        a = {"type": "youtube"}
        a.update(DEFAULTS["youtube"])
        a.update(split=self.split.isChecked(), order=self.order.currentData(),
                 overwrite_artist=self.overwrite.isChecked(), keep_cover=self.keep_cover.isChecked(),
                 remove_regex=self.remove.currentText(), split_regex=self.split_rx.currentText())
        return a

    def headers(self):
        return ["원래 제목 (없으면 파일명)", "새 아티스트", "새 제목", "상태"]

    def make_plans(self):
        a = self.action()
        remove_rx = compile_rx(a["remove_regex"], "더 지울 말")
        split_rx = compile_rx(a["split_regex"], "나누기")
        plans = []
        for f in self.files:
            src = f.get("title") or f.stem
            title, artist = clean(src, f.get("artist"), split=a["split"], keep_cover=a["keep_cover"],
                                  overwrite_artist=a["overwrite_artist"], order=a["order"],
                                  remove_rx=remove_rx, split_rx=split_rx)
            status = ""
            if id(f) in self.flipped and artist:
                title, artist = artist, title
                status = "↔ 뒤바꿈"
            changes = {}
            if title and [title] != f.tags.get("title"):
                changes["title"] = [title]
            if artist and [artist] != f.tags.get("artist"):
                changes["artist"] = [artist]
            p = Plan(f, changes=changes, cells=[src, artist, title])
            if id(f) in self.skipped:
                p.changes, p.unchanged = {}, True
                p.cells = [src, f.get("artist"), f.get("title"), "빼 둠"]
            else:
                p.unchanged = not changes
                if status and changes:
                    p.cells.append(status)
            plans.append(p)
        return plans

    def _toggle(self, bucket):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        picked = {id(self.plans[r].file) for r in rows if r < len(self.plans)}
        if not picked:
            return
        if picked <= bucket:
            bucket -= picked
        else:
            bucket |= picked
        self.refresh()
        sm = self.table.selectionModel()
        for r, p in enumerate(self.plans):
            if id(p.file) in picked:
                sm.select(self.table.model().index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def remember(self):
        self.remove.remember()
        self.split_rx.remember()
        self.settings.setValue("quick/youtube", self.action())
