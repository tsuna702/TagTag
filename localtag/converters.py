"""변환기 대화상자 (Mp3tag의 Convert 메뉴)."""
from __future__ import annotations

import os
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QSpinBox, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .plan import Plan
from .script import HELP_HTML, Context, Pattern, ScriptError, evaluate, split_values
from .tagio import FIELD_LABELS, FIELDS, SEP, AudioFile, nfc

PREVIEW_LIMIT = 1000
HISTORY_MAX = 20
ERROR_BRUSH = QBrush(QColor(200, 40, 40))
SAME_BRUSH = QBrush(QColor(140, 140, 140))


# ------------------------------------------------------------------ 파일명 만들기

_WIN_INVALID = '*?"<>|'


def make_value_filter(win_compat: bool):
    """필드 값 안의 경로 구분자 등은 폴더가 되지 않도록 바꾼다 (AC/DC → AC_DC)."""
    def f(s: str) -> str:
        s = s.replace("/", "_").replace("\\", "_").replace(":", "_")
        if win_compat:
            s = "".join("_" if c in _WIN_INVALID else c for c in s)
        return s
    return f


def _clean_component(c: str, win_compat: bool) -> str:
    c = "".join(ch for ch in c if ch >= " ")  # 제어 문자 제거
    c = c.replace(":", "_")
    if win_compat:
        c = "".join("_" if ch in _WIN_INVALID else ch for ch in c)
        c = c.rstrip(". ")
    c = c.strip()
    # 파일 시스템 이름 길이 제한 (255바이트)
    while len(c.encode("utf-8")) > 250:
        c = c[:-1]
    return c


def build_target_path(af: AudioFile, result: str, win_compat: bool) -> str:
    """포맷 결과로 새 전체 경로를 만든다. '/' 나 '\\' 는 폴더 구분."""
    absolute = result.startswith("/")
    parts = [p for p in re.split(r"[\\/]", result)]
    if absolute:
        parts = parts[1:]
    cleaned = [_clean_component(nfc(p), win_compat) for p in parts]
    if not cleaned or any(c in ("", ".", "..") for c in cleaned):
        raise ValueError("빈 파일명 또는 폴더 이름이 생깁니다")
    base = "/" if absolute else os.path.dirname(af.path)
    ext = os.path.splitext(af.path)[1]
    return os.path.join(base, *cleaned[:-1], cleaned[-1] + ext)


def display_path(af: AudioFile, path: str) -> str:
    folder = os.path.dirname(af.path)
    rel = os.path.relpath(path, folder)
    return nfc(rel if not rel.startswith("..") else path)


def check_conflicts(plans):
    """같은 이름으로 바뀌는 파일, 이미 있는 다른 파일과 겹치는 경우 오류 표시."""
    seen = {}
    for p in plans:
        if p.error or not p.new_path:
            continue
        key = nfc(p.new_path).casefold()  # macOS 기본 파일 시스템은 대소문자 구분 없음
        if key in seen:
            p.error = "다른 파일과 이름이 겹침"
            seen[key].error = seen[key].error or "다른 파일과 이름이 겹침"
            continue
        seen[key] = p
        if os.path.exists(p.new_path) and not os.path.samefile(p.new_path, p.file.path):
            p.error = "같은 이름의 파일이 이미 있음"


def normalize_numbered(s: str) -> str:
    """Mp3tag식 %1, %2 를 %1%, %2% 로."""
    return re.sub(r"%(\d+)%?", r"%\1%", s)


# ------------------------------------------------------------------ 공통 대화상자

class HelpDialog(QDialog):
    _instance = None

    @classmethod
    def show_help(cls, parent):
        if cls._instance is None:
            cls._instance = HelpDialog(parent)
        cls._instance.show()
        cls._instance.raise_()

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("포맷 문자열 도움말")
        self.resize(620, 720)
        v = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setHtml(HELP_HTML)
        v.addWidget(tb)


class FormatCombo(QComboBox):
    """기록이 남는 포맷 문자열 입력칸."""

    def __init__(self, settings, key, defaults):
        super().__init__()
        self.settings, self.key = settings, f"history/{key}"
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumContentsLength(40)
        hist = settings.value(self.key, [])
        if isinstance(hist, str):
            hist = [hist]
        items = list(dict.fromkeys(list(hist) + list(defaults)))
        self.addItems(items)
        self.setEditText(items[0] if items else "")

    def remember(self):
        text = self.currentText()
        hist = [text] + [self.itemText(i) for i in range(self.count()) if self.itemText(i) != text]
        self.settings.setValue(self.key, hist[:HISTORY_MAX])


class ConverterDialog(QDialog):
    title = ""
    description = ""
    ok_text = "확인"

    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.files = files
        self.settings = settings
        self.plans: list[Plan] = []
        self.setWindowTitle(self.title)
        self.resize(900, 600)

        self.layout_ = QVBoxLayout(self)
        if self.description:
            d = QLabel(self.description)
            d.setWordWrap(True)
            self.layout_.addWidget(d)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.layout_.addLayout(self.form)
        self.build()

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.layout_.addWidget(self.table, 1)

        self.summary = QLabel()
        self.layout_.addWidget(self.summary)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        self.ok_btn.setText(self.ok_text)
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        help_btn = bb.addButton("도움말", QDialogButtonBox.HelpRole)
        help_btn.clicked.connect(lambda: HelpDialog.show_help(self))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.layout_.addWidget(bb)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    # 하위 클래스에서 구현
    def build(self):
        pass

    def headers(self) -> list[str]:
        return []

    def make_plans(self) -> list[Plan]:
        return []

    def remember(self):
        pass

    # 공통
    def add_format(self, label, key, defaults):
        cb = FormatCombo(self.settings, key, defaults)
        cb.editTextChanged.connect(self.schedule)
        self.form.addRow(label, cb)
        return cb

    def add_check(self, text, key, default):
        cb = QCheckBox(text)
        cb.setChecked(self.settings.value(f"conv/{key}", default, bool))
        cb.toggled.connect(lambda on: self.settings.setValue(f"conv/{key}", on))
        cb.toggled.connect(self.schedule)
        self.form.addRow("", cb)
        return cb

    def schedule(self, *_):
        self.timer.start()

    def refresh(self):
        try:
            self.plans = self.make_plans()
            err = None
        except (ScriptError, ValueError) as e:
            self.plans, err = [], str(e)
        headers = self.headers()
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        shown = self.plans[:PREVIEW_LIMIT]
        self.table.setRowCount(len(shown))
        for r, p in enumerate(shown):
            cells = list(p.cells)
            if p.error:
                cells.append("⚠ " + p.error)
            elif p.unchanged:
                cells.append("변경 없음")
            for c, text in enumerate(cells[:len(headers)]):
                item = QTableWidgetItem(text)
                if p.error:
                    item.setForeground(ERROR_BRUSH)
                elif p.unchanged:
                    item.setForeground(SAME_BRUSH)
                self.table.setItem(r, c, item)
        hh = self.table.horizontalHeader()
        for c in range(len(headers) - 1):
            hh.setSectionResizeMode(c, QHeaderView.Interactive)
            self.table.setColumnWidth(c, max(120, min(320, 860 // max(len(headers), 1))))
        if err:
            self.summary.setText(f"<span style='color:#c82828'>⚠ {err}</span>")
            self.ok_btn.setEnabled(False)
            return
        n_ok = sum(1 for p in self.plans if p.actionable)
        n_err = sum(1 for p in self.plans if p.error)
        n_same = sum(1 for p in self.plans if p.unchanged)
        parts = [f"파일 {len(self.files)}개", f"바뀜 {n_ok}개"]
        if n_same:
            parts.append(f"변경 없음 {n_same}개")
        if n_err:
            parts.append(f"<span style='color:#c82828'>오류 {n_err}개 (건너뜀)</span>")
        if len(self.plans) > PREVIEW_LIMIT:
            parts.append(f"미리보기는 처음 {PREVIEW_LIMIT}개만 표시")
        self.summary.setText(" · ".join(parts))
        self.ok_btn.setEnabled(n_ok > 0)

    def accept(self):
        self.timer.stop()
        self.refresh()
        if not any(p.actionable for p in self.plans):
            return
        self.remember()
        super().accept()

    def result_plans(self):
        return [p for p in self.plans if p.actionable]


# ------------------------------------------------------------------ 태그 → 파일명

class TagToFilename(ConverterDialog):
    title = "변환: 태그 → 파일명"
    description = ("태그로 파일 이름을 만듭니다. 확장자는 그대로 둡니다. "
                   "'/' 를 넣으면 폴더를 만들어 옮깁니다 (예: %artist%/%album%/%title%).")
    ok_text = "이름 바꾸기"

    def build(self):
        self.fmt = self.add_format("포맷:", "tag2file", [
            "$num(%track%,2) %title%",
            "%artist% - %title%",
            "$num(%track%,2). %artist% - %title%",
            "[%disc%-]$num(%track%,2) %title%",
        ])
        self.win = self.add_check("Windows에서 쓸 수 없는 문자(* ? \" < > |)도 바꾸기", "win_compat", False)

    def headers(self):
        return ["현재 파일명", "새 파일명", "상태"]

    def make_plans(self):
        fmt = self.fmt.currentText()
        if not fmt.strip():
            raise ValueError("포맷 문자열을 입력하세요")
        vf = make_value_filter(self.win.isChecked())
        plans = []
        for i, f in enumerate(self.files):
            p = Plan(f, cells=[f.filename])
            try:
                result = evaluate(fmt, Context(f, i, len(self.files), value_filter=vf))
                new = build_target_path(f, result, self.win.isChecked())
                p.cells.append(display_path(f, new))
                if new == f.path:
                    p.unchanged = True
                else:
                    p.new_path = new
            except ValueError as e:
                p.cells.append("")
                p.error = str(e)
            plans.append(p)
        check_conflicts(plans)
        return plans

    def remember(self):
        self.fmt.remember()


# ------------------------------------------------------------------ 파일명 → 태그

class FilenameToTag(ConverterDialog):
    title = "변환: 파일명 → 태그"
    description = ("파일 이름을 패턴에 맞춰 잘라서 태그에 넣습니다. 필요 없는 부분은 %dummy% 로 건너뜁니다. "
                   "'/' 를 넣으면 상위 폴더 이름도 씁니다 (예: %artist%/%album%/%track% %title%).")
    ok_text = "태그 쓰기"

    def build(self):
        self.fmt = self.add_format("패턴:", "file2tag", [
            "%track% %title%",
            "%artist% - %title%",
            "%track% - %artist% - %title%",
            "%track%. %title%",
            "%artist%/%album%/%track% %title%",
        ])
        self._names = []

    def headers(self):
        return ["파일명"] + [FIELD_LABELS.get(n, n) for n in self._names] + ["상태"]

    def make_plans(self):
        self._names = []
        fmt = self.fmt.currentText()
        if "%" not in fmt:
            raise ValueError("패턴에 %필드% 를 하나 이상 넣으세요")
        pat = Pattern(fmt)
        self._names = list(dict.fromkeys(pat.names))
        plans = []
        for f in self.files:
            src = pat.source_for_path(f)
            p = Plan(f, cells=[src])
            m = pat.match(src)
            if m is None:
                p.cells += [""] * len(self._names)
                p.error = "패턴과 맞지 않음"
            else:
                p.cells += [m.get(n, "") for n in self._names]
                p.changes = {n: [v] for n, v in m.items() if v and f.tags.get(n) != [v]}
                p.unchanged = not p.changes
            plans.append(p)
        return plans

    def remember(self):
        self.fmt.remember()


# ------------------------------------------------------------------ 파일명 → 파일명

class FilenameToFilename(ConverterDialog):
    title = "변환: 파일명 → 파일명"
    description = ("기존 파일명을 패턴으로 잘라 새 이름으로 다시 조합합니다. "
                   "조각은 %1 %2 … 또는 %이름% 으로 표시합니다 (예: %1 - %2 → %2 - %1).")
    ok_text = "이름 바꾸기"

    def build(self):
        self.src = self.add_format("기존 파일명 패턴:", "file2file_src", ["%1 - %2", "%1 %2"])
        self.dst = self.add_format("새 파일명 패턴:", "file2file_dst", ["%2 - %1", "%2"])
        self.win = self.add_check("Windows에서 쓸 수 없는 문자(* ? \" < > |)도 바꾸기", "win_compat", False)

    def headers(self):
        return ["현재 파일명", "새 파일명", "상태"]

    def make_plans(self):
        src = normalize_numbered(self.src.currentText())
        dst = normalize_numbered(self.dst.currentText())
        if "%" not in src:
            raise ValueError("기존 파일명 패턴에 %1 같은 조각을 넣으세요")
        if not dst.strip():
            raise ValueError("새 파일명 패턴을 입력하세요")
        pat = Pattern(src, allow_any_name=True)
        vf = make_value_filter(self.win.isChecked())
        plans = []
        for i, f in enumerate(self.files):
            p = Plan(f, cells=[f.filename])
            m = pat.match(pat.source_for_path(f))
            if m is None:
                p.cells.append("")
                p.error = "패턴과 맞지 않음"
            else:
                try:
                    result = evaluate(dst, Context(f, i, len(self.files), overrides=m, value_filter=vf))
                    new = build_target_path(f, result, self.win.isChecked())
                    p.cells.append(display_path(f, new))
                    if new == f.path:
                        p.unchanged = True
                    else:
                        p.new_path = new
                except ValueError as e:
                    p.cells.append("")
                    p.error = str(e)
            plans.append(p)
        check_conflicts(plans)
        return plans

    def remember(self):
        self.src.remember()
        self.dst.remember()


# ------------------------------------------------------------------ 태그 → 태그

class TagToTag(ConverterDialog):
    title = "변환: 태그 → 태그"
    description = ("포맷 문자열의 결과를 선택한 필드에 씁니다. 결과가 비면 필드를 지웁니다. "
                   "여러 값은 \\\\ 로 구분합니다.")
    ok_text = "태그 쓰기"

    def build(self):
        from .actiondialogs import field_combo
        last = self.settings.value("conv/tag2tag_field", "title", str)
        self.target, self.target_value = field_combo(FIELDS, last)
        self.target.editTextChanged.connect(self.schedule)
        self.target.currentIndexChanged.connect(self.schedule)
        self.form.addRow("필드:", self.target)
        self.fmt = self.add_format("포맷:", "tag2tag", [
            "$caps(%title%)",
            "$trim(%title%)",
            "$num(%track%,2)",
            "%artist% - %title%",
            "$replace(%title%,_, )",
        ])

    def headers(self):
        return ["파일명", "현재 값", "새 값", "상태"]

    def make_plans(self):
        fmt = self.fmt.currentText()
        name = self.target_value()
        plans = []
        for i, f in enumerate(self.files):
            vals = split_values(evaluate(fmt, Context(f, i, len(self.files))))
            p = Plan(f, cells=[f.filename, f.get(name), SEP.join(vals)])
            if vals == f.tags.get(name, []):
                p.unchanged = True
            else:
                p.changes = {name: vals}
            plans.append(p)
        return plans

    def remember(self):
        self.fmt.remember()
        self.settings.setValue("conv/tag2tag_field", self.target_value())


# ------------------------------------------------------------------ 텍스트 → 태그

def read_text_file(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


class TextToTag(ConverterDialog):
    title = "변환: 텍스트 → 태그"
    description = ("텍스트의 각 줄을 목록 순서대로 파일에 하나씩 대응시켜, 패턴에 맞춰 태그에 넣습니다. "
                   "곡 목록을 붙여 넣어 제목을 한꺼번에 넣을 때 좋습니다.")
    ok_text = "태그 쓰기"

    def build(self):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        b1 = QPushButton("텍스트 파일 열기…")
        b1.clicked.connect(self.open_file)
        b2 = QPushButton("클립보드에서 붙여넣기")
        b2.clicked.connect(lambda: self.text.setPlainText(QGuiApplication.clipboard().text()))
        h.addWidget(b1)
        h.addWidget(b2)
        h.addStretch(1)
        self.form.addRow("텍스트:", row)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("1. 첫 번째 곡\n2. 두 번째 곡\n…")
        self.text.setMaximumHeight(160)
        self.text.textChanged.connect(self.schedule)
        self.form.addRow("", self.text)
        self.fmt = self.add_format("패턴:", "text2tag", [
            "%title%",
            "%track%. %title%",
            "%track% %title%",
            "%artist% - %title%",
            "%track%. %title% (%dummy%)",
        ])
        self.skip_blank = self.add_check("빈 줄 무시", "skip_blank", True)
        self._names = []

    def open_file(self):
        start = os.path.dirname(self.files[0].path) if self.files else ""
        path, _ = QFileDialog.getOpenFileName(self, "텍스트 파일", start, "텍스트 (*.txt *.csv *.cue *.m3u *.*)")
        if path:
            try:
                self.text.setPlainText(read_text_file(path))
            except OSError as e:
                self.summary.setText(str(e))

    def headers(self):
        return ["파일명", "줄"] + [FIELD_LABELS.get(n, n) for n in self._names] + ["상태"]

    def make_plans(self):
        self._names = []
        fmt = self.fmt.currentText()
        if "%" not in fmt:
            raise ValueError("패턴에 %필드% 를 하나 이상 넣으세요")
        pat = Pattern(fmt)
        self._names = list(dict.fromkeys(pat.names))
        lines = self.text.toPlainText().splitlines()
        if self.skip_blank.isChecked():
            lines = [l for l in lines if l.strip()]
        plans = []
        for i, f in enumerate(self.files):
            line = lines[i] if i < len(lines) else None
            p = Plan(f, cells=[f.filename, line or ""])
            if line is None:
                p.cells += [""] * len(self._names)
                p.error = "대응하는 줄이 없음"
            else:
                m = pat.match(line.strip())
                if m is None:
                    p.cells += [""] * len(self._names)
                    p.error = "패턴과 맞지 않음"
                else:
                    p.cells += [m.get(n, "") for n in self._names]
                    p.changes = {n: [v] for n, v in m.items() if v and f.tags.get(n) != [v]}
                    p.unchanged = not p.changes
            plans.append(p)
        return plans

    def remember(self):
        self.fmt.remember()


# ------------------------------------------------------------------ 자동 번호 매기기

class AutoNumber(ConverterDialog):
    title = "자동 번호 매기기"
    description = "목록에 보이는 순서대로 트랙 번호를 매깁니다."
    ok_text = "번호 매기기"

    def _spin(self, label, key, lo, hi, default):
        sb = QSpinBox()
        sb.setRange(lo, hi)
        sb.setValue(self.settings.value(f"conv/{key}", default, int))
        sb.valueChanged.connect(lambda v: self.settings.setValue(f"conv/{key}", v))
        sb.valueChanged.connect(self.schedule)
        self.form.addRow(label, sb)
        return sb

    def build(self):
        self.start = self._spin("시작 번호:", "num_start", 0, 9999, 1)
        self.digits = self._spin("자릿수 (앞을 0으로 채움):", "num_digits", 1, 4, 2)
        self.total = self.add_check("전체 곡 수도 넣기 (예: 03/12)", "num_total", False)
        self.per_folder = self.add_check("폴더가 바뀌면 다시 시작", "num_per_folder", True)
        self.disc = self.add_check("폴더마다 디스크 번호도 1, 2, 3… 으로 매기기", "num_disc", False)

    def headers(self):
        return ["파일명", "현재 트랙", "새 트랙", "디스크", "상태"]

    def make_plans(self):
        groups = []
        for f in self.files:
            key = os.path.dirname(f.path) if self.per_folder.isChecked() else ""
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(f)
        plans = []
        digits = self.digits.value()
        for gi, (_, files) in enumerate(groups):
            n_total = len(files)
            for i, f in enumerate(files):
                num = str(self.start.value() + i).zfill(digits)
                track = f"{num}/{str(n_total).zfill(digits)}" if self.total.isChecked() else num
                changes = {}
                if f.tags.get("track") != [track]:
                    changes["track"] = [track]
                disc = ""
                if self.disc.isChecked():
                    disc = str(gi + 1)
                    if f.tags.get("disc") != [disc]:
                        changes["disc"] = [disc]
                p = Plan(f, changes=changes, cells=[f.filename, f.get("track"), track, disc])
                p.unchanged = not changes
                plans.append(p)
        return plans


CONVERTERS = [
    ("태그 → 파일명…", "Alt+1", TagToFilename),
    ("파일명 → 태그…", "Alt+2", FilenameToTag),
    ("파일명 → 파일명…", "Alt+3", FilenameToFilename),
    ("텍스트 → 태그…", "Alt+4", TextToTag),
    ("태그 → 태그…", "Alt+5", TagToTag),
]
