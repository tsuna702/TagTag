"""Apple Music 에서 태그 가져오기 창.

앨범을 검색해 고르면 곡 목록을 내려받아 고른 곡과 자동으로 짝짓는다 (제목·트랙 번호·길이).
짝은 곡마다 콤보 상자로 직접 고칠 수 있다. 체크한 필드만, 바뀌는 곡에만 적용한다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import tagsource as ts
from .model import fmt_length
from .plan import Plan
from .tasks import run_task

FIELDS = [("title", "제목"), ("artist", "아티스트"), ("album", "앨범"), ("albumartist", "앨범 아티스트"),
          ("year", "연도"), ("genre", "장르"), ("track", "트랙 번호"), ("disc", "디스크 번호")]
COVER_SIZES = [600, 1000, 1500, 3000]
WEAK = QBrush(QColor(205, 120, 0))
GRAY = QBrush(QColor(140, 140, 140))


class TagSourceDialog(QDialog):
    """결과: self.plans"""

    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.files, self.settings = files, settings
        self.rel = None
        self.pairs = [None] * len(files)
        self.scores = [0.0] * len(files)
        self.plans = []
        self._cache = {}
        self.setWindowTitle("Apple Music 에서 태그 가져오기")
        self.resize(1180, 720)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.query = QLineEdit(ts.guess_query(files))
        self.query.setPlaceholderText("아티스트 앨범 이름, 또는 Apple Music 앨범 링크")
        self.query.returnPressed.connect(self.search)
        self.country = QComboBox()
        for k, t in ts.COUNTRIES.items():
            self.country.addItem(t, k)
        self.country.setCurrentIndex(max(0, self.country.findData(settings.value("source/country", "kr", str))))
        self.country.setToolTip("이 스토어의 이름으로 가져옵니다 — 검색 결과가 없으면 일본·미국 스토어에서 찾은 뒤 이 스토어 이름으로 바꿉니다")
        go = QPushButton("검색")
        go.setAutoDefault(False)
        go.clicked.connect(self.search)
        top.addWidget(QLabel("검색:"))
        top.addWidget(self.query, 1)
        top.addWidget(self.country)
        top.addWidget(go)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.results = QListWidget()
        self.results.currentRowChanged.connect(self._pick_release)
        split.addWidget(self.results)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.album_label = QLabel("왼쪽에서 앨범을 고르세요.")
        self.album_label.setWordWrap(True)
        rv.addWidget(self.album_label)
        self.table = QTableWidget(len(files), 6)
        self.table.setHorizontalHeaderLabels(["곡 (지금)", "짝지은 Apple Music 곡", "새 제목", "새 아티스트", "새 번호", "맞음"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for c, w in enumerate((230, 260, 170, 120, 60)):
            self.table.setColumnWidth(c, w)
        hh.setStretchLastSection(True)
        rv.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, slot, tip in (("자동으로 짝짓기", self.pair_auto, "제목·트랙 번호·길이가 비슷한 곡끼리"),
                                ("순서대로 짝짓기", self.pair_ordered, "목록 순서 그대로 1번 곡 ↔ 1번 트랙 …"),
                                ("짝 모두 풀기", self.pair_clear, "")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            row.addWidget(b)
        hint = QLabel("곡마다 '짝지은 곡' 칸에서 직접 고를 수 있습니다. 주황색은 자신 없는 짝·겹친 짝.")
        hint.setStyleSheet("color: gray;")
        row.addWidget(hint, 1)
        rv.addLayout(row)
        split.addWidget(right)
        split.setSizes([330, 850])
        v.addWidget(split, 1)

        box = QGroupBox("넣을 정보")
        g = QGridLayout(box)
        saved = settings.value("source/fields", [k for k, _ in FIELDS] + ["cover"])
        if isinstance(saved, str):
            saved = [saved]
        self.checks = {}
        for i, (k, t) in enumerate(FIELDS):
            cb = QCheckBox(t)
            cb.setChecked(k in saved)
            cb.toggled.connect(self._refresh_rows)
            g.addWidget(cb, i // 4, i % 4)
            self.checks[k] = cb
        self.totals = QCheckBox("총수도 넣기 (3/12)")
        self.totals.setChecked(settings.value("source/totals", True, bool))
        self.totals.toggled.connect(self._refresh_rows)
        g.addWidget(self.totals, 2, 0)
        self.cover = QCheckBox("커버")
        self.cover.setChecked("cover" in saved)
        g.addWidget(self.cover, 2, 1)
        self.cover_size = QComboBox()
        for s in COVER_SIZES:
            self.cover_size.addItem(f"{s} × {s}" + (" (가장 큰 크기)" if s == 3000 else ""), s)
        self.cover_size.setCurrentIndex(max(0, self.cover_size.findData(settings.value("source/cover_size", 1000, int))))
        g.addWidget(self.cover_size, 2, 2)
        v.addWidget(box)

        self.summary = QLabel()
        v.addWidget(self.summary)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("적용")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        for i, f in enumerate(files):
            it = QTableWidgetItem(f"{f.get('title') or f.stem}  ({fmt_length(f.length)})")
            it.setToolTip(f.path)
            self.table.setItem(i, 0, it)
        self._refresh_rows()

    # ------------------------------------------------------------ 검색
    def search(self):
        term, country = self.query.text(), self.country.currentData()
        self.settings.setValue("source/country", country)
        run_task(self, "Apple Music 에서 찾는 중…", lambda prog, cancelled: ts.search(term, country),
                 self._show_results, self._failed)

    def _failed(self, msg):
        self.album_label.setText(f"<span style='color:#c82828'>⚠ {msg}</span>")

    def _show_results(self, releases):
        self.releases = releases
        self.results.clear()
        for r in releases:
            it = QListWidgetItem(r.label())
            it.setToolTip(r.label())
            self.results.addItem(it)
        if not releases:
            self.album_label.setText("찾은 앨범이 없습니다. 검색어를 바꾸거나 Apple Music 앨범 링크를 붙여 넣어 보세요.")
        else:
            self.results.setCurrentRow(0)

    def _pick_release(self, row):
        if not (0 <= row < len(getattr(self, "releases", []))):
            return
        r = self.releases[row]
        key = (r.id, self.country.currentData())
        if key in self._cache:
            self._set_release(self._cache[key])
            return
        run_task(self, "곡 목록을 받는 중…", lambda prog, cancelled: ts.lookup(r.id, key[1]),
                 lambda rel: (self._cache.__setitem__(key, rel), self._set_release(rel)), self._failed)

    def _set_release(self, rel):
        self.rel = rel
        self.album_label.setText(f"<b>{rel.album}</b> — {rel.artist} · {rel.year} · {rel.genre} · 곡 {len(rel.tracks)}개")
        for i in range(len(self.files)):
            cb = QComboBox()
            cb.addItem("— 짝 없음 —", None)
            for j, t in enumerate(rel.tracks):
                cb.addItem(t.label(), j)
            cb.currentIndexChanged.connect(lambda _x, i=i, cb=cb: self._set_pair(i, cb.currentData(), manual=True))
            self.table.setCellWidget(i, 1, cb)
        self.pair_auto()

    # ------------------------------------------------------------ 짝짓기
    def _apply_pairs(self, pairs):
        if not self.rel:
            return
        self.pairs = list(pairs)
        self.scores = [ts.score(f, self.rel.tracks[j]) if j is not None else 0.0 for f, j in zip(self.files, pairs)]
        for i, j in enumerate(self.pairs):
            cb = self.table.cellWidget(i, 1)
            cb.blockSignals(True)
            cb.setCurrentIndex(0 if j is None else j + 1)
            cb.blockSignals(False)
        self._refresh_rows()

    def pair_auto(self):
        if self.rel:
            self._apply_pairs(ts.auto_pairs(self.files, self.rel.tracks))

    def pair_ordered(self):
        if self.rel:
            self._apply_pairs(ts.ordered_pairs(self.files, self.rel.tracks))

    def pair_clear(self):
        if self.rel:
            self._apply_pairs([None] * len(self.files))

    def _set_pair(self, i, j, manual=False):
        self.pairs[i] = j
        # 직접 고른 짝은 사용자가 확인한 것으로 본다
        self.scores[i] = 1.0 if manual and j is not None else 0.0
        self._refresh_rows()

    def fields(self) -> set:
        return {k for k, cb in self.checks.items() if cb.isChecked()}

    def _refresh_rows(self, *_):
        used = {}
        for j in self.pairs:
            if j is not None:
                used[j] = used.get(j, 0) + 1
        fields = self.fields()
        n_pair = n_change = 0
        for i, f in enumerate(self.files):
            j = self.pairs[i] if self.rel else None
            if j is None:
                cells = ["", "", "", "—"]
            else:
                t = self.rel.tracks[j]
                n_pair += 1
                num = f"{t.disc}-{t.num:02d}" if t.discs > 1 else f"{t.num:02d}"
                ch = ts.changes_for(f, self.rel, t, fields, self.totals.isChecked())
                n_change += bool(ch) or self.cover.isChecked()
                mark = "겹침" if used[j] > 1 else ("✓" if self.scores[i] >= 0.8 else f"{int(self.scores[i] * 100)}%")
                cells = [t.title, t.artist, num, mark]
            for c, text in enumerate(cells, start=2):
                it = QTableWidgetItem(text)
                if j is None:
                    it.setForeground(GRAY)
                elif used[j] > 1 or self.scores[i] < 0.8:
                    it.setForeground(WEAK)
                self.table.setItem(i, c, it)
        if not self.rel:
            self.summary.setText(f"곡 {len(self.files)}개 — 앨범을 검색해 고르세요.")
        else:
            dup = sum(1 for n in used.values() if n > 1)
            extra = f" · <span style='color:#cd7800'>겹친 짝 {dup}개</span>" if dup else ""
            self.summary.setText(f"곡 {len(self.files)}개 중 {n_pair}개 짝지음 · Apple Music 곡 {len(self.rel.tracks)}개{extra}")
        self.ok_btn.setEnabled(bool(self.rel) and n_pair > 0)

    # ------------------------------------------------------------ 적용
    def _remember(self):
        self.settings.setValue("source/fields", sorted(self.fields()) + (["cover"] if self.cover.isChecked() else []))
        self.settings.setValue("source/totals", self.totals.isChecked())
        self.settings.setValue("source/cover_size", self.cover_size.currentData())

    def apply(self):
        if not self.rel:
            return
        self._remember()
        rel, fields, totals = self.rel, self.fields(), self.totals.isChecked()
        want_cover = self.cover.isChecked() and bool(rel.artwork)
        size = self.cover_size.currentData()

        def job(prog, cancelled):
            pic = None
            if want_cover:
                from . import artwork as art
                from .extract import hi_res
                data = art.fetch(hi_res(rel.artwork, size), 30 * 1024 * 1024)
                pic = art.process(data)
            return pic

        def done(pic):
            plans = []
            for f, j in zip(self.files, self.pairs):
                if j is None:
                    continue
                p = Plan(f, changes=ts.changes_for(f, rel, rel.tracks[j], fields, totals))
                if pic is not None:
                    pics = list(f.pictures)
                    idx = f.primary_index()
                    if idx >= 0:
                        pics[idx] = pic
                    else:
                        pics.insert(0, pic)
                    p.pictures = pics
                if p.touches_tags:
                    plans.append(p)
            self.plans = plans
            self.accept()

        if want_cover:
            run_task(self, "커버를 받는 중…", job, done, self._failed)
        else:
            done(None)
