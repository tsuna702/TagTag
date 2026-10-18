"""Apple Music 에서 태그 가져오기 창 (MusicBrainz 로 보충).

앨범을 검색해 고르면 곡 목록을 내려받아 고른 곡과 자동으로 짝짓는다 (제목·트랙 번호·길이).
짝은 곡마다 콤보 상자로 직접 고칠 수 있다. 체크한 필드만, 바뀌는 곡에만 적용한다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import tagsource as ts
from .model import fmt_length
from .plan import Plan
from .tasks import run_task

FIELDS = [("title", "제목"), ("artist", "아티스트"), ("album", "앨범"), ("albumartist", "앨범 아티스트"),
          ("year", "연도"), ("genre", "장르"), ("track", "트랙 번호"), ("disc", "디스크 번호"),
          ("composer", "작곡가"), ("COPYRIGHT", "저작권"), ("PUBLISHER", "레이블"), ("ISRC", "ISRC"),
          ("BARCODE", "바코드 (UPC)"), ("COMPILATION", "컴필레이션"), ("date", "연도에 발매일 전체 (2014-05-16)")]
# 처음 보는 필드(새로 생긴 것)는 켠 채로 시작한다 — 발매일 전체는 끈 채로
OFF_BY_DEFAULT = {"date"}
COVER_SIZES = [600, 1000, 1500, 3000]
WEAK = QBrush(QColor(205, 120, 0))
GRAY = QBrush(QColor(140, 140, 140))
PREVIEW = 110  # 커버 미리보기 크기 (px)


def _current_key(f):
    """지금 곡의 정렬 순서: 디스크 → 트랙 번호 → 제목."""
    def num(name):
        v = f.get(name).split("/")[0].strip()
        return int(v) if v.isdigit() else 10 ** 6
    return num("disc") if f.get("disc") else 0, num("track"), (f.get("title") or f.stem).casefold()


class TagSourceDialog(QDialog):
    """결과: self.plans"""

    def __init__(self, parent, files, settings):
        super().__init__(parent)
        # 처음에는 지금 디스크·트랙 번호 순으로 (오름차순). 머리글을 눌러 다른 칸으로 정렬한다
        self.files, self.settings = sorted(files, key=_current_key), settings
        self.sort_col, self.sort_desc = 0, False
        self.rel = None
        self.pairs = [None] * len(files)
        self.scores = [0.0] * len(files)
        self.plans = []
        self._cache = {}
        self.setWindowTitle("Apple Music · MusicBrainz 에서 태그 가져오기")
        self.resize(1180, 720)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.query = QLineEdit(ts.guess_query(files))
        self.query.setPlaceholderText("아티스트 앨범 이름, 또는 Apple Music · MusicBrainz 앨범 링크")
        self.query.returnPressed.connect(self.search)
        self.country = QComboBox()
        for k, t in ts.COUNTRIES.items():
            self.country.addItem(t, k)
        self.country.setCurrentIndex(max(0, self.country.findData(settings.value("source/country", "kr", str))))
        self.country.setToolTip("이 스토어의 이름으로 가져옵니다 — 검색 결과가 없으면 일본·미국 스토어에서 찾은 뒤 이 스토어 이름으로 바꿉니다")
        self.use_mb = QCheckBox("MusicBrainz")
        self.use_mb.setChecked(settings.value("source/musicbrainz", True, bool))
        self.use_mb.setToolTip("Apple Music 결과 뒤에 MusicBrainz 결과를 붙입니다 — 애니·게임 OST 처럼 Apple Music 에 없는 앨범")
        go = QPushButton("검색")
        go.setAutoDefault(False)
        go.clicked.connect(self.search)
        top.addWidget(QLabel("검색:"))
        top.addWidget(self.query, 1)
        top.addWidget(self.country)
        top.addWidget(self.use_mb)
        top.addWidget(go)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.results = QTableWidget(0, 6)
        self.results.setHorizontalHeaderLabels(["앨범", "아티스트", "연도", "곡 수", "나라", "출처"])
        self.results.verticalHeader().hide()
        self.results.verticalHeader().setDefaultSectionSize(24)
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results.setSelectionBehavior(QTableWidget.SelectRows)
        self.results.setSelectionMode(QTableWidget.SingleSelection)
        self.results.setWordWrap(False)
        rh = self.results.horizontalHeader()
        rh.setSectionResizeMode(QHeaderView.Interactive)  # 칸 경계를 끌어 너비 조정
        rh.setStretchLastSection(True)
        rh.setMinimumSectionSize(30)
        for c, w in enumerate((230, 110, 46, 46, 56, 50)):
            self.results.setColumnWidth(c, w)
        state = settings.value("source/results_header")
        if state is not None:
            rh.restoreState(state)
        self.results.currentCellChanged.connect(lambda row, *_: self._pick_release(self._release_row(row)))
        split.addWidget(self.results)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW, PREVIEW)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid rgba(128,128,128,0.4); color: gray;")
        self.preview.setToolTip("고른 앨범의 커버")
        self.album_label = QLabel("왼쪽에서 앨범을 고르세요.")
        self.album_label.setWordWrap(True)
        self.album_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        head.addWidget(self.preview)
        head.addWidget(self.album_label, 1)
        rv.addLayout(head)
        self._previews = {}
        self.table = QTableWidget(len(files), 7)
        self.table.setHorizontalHeaderLabels(["곡 (지금)", "짝지은 Apple Music 곡", "새 제목", "새 아티스트", "새 번호", "맞음",
                                              "작곡가"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(0, Qt.AscendingOrder)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._header_clicked)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for c, w in enumerate((230, 260, 170, 120, 60, 50)):
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
        split.setSizes([520, 660])
        v.addWidget(split, 1)

        box = QGroupBox("넣을 정보")
        g = QGridLayout(box)
        allrow = QHBoxLayout()
        allrow.addStretch(1)
        for text, on in (("모두 선택", True), ("모두 해제", False)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, on=on: self.check_all(on))
            allrow.addWidget(b)
        rows = (len(FIELDS) + 3) // 4
        g.addLayout(allrow, rows, 3)
        saved = settings.value("source/fields", [k for k, _ in FIELDS if k not in OFF_BY_DEFAULT] + ["cover"])
        if isinstance(saved, str):
            saved = [saved]
        known = settings.value("source/fields_known", [k for k, _ in FIELDS[:8]] + ["cover"])
        if isinstance(known, str):
            known = [known]
        self.checks = {}
        for i, (k, t) in enumerate(FIELDS):
            cb = QCheckBox(t)
            cb.setChecked(k in saved or (k not in known and k not in OFF_BY_DEFAULT))
            cb.toggled.connect(self._refresh_rows)
            g.addWidget(cb, i // 4, i % 4)
            self.checks[k] = cb
        self.totals = QCheckBox("총수도 넣기 (3/12)")
        self.totals.setChecked(settings.value("source/totals", True, bool))
        self.totals.toggled.connect(self._refresh_rows)
        g.addWidget(self.totals, rows, 0)
        self.cover = QCheckBox("커버")
        self.cover.setChecked("cover" in saved)
        g.addWidget(self.cover, rows, 1)
        self.cover_size = QComboBox()
        for s in COVER_SIZES:
            self.cover_size.addItem(f"{s} × {s}" + (" (가장 큰 크기)" if s == 3000 else ""), s)
        self.cover_size.setCurrentIndex(max(0, self.cover_size.findData(settings.value("source/cover_size", 1000, int))))
        g.addWidget(self.cover_size, rows, 2)
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

        self._fill_current()
        self._refresh_rows()

    def _fill_current(self):
        for i, f in enumerate(self.files):
            it = QTableWidgetItem(f"{f.get('title') or f.stem}  ({fmt_length(f.length)})")
            it.setToolTip(f.path)
            self.table.setItem(i, 0, it)

    def done(self, r):
        self.settings.setValue("source/results_header", self.results.horizontalHeader().saveState())
        super().done(r)

    # ------------------------------------------------------------ 검색
    def search(self):
        term, country, mb = self.query.text(), self.country.currentData(), self.use_mb.isChecked()
        self.settings.setValue("source/country", country)
        self.settings.setValue("source/musicbrainz", mb)
        run_task(self, "찾는 중…", lambda prog, cancelled: ts.find(term, country, mb),
                 self._show_results, self._failed)

    def _failed(self, msg):
        self.album_label.setText(f"<span style='color:#c82828'>⚠ {msg}</span>")

    def _show_results(self, releases):
        self.releases = releases
        t = self.results
        t.setSortingEnabled(False)
        t.clearContents()
        t.setRowCount(len(releases))
        for i, r in enumerate(releases):
            cells = (r.album, r.artist, int(r.year) if r.year.isdigit() else r.year, r.count or "",
                     r.country_name(), "MB" if r.source == "mb" else "Apple")
            for c, val in enumerate(cells):
                it = QTableWidgetItem()
                it.setData(Qt.DisplayRole, val)
                if c >= 2:
                    it.setTextAlignment(Qt.AlignCenter)
                it.setToolTip(r.label())
                it.setData(Qt.UserRole, i)
                t.setItem(i, c, it)
        t.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)  # 처음엔 검색 순서 그대로
        t.setSortingEnabled(True)  # 머리글을 눌러 연도·곡 수로 정렬
        if not releases:
            self.album_label.setText("찾은 앨범이 없습니다. 검색어를 바꾸거나 Apple Music 앨범 링크를 붙여 넣어 보세요.")
        else:
            t.setCurrentCell(0, 0)

    def _release_row(self, row):
        it = self.results.item(row, 0)
        return it.data(Qt.UserRole) if it else -1

    def _pick_release(self, row):
        if not (0 <= row < len(getattr(self, "releases", []))):
            return
        r = self.releases[row]
        key = (r.source, r.id, "" if r.source == "mb" else self.country.currentData())
        if key in self._cache:
            self._set_release(self._cache[key])
            return
        run_task(self, "곡 목록을 받는 중…", lambda prog, cancelled: ts.get(r, key[2]),
                 lambda rel: (self._cache.__setitem__(key, rel), self._set_release(rel)), self._failed)

    def _set_release(self, rel):
        self.rel = rel
        bits = " · ".join(x for x in (rel.year, rel.genre, f"곡 {len(rel.tracks)}개", ts.SOURCES[rel.source]) if x)
        nocover = (" · 커버 없음" if not rel.artwork else
                   f" · <span style='color:gray'>커버: {rel.artwork_from}</span>" if rel.artwork_from else "")
        more = " · ".join(x for x in (rel.date, rel.record_label, "컴필레이션" if rel.compilation else "") if x)
        if rel.details:
            more = (more + " · " if more else "") + f"<span style='color:#cd7800'>{rel.details}</span>"
        self.album_label.setText(f"<b>{rel.album}</b> — {rel.artist} · {bits}{nocover}"
                                 + (f"<br><span style='color:gray'>{more}</span>" if more else ""))
        self._show_preview(rel)
        self._build_combos()
        self.pair_auto()

    def _build_combos(self):
        rel = self.rel
        for i in range(len(self.files)):
            cb = QComboBox()
            cb.addItem("— 짝 없음 —", None)
            for j, t in enumerate(rel.tracks):
                cb.addItem(t.label(), j)
            j = self.pairs[i] if i < len(self.pairs) else None
            cb.setCurrentIndex(0 if j is None else j + 1)
            cb.currentIndexChanged.connect(lambda _x, i=i, cb=cb: self._set_pair(i, cb.currentData(), manual=True))
            self.table.setCellWidget(i, 1, cb)

    # ------------------------------------------------------------ 정렬
    def _header_clicked(self, col):
        desc = (not self.sort_desc) if col == self.sort_col else False
        self.sort_rows(col, desc)

    def sort_rows(self, col: int, desc: bool = False):
        """곡 표를 col 칸으로 정렬 (짝·점수도 함께 옮긴다). 값이 없는 줄은 늘 맨 아래."""
        def track(i):
            j = self.pairs[i] if self.rel else None
            return self.rel.tracks[j] if j is not None else None

        def key(i):
            t = track(i)
            if col == 0:
                return _current_key(self.files[i])
            if col == 5:
                return self.scores[i] if t else None
            if t is None:
                return None
            return {1: (t.disc, t.num), 4: (t.disc, t.num), 2: t.title.casefold(), 3: t.artist.casefold(),
                    6: t.composer.casefold()}[col]

        idx = list(range(len(self.files)))
        have = sorted((i for i in idx if key(i) is not None), key=key, reverse=desc)
        order = have + [i for i in idx if key(i) is None]
        self.sort_col, self.sort_desc = col, desc
        self.table.horizontalHeader().setSortIndicator(col, Qt.DescendingOrder if desc else Qt.AscendingOrder)
        if order == idx:
            return
        self.files = [self.files[i] for i in order]
        self.pairs = [self.pairs[i] for i in order]
        self.scores = [self.scores[i] for i in order]
        self._fill_current()
        if self.rel:
            self._build_combos()
        self._refresh_rows()

    def _show_preview(self, rel):
        """커버 미리보기 — 작은 그림을 뒤에서 받는다 (실패해도 조용히)."""
        if not rel.artwork:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("커버 없음")
            return
        url = ts.cover_url(rel, 250)
        if url in self._previews:
            self._set_preview(self._previews[url])
            return
        self.preview.setPixmap(QPixmap())
        self.preview.setText("…")
        self._preview_url = url

        def done(data):
            self._previews[url] = data
            if self._preview_url == url:  # 그 사이 다른 앨범을 고르지 않았으면
                self._set_preview(data)

        def failed(_msg):
            if self._preview_url == url:
                self.preview.setText("커버를\n못 받음")

        from . import artwork as art
        run_task(self, "", lambda prog, cancelled: art.fetch(url, 5 * 1024 * 1024), done, failed, modal=False)

    def _set_preview(self, data):
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self.preview.setPixmap(pm.scaled(PREVIEW, PREVIEW, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.preview.setToolTip(f"고른 앨범의 커버 (미리보기 {pm.width()} × {pm.height()})")
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("커버를\n못 받음")

    def check_all(self, on: bool):
        for cb in list(self.checks.values()) + [self.totals, self.cover]:
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        self._refresh_rows()

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
                cells = ["", "", "", "—", ""]
            else:
                t = self.rel.tracks[j]
                n_pair += 1
                num = f"{t.disc}-{t.num:02d}" if t.discs > 1 else f"{t.num:02d}"
                ch = ts.changes_for(f, self.rel, t, fields, self.totals.isChecked())
                n_change += bool(ch) or self.cover.isChecked()
                mark = "겹침" if used[j] > 1 else ("✓" if self.scores[i] >= 0.8 else f"{int(self.scores[i] * 100)}%")
                cells = [t.title, t.artist, num, mark, t.composer]
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
            self.summary.setText(f"곡 {len(self.files)}개 중 {n_pair}개 짝지음 · {ts.SOURCES[self.rel.source]} 곡 {len(self.rel.tracks)}개{extra}")
        self.ok_btn.setEnabled(bool(self.rel) and n_pair > 0)

    # ------------------------------------------------------------ 적용
    def _confirm(self, text) -> bool:
        """적용하기 전에 한 번 더 묻는다 (테스트에서 바꿔 끼운다)."""
        box = QMessageBox(QMessageBox.Question, "태그 가져오기", text, parent=self)
        ok = box.addButton("적용", QMessageBox.AcceptRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(ok)
        box.exec()
        return box.clickedButton() is ok

    def _remember(self):
        self.settings.setValue("source/fields", sorted(self.fields()) + (["cover"] if self.cover.isChecked() else []))
        self.settings.setValue("source/fields_known", [k for k, _ in FIELDS] + ["cover"])
        self.settings.setValue("source/totals", self.totals.isChecked())
        self.settings.setValue("source/cover_size", self.cover_size.currentData())

    def apply(self):
        if not self.rel:
            return
        rel, fields, totals = self.rel, self.fields(), self.totals.isChecked()
        want_cover = self.cover.isChecked() and bool(rel.artwork)
        paired = [(f, rel.tracks[j]) for f, j in zip(self.files, self.pairs) if j is not None]
        n = sum(1 for f, t in paired if want_cover or ts.changes_for(f, rel, t, fields, totals))
        names = [t for k, t in FIELDS if k in fields] + (["커버"] if want_cover else [])
        if not n:
            self._failed("바뀌는 곡이 없습니다 — 넣을 정보를 확인해 주세요.")
            return
        if not self._confirm(f"{n}곡에 적용할까요?\n\n앨범: {rel.album} — {rel.artist}\n넣을 정보: {', '.join(names)}"):
            return
        self._remember()
        size = self.cover_size.currentData()

        def job(prog, cancelled):
            pic = None
            if want_cover:
                from . import artwork as art
                data = art.fetch(ts.cover_url(rel, size), 30 * 1024 * 1024)
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
