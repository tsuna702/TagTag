"""YouTube 업로드 순서로 트랙 번호 매기기.

채널(동영상 탭)·재생목록의 영상과 곡을 제목(【】·[] 같은 군더더기를 뺀)과 길이로 짝짓고,
영상이 올라온 날짜·시각 순서대로 번호를 매긴다. 파일 이름의 [영상ID]나 태그 안의 YouTube 주소가 있으면
그 영상을 먼저 쓴다. 날짜는 곡마다 직접 넣거나 영상 링크로 바꿀 수 있다.
인터넷은 '불러오기'를 눌렀을 때만 쓴다.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from . import artwork as art
from . import extract as ex
from .plan import Plan
from .tasks import run_task

SURE = 0.85   # 이보다 낮으면 주황색 (확실하지 않은 짝)
LEAST = 0.6   # 이보다 낮으면 짝짓지 않는다
WEAK = QBrush(QColor(205, 120, 0))
GRAY = QBrush(QColor(140, 140, 140))
TOTALS = {"total": "3/42 (전체 곡 수까지)", "plain": "3"}
MODES = {"all": "① 한 줄로 이어서 — 모든 곡에 트랙 번호 1, 2, 3 … 끝까지",
         "year": "② 연도마다 디스크 — 2018 → 디스크 1, 2019 → 디스크 2 … 그 안에서 트랙 번호 1, 2, 3 …"}
UNDATED = {"keep": "번호 그대로 두기", "end": "맨 뒤에 (지금 순서대로)"}


class OrderError(Exception):
    pass


@dataclass
class Video:
    id: str
    title: str
    seconds: int = 0
    order: int = 0  # 불러온 목록에서의 순서 (채널은 최신이 0)


# 영상 ID → 업로드 시각 (이 Mac 의 시간대) · 앱이 켜져 있는 동안 기억
_DATES: dict[str, datetime] = {}
_INFO: dict[str, Video] = {}


# ------------------------------------------------------------------ 인터넷

def list_videos(text: str) -> list[Video]:
    """주소 여러 개(띄어 쓰기·줄 바꿈)의 영상을 한 목록으로: 채널 · 재생목록 · 영상 하나."""
    out, seen = [], set()

    def add(vid, title, secs):
        if vid not in seen:
            seen.add(vid)
            out.append(Video(vid, title, secs, len(out)))

    words = text.split()
    if not words:
        raise OrderError("채널이나 재생목록 주소를 넣어 주세요.")
    for w in words:
        vid, lst = ex.parse_youtube(w)
        ch = ex.parse_channel(w)
        try:
            if lst:
                for v in ex.youtube_playlist(lst, with_length=True)[1]:
                    add(*v)
            elif ch:
                for v in ex.youtube_channel(ch)[1]:
                    add(*v)
            elif vid:
                v = video_info(vid)
                add(v.id, v.title, v.seconds)
            else:
                raise OrderError(f"YouTube 채널·재생목록·영상 주소가 아닙니다: {w}")
        except (ex.ExtractError, art.ArtworkError) as e:
            raise OrderError(f"{w}\n{e}") from e
    return out


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo else dt


def video_info(vid: str) -> Video:
    """영상 하나의 제목·길이·업로드 시각 (공개 player API, 안 되면 영상 페이지)."""
    if vid in _INFO and vid in _DATES:
        return _INFO[vid]
    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00", "hl": "ko"}},
                       "videoId": vid}).encode()
    title, secs, when = vid, 0, None
    try:
        j = json.loads(art.fetch("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", ex.PAGE_LIMIT,
                                 {**ex.YT_HEADERS, "Content-Type": "application/json"}, body))
        mf = (j.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        vd = j.get("videoDetails") or {}
        title = vd.get("title") or title
        secs = int(vd.get("lengthSeconds") or 0)
        when = _parse_iso(mf.get("publishDate") or mf.get("uploadDate") or "")
    except (art.ArtworkError, ValueError):
        pass
    if when is None:
        try:
            html = art.fetch(f"https://www.youtube.com/watch?v={vid}&hl=ko", ex.PAGE_LIMIT, ex.YT_HEADERS).decode("utf-8", "replace")
        except art.ArtworkError as e:
            raise OrderError(f"영상 {vid} 의 정보를 받지 못했습니다.\n{e}") from e
        m = re.search(r'"(?:publishDate|uploadDate)":"([^"]+)"', html)
        when = _parse_iso(m.group(1)) if m else None
        m = re.search(r'"lengthSeconds":"(\d+)"', html)
        secs = secs or (int(m.group(1)) if m else 0)
    if when is None:
        raise OrderError(f"영상 {vid} 의 업로드 날짜를 찾지 못했습니다 (비공개·삭제된 영상일 수 있음).")
    _DATES[vid] = when
    _INFO[vid] = Video(vid, title, secs)
    return _INFO[vid]


def fetch_dates(vids, progress=lambda n, total: None, cancelled=lambda: False) -> dict[str, str]:
    """아직 모르는 영상의 업로드 시각을 동시에 받아 _DATES 에 넣는다. → {ID: 실패 이유}"""
    todo = [v for v in dict.fromkeys(vids) if v and v not in _DATES]
    failed = {}

    def one(vid):
        if cancelled():
            return
        try:
            video_info(vid)
        except OrderError as e:
            failed[vid] = str(e)

    with ThreadPoolExecutor(6) as pool:
        for n, _ in enumerate(pool.map(one, todo), 1):
            progress(n, len(todo))
    return failed


# ------------------------------------------------------------------ 짝짓기

_BRACKETS = re.compile(r"【[^】]*】|「[^」]*」|『[^』]*』|〔[^〕]*〕|\[[^\]]*\]|\([^)]*\)|（[^）]*）|<[^>]*>|〈[^〉]*〉")
_SPLITS = re.compile(r"\s+[/｜|]\s+|\s+-\s+|／")


def _alnum(s: str) -> str:
    s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", s)).casefold()
    return "".join(c for c in s if c.isalnum())


def variants(title: str) -> set[str]:
    """비교할 제목 조각들: 괄호 속 군더더기를 뺀 제목, ' / '·' - ' 로 나눈 앞뒤 조각."""
    base = _BRACKETS.sub(" ", title or "")
    out = {_alnum(base)} | {_alnum(p) for p in _SPLITS.split(base)}
    out.discard("")
    return out or ({_alnum(title)} - {""})


def file_title(f) -> str:
    return f.get("title") or f.stem


def file_video_id(f) -> str | None:
    """파일 이름의 [영상ID] (yt-dlp 기본 이름) 또는 태그 안의 YouTube 주소."""
    m = re.search(r"\[([\w-]{11})\]", f.stem)
    if m:  # '[EurobeatMix]' 같은 낱말은 아니게 (영상 ID 는 숫자·기호나 대문자가 여기저기 섞여 있다)
        s = m.group(1)
        if re.search(r"[0-9_-]", s) or (sum(c.isupper() for c in s) >= 3 and sum(c.islower() for c in s) >= 2):
            return s
    for vals in f.tags.values():
        for v in vals:
            if isinstance(v, str) and ("youtu" in v):
                for w in v.split():
                    vid, _ = ex.parse_youtube(w)
                    if vid and "youtu" in w:
                        return vid
    return None


def _length_factor(a: float, b: float) -> float:
    if not a or not b:
        return 1.0
    d = abs(a - b)
    return 1.0 if d <= 3 else 0.95 if d <= 10 else 0.85 if d <= 30 else 0.7


def match(files, videos) -> tuple[list, list]:
    """곡마다 (영상 ID 또는 None, 점수). 점수가 높은 짝부터 한 영상에 한 곡씩.
    여러 영상에 되풀이되는 조각('eurobeatremix' 같은 채널 꼬리말)은 비교에서 뺀다."""
    common = Counter(p for v in videos for p in variants(v.title))
    many = {p for p, n in common.items() if n > 3}
    vparts = [(variants(v.title) - many) or variants(v.title) for v in videos]
    cands = []
    for i, f in enumerate(files):
        fp = (variants(file_title(f)) - many) or variants(file_title(f))
        for j, v in enumerate(videos):
            best = 0.0
            for a in fp:
                for b in vparts[j]:
                    if a == b:
                        best = 1.0
                        break
                    sm = difflib.SequenceMatcher(None, a, b)
                    if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
                        best = max(best, sm.ratio())
                if best == 1.0:
                    break
            score = best * _length_factor(f.length, v.seconds)
            if score >= LEAST:
                cands.append((score, i, j))
    pairs, scores = [None] * len(files), [0.0] * len(files)
    used = set()
    for score, i, j in sorted(cands, key=lambda c: (-c[0], c[1], c[2])):
        if pairs[i] is None and j not in used:
            pairs[i], scores[i] = videos[j].id, score
            used.add(j)
    return pairs, scores


# ------------------------------------------------------------------ 번호

def _current_key(f):
    def num(name):
        v = f.get(name).split("/")[0].strip()
        return int(v) if v.isdigit() else 10 ** 6
    return num("disc") if f.get("disc") else 0, num("track"), file_title(f).casefold()


def assign(files, dates, total="total", disc="keep", undated="keep") -> list:
    """곡마다 (디스크, 트랙) — 바꾸지 않을 칸은 None. dates: 곡마다 업로드 시각 또는 None.
    같은 날짜면 올라온 시각, 그것도 같으면 지금 순서."""
    now = sorted(range(len(files)), key=lambda i: _current_key(files[i]))
    rank = {i: k for k, i in enumerate(now)}
    dated = sorted((i for i in range(len(files)) if dates[i]), key=lambda i: (dates[i].replace(tzinfo=None), rank[i]))
    rest = [i for i in now if not dates[i]] if undated == "end" else []
    if disc == "year":
        years = sorted({dates[i].year for i in dated})
        groups = [[i for i in dated if dates[i].year == y] for y in years] + ([rest] if rest else [])
    else:
        groups = [dated + rest]
    out = [(None, None)] * len(files)
    for g, members in enumerate(groups):
        for k, i in enumerate(members):
            track = f"{k + 1}/{len(members)}" if total == "total" else str(k + 1)
            d = None
            if disc == "year":
                d = f"{g + 1}/{len(groups)}" if total == "total" else str(g + 1)
            elif disc == "clear":
                d = ""
            out[i] = (d, track)
    return out


def parse_date(text: str) -> datetime | None:
    """'2019-08-14', '2019.08.14 20:01' 같은 직접 입력."""
    m = re.fullmatch(r"\s*(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*", text or "")
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s).astimezone()
    except ValueError:
        return None


def fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _mmss(secs) -> str:
    secs = int(secs or 0)
    return f"{secs // 60}:{secs % 60:02d}" if secs else ""


# ------------------------------------------------------------------ 창

COLS = ["곡", "짝지은 영상", "확신", "업로드 (직접 입력 가능)", "길이", "지금", "새 디스크", "새 트랙"]


class YtOrderDialog(QDialog):
    """결과: self.plans"""

    def __init__(self, parent, files, settings, mode=None):
        super().__init__(parent)
        self.files = sorted(files, key=_current_key)
        self.settings = settings
        self.videos: list[Video] = []
        n = len(self.files)
        self.pairs = [file_video_id(f) for f in self.files]
        self.scores = [1.0 if p else 0.0 for p in self.pairs]
        self.how = ["파일" if p else "" for p in self.pairs]  # 파일 · 직접 · ''(자동)
        self.manual = [None] * n
        self.rows = list(range(n))
        self.plans = []
        self._filling = False
        self.setWindowTitle("YouTube 업로드 순서로 번호 매기기")
        self.resize(1180, 720)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("채널 · 재생목록 주소:"))
        self.url = QLineEdit(settings.value("ytorder/url", "", str))
        self.url.setPlaceholderText("https://www.youtube.com/@채널 — 재생목록·영상 주소도 되고, 여러 개면 띄어 씁니다")
        self.url.returnPressed.connect(self.load)
        top.addWidget(self.url, 1)
        go = QPushButton("불러오기")
        go.setAutoDefault(False)
        go.clicked.connect(self.load)
        top.addWidget(go)
        v.addLayout(top)
        hint = QLabel("영상 제목(【】·[]·() 속 군더더기와 ' / ' 뒤 꼬리말은 빼고)과 길이로 곡과 영상을 짝짓고, 올라온 날짜·시각 "
                      "순서대로 번호를 매깁니다. 파일 이름의 [영상ID]나 태그 안의 YouTube 주소가 있으면 그 영상을 씁니다. "
                      "확실하지 않은 짝은 주황색 — '짝지은 영상'에서 고치거나, 업로드 칸에 날짜(2019-08-14)나 영상 링크를 "
                      "직접 넣으세요. 시각은 이 컴퓨터의 시간대 기준입니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)

        if mode is None:  # 1.1.4 의 '디스크: 연도마다' 설정을 이어받는다
            old = settings.value("ytorder/disc", "", str)
            mode = settings.value("ytorder/mode", "year" if old == "year" else "all", str)
        box = QGroupBox("번호 매기는 방식")
        bv = QVBoxLayout(box)
        self.modes = QButtonGroup(self)
        for k, text in MODES.items():
            rb = QRadioButton(text)
            rb.setProperty("mode", k)
            rb.setChecked(k == mode)
            self.modes.addButton(rb)
            bv.addWidget(rb)
            if k == "all":
                self.clear_disc = QCheckBox("디스크 번호는 지우기 (연도마다 나눠 두었던 디스크 번호 등)")
                self.clear_disc.setChecked(settings.value("ytorder/clear_disc", True, bool))
                self.clear_disc.toggled.connect(lambda on: (settings.setValue("ytorder/clear_disc", on), self.recompute()))
                indent = QHBoxLayout()
                indent.addSpacing(24)
                indent.addWidget(self.clear_disc)
                indent.addStretch(1)
                bv.addLayout(indent)
        if not self.modes.checkedButton():
            self.modes.buttons()[0].setChecked(True)
        self.modes.buttonToggled.connect(self._mode_changed)
        v.addWidget(box)

        opts = QHBoxLayout()
        self.total = self._combo(opts, "트랙 번호:", TOTALS, "ytorder/total", "total")
        self.undated = self._combo(opts, "날짜 없는 곡:", UNDATED, "ytorder/undated", "keep")
        self.year = QComboBox()
        self.year.addItem("연도: 그대로", False)
        self.year.addItem("연도: 업로드 연도로", True)
        self.year.setCurrentIndex(1 if settings.value("ytorder/year", False, bool) else 0)
        self.year.currentIndexChanged.connect(lambda: (settings.setValue("ytorder/year", self.year.currentData()),
                                                       self.recompute()))
        opts.addWidget(self.year)
        opts.addStretch(1)
        resort = QPushButton("새 순서로 정렬")
        resort.setAutoDefault(False)
        resort.setToolTip("표를 새 디스크·트랙 번호 순서로 다시 늘어놓습니다")
        resort.clicked.connect(self.sort_new)
        opts.addWidget(resort)
        v.addLayout(opts)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        for c, w in enumerate([300, 330, 50, 150, 50, 60, 70, 70]):
            self.table.setColumnWidth(c, w)
        h.setStretchLastSection(True)
        self.table.itemChanged.connect(self._item_changed)
        v.addWidget(self.table, 1)
        self.status = QLabel()
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setText("번호 매기기")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._mode_changed()
        self.fill()

    def mode(self) -> str:
        return self.modes.checkedButton().property("mode")

    def _mode_changed(self, *_):
        self.settings.setValue("ytorder/mode", self.mode())
        self.clear_disc.setEnabled(self.mode() == "all")
        self.setWindowTitle("YouTube 업로드 순서로 번호 매기기 — " + ("한 줄로 이어서" if self.mode() == "all" else "연도마다 디스크"))
        self.recompute()

    def disc_rule(self) -> str:
        if self.mode() == "year":
            return "year"
        return "clear" if self.clear_disc.isChecked() else "keep"

    def _combo(self, row, label, choices, key, default):
        row.addWidget(QLabel(label))
        cb = QComboBox()
        for k, t in choices.items():
            cb.addItem(t, k)
        cb.setCurrentIndex(max(0, cb.findData(self.settings.value(key, default, str))))
        cb.currentIndexChanged.connect(lambda: (self.settings.setValue(key, cb.currentData()), self.recompute()))
        row.addWidget(cb)
        return cb

    # ------------------------------------------------------------ 불러오기
    def load(self):
        text = self.url.text().strip()
        self.settings.setValue("ytorder/url", text)
        have = [p for p, h in zip(self.pairs, self.how) if p and h]  # 파일·직접으로 정한 영상

        def job(prog, cancelled):
            videos = []
            if text:
                prog(0, 0, "영상 목록을 받는 중…")
                videos = list_videos(text)
            auto = [i for i, h in enumerate(self.how) if not h]
            pairs, scores = match([self.files[i] for i in auto], videos)
            want = have + [p for p in pairs if p]
            failed = fetch_dates(want, lambda n, t: prog(n, t, f"업로드 날짜를 받는 중… ({n}/{t})"), cancelled)
            return videos, auto, pairs, scores, failed

        def done(res):
            videos, auto, pairs, scores, failed = res
            self.videos = videos
            for k, i in enumerate(auto):
                self.pairs[i], self.scores[i] = pairs[k], scores[k]
            self.sort_new(refill=False)
            self.fill()
            if failed:
                QMessageBox.warning(self, self.windowTitle(), f"{len(failed)}개 영상의 날짜를 받지 못했습니다:\n\n"
                                    + "\n".join(list(failed.values())[:5]))

        if not text and not have:
            QMessageBox.information(self, self.windowTitle(), "채널이나 재생목록 주소를 넣어 주세요.\n"
                                    "(파일 이름이나 태그에 영상 주소가 있는 곡은 주소 없이도 됩니다)")
            return
        run_task(self, "YouTube 에서 불러오는 중…", job, done,
                 lambda m: QMessageBox.warning(self, self.windowTitle(), m))

    def _fetch_then(self, vids, then):
        """영상 몇 개의 날짜를 받고 then() — 표를 고치는 중에 (창을 막지 않음)."""
        vids = [v for v in vids if v and v not in _DATES]
        if not vids:
            then()
            return
        self.status.setText("업로드 날짜를 받는 중…")

        def done(failed):
            if failed:
                QMessageBox.warning(self, self.windowTitle(), "\n".join(failed.values()))
            then()

        run_task(self, "", lambda prog, cancelled: fetch_dates(vids), done,
                 lambda m: (QMessageBox.warning(self, self.windowTitle(), m), then()), modal=False)

    # ------------------------------------------------------------ 표
    def date_of(self, i):
        return self.manual[i] or _DATES.get(self.pairs[i])

    def video_of(self, vid) -> Video | None:
        return next((v for v in self.videos if v.id == vid), None) or _INFO.get(vid)

    def sort_new(self, refill=True):
        res = self.result()
        self.rows = sorted(range(len(self.files)), key=lambda i: (
            res[i][1] is None, _num(res[i][0]), _num(res[i][1]), _current_key(self.files[i])))
        if refill:
            self.fill()

    def fill(self):
        self._filling = True
        t = self.table
        t.setRowCount(0)
        t.setRowCount(len(self.rows))
        choices = sorted(self.videos, key=lambda x: x.title.casefold())
        for r, i in enumerate(self.rows):
            f = self.files[i]
            it = QTableWidgetItem(file_title(f))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setToolTip(f.path)
            t.setItem(r, 0, it)
            cb = QComboBox()
            cb.addItem("— 짝 없음 —", None)
            vid = self.pairs[i]
            for x in choices:
                cb.addItem(f"{x.title}  ({_mmss(x.seconds)})" if x.seconds else x.title, x.id)
            if vid and cb.findData(vid) < 0:
                x = self.video_of(vid)
                cb.addItem(x.title if x else vid, vid)
            cb.setCurrentIndex(max(0, cb.findData(vid)) if vid else 0)
            cb.currentIndexChanged.connect(lambda _x, i=i, cb=cb: self._pick(i, cb.currentData()))
            t.setCellWidget(r, 1, cb)
            for c in range(2, len(COLS)):
                item = QTableWidgetItem()
                if c != 3:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                t.setItem(r, c, item)
            t.item(r, 4).setText(_mmss(f.length))
            t.item(r, 5).setText("-".join(x for x in (f.get("disc").split("/")[0], f.get("track").split("/")[0]) if x))
        self._filling = False
        self.recompute()

    def _pick(self, i, vid):
        self.pairs[i], self.scores[i], self.how[i] = vid, 1.0 if vid else 0.0, "직접" if vid else ""
        self._fetch_then([vid], self.recompute)

    def _item_changed(self, item):
        if self._filling or item.column() != 3:
            return
        i = self.rows[item.row()]
        text = item.text().strip()
        vid, _ = ex.parse_youtube(text) if not parse_date(text) else (None, None)
        if vid:
            def then():
                if vid in _DATES:
                    self.pairs[i], self.scores[i], self.how[i] = vid, 1.0, "직접"
                    self.manual[i] = None
                    cb = self.table.cellWidget(item.row(), 1)
                    if cb.findData(vid) < 0:
                        x = _INFO.get(vid)
                        cb.addItem(x.title if x else vid, vid)
                    cb.blockSignals(True)
                    cb.setCurrentIndex(cb.findData(vid))
                    cb.blockSignals(False)
                self.recompute()
            self._fetch_then([vid], then)
            return
        self.manual[i] = parse_date(text)  # 비우거나 못 읽으면 영상 날짜로 돌아간다
        self.recompute()

    def result(self):
        return assign(self.files, [self.date_of(i) for i in range(len(self.files))],
                      self.total.currentData(), self.disc_rule(), self.undated.currentData())

    def recompute(self):
        if self._filling or not hasattr(self, "status") or self.table.rowCount() != len(self.rows):
            return
        self._filling = True
        res = self.result()
        weak = undated = 0
        for r, i in enumerate(self.rows):
            t = self.table
            when = self.date_of(i)
            vid = self.pairs[i]
            score = self.how[i] or (f"{round(self.scores[i] * 100)}%" if vid else "")
            t.item(r, 2).setText(score)
            t.item(r, 3).setText(fmt_date(when) + (" (직접)" if self.manual[i] else ""))
            t.item(r, 3).setToolTip(f"https://www.youtube.com/watch?v={vid}" if vid else "")
            d, tr = res[i]
            t.item(r, 6).setText("" if d is None else (d or "(지움)"))
            t.item(r, 7).setText(tr or "")
            for c, new, old in ((6, d, self.files[i].get("disc")), (7, tr, self.files[i].get("track"))):
                fnt = t.item(r, c).font()
                fnt.setBold(new is not None and new != old)  # 바뀌는 칸은 굵게
                t.item(r, c).setFont(fnt)
            is_weak = bool(vid) and not self.how[i] and self.scores[i] < SURE
            weak += is_weak
            undated += not when
            for c in (0, 2):
                t.item(r, c).setForeground(WEAK if is_weak else (GRAY if not when else QBrush()))
        self._filling = False
        n = len(self.files)
        self.status.setText(f"{n}곡 중 날짜 있음 {n - undated}곡 · 확실하지 않은 짝 {weak}곡 (주황) · 날짜 없음 {undated}곡"
                            + (f" · 불러온 영상 {len(self.videos)}개" if self.videos else ""))
        self.ok.setEnabled(undated < n)

    # ------------------------------------------------------------ 적용
    def make_plans(self) -> list[Plan]:
        res = self.result()
        plans = []
        for i, f in enumerate(self.files):
            d, tr = res[i]
            if tr is None:
                continue
            changes = {}
            if f.tags.get("track") != [tr]:
                changes["track"] = [tr]
            if d is not None and f.get("disc") != d:
                changes["disc"] = [d] if d else []
            when = self.date_of(i)
            if self.year.currentData() and when and f.get("year") != str(when.year):
                changes["year"] = [str(when.year)]
            if changes:
                plans.append(Plan(f, changes=changes))
        return plans

    def accept(self):
        self.plans = self.make_plans()
        if not self.plans:
            QMessageBox.information(self, self.windowTitle(), "바뀌는 곡이 없습니다.")
            return
        super().accept()


def _num(s) -> int:
    v = (s or "").split("/")[0].strip()
    return int(v) if v.isdigit() else 0
