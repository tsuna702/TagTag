"""목록 내보내기(CSV/HTML/텍스트)와 재생목록(M3U) 만들기."""
from __future__ import annotations

import csv
import html
import io
import os
import time

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

from .converters import FormatCombo, HelpDialog
from .model import fmt_length
from .script import Context, ScriptError, evaluate

DEFAULT_COLUMNS = """트랙=%track%
제목=%title%
아티스트=%artist%
앨범=%album%
연도=%year%
장르=%genre%
길이=%_length%
비트레이트=%_bitrate%
파일=%_filename_ext%
경로=%_folderpath%"""

FORMATS = [("csv", "CSV (엑셀)"), ("html", "HTML (웹 페이지)"), ("txt", "텍스트")]


def parse_columns(text: str):
    cols = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, sep, fmt = line.partition("=")
        if not sep:
            name, fmt = line.strip(), line.strip()
        cols.append((name.strip(), fmt.strip()))
    return cols


def render(kind, files, columns, line_fmt, title):
    n = len(files)
    if kind == "txt":
        return "".join(evaluate(line_fmt, Context(f, i, n)) + "\n" for i, f in enumerate(files))
    rows = [[evaluate(fmt, Context(f, i, n)) for _, fmt in columns] for i, f in enumerate(files)]
    if kind == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c for c, _ in columns])
        w.writerows(rows)
        return buf.getvalue()
    total = fmt_length(sum(f.length for f in files))
    head = "".join(f"<th>{html.escape(c)}</th>" for c, _ in columns)
    body = "\n".join("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in r) + "</tr>" for r in rows)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --line: #8884; --head: #8882; }}
body {{ font: 14px -apple-system, "Apple SD Gothic Neo", sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
p {{ color: gray; margin: 0 0 16px; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; white-space: nowrap; }}
th {{ background: var(--head); position: sticky; top: 0; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>{n}곡 · 총 {total} · {time.strftime("%Y-%m-%d %H:%M")}</p>
<div class="wrap"><table>
<thead><tr>{head}</tr></thead>
<tbody>
{body}
</tbody></table></div>
</body></html>
"""


class ExportDialog(QDialog):
    def __init__(self, parent, selected, visible, settings, start_dir):
        super().__init__(parent)
        self.selected, self.visible, self.settings, self.start_dir = selected, visible, settings, start_dir
        self.setWindowTitle("내보내기")
        self.resize(820, 640)
        v = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        v.addLayout(form)

        self.kind = QComboBox()
        for k, label in FORMATS:
            self.kind.addItem(label, k)
        last = settings.value("export/kind", "csv", str)
        self.kind.setCurrentIndex(max(0, [k for k, _ in FORMATS].index(last) if last in dict(FORMATS) else 0))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("형식:", self.kind)

        scope = QWidget()
        sh = QHBoxLayout(scope)
        sh.setContentsMargins(0, 0, 0, 0)
        self.r_sel = QRadioButton(f"선택한 파일 ({len(selected)}개)")
        self.r_all = QRadioButton(f"목록에 보이는 모든 파일 ({len(visible)}개)")
        grp = QButtonGroup(self)
        grp.addButton(self.r_sel)
        grp.addButton(self.r_all)
        (self.r_sel if selected else self.r_all).setChecked(True)
        self.r_sel.setEnabled(bool(selected))
        for r in (self.r_sel, self.r_all):
            r.toggled.connect(self.schedule)
            sh.addWidget(r)
        sh.addStretch(1)
        form.addRow("대상:", scope)

        self.title = QLineEdit(settings.value("export/title", "음악 목록", str))
        self.title.textChanged.connect(self.schedule)
        self.title_label = QLabel("제목 (HTML):")
        form.addRow(self.title_label, self.title)

        self.columns = QPlainTextEdit(settings.value("export/columns", DEFAULT_COLUMNS, str))
        self.columns.setMaximumHeight(170)
        self.columns.textChanged.connect(self.schedule)
        self.columns_label = QLabel("열 (한 줄에 하나,\n이름=포맷):")
        form.addRow(self.columns_label, self.columns)

        self.line_fmt = FormatCombo(settings, "export_line", [
            "%artist% - %title%", "$num(%track%,2). %title% (%_length%)", "%_path%"])
        self.line_fmt.editTextChanged.connect(self.schedule)
        self.line_label = QLabel("줄 포맷 (텍스트):")
        form.addRow(self.line_label, self.line_fmt)

        self.open_after = QCheckBox("저장한 뒤 열기")
        self.open_after.setChecked(settings.value("export/open_after", True, bool))
        form.addRow("", self.open_after)

        v.addWidget(QLabel("미리보기 (처음 30개):"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(self.preview, 1)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c82828;")
        v.addWidget(self.error)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("저장…")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.addButton("도움말", QDialogButtonBox.HelpRole).clicked.connect(lambda: HelpDialog.show_help(self))
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self._kind_changed()

    def files(self):
        return self.selected if self.r_sel.isChecked() else self.visible

    def _kind_changed(self, *_):
        k = self.kind.currentData()
        for w in (self.columns, self.columns_label):
            w.setVisible(k != "txt")
        for w in (self.line_fmt, self.line_label):
            w.setVisible(k == "txt")
        for w in (self.title, self.title_label):
            w.setVisible(k == "html")
        self.refresh()

    def schedule(self, *_):
        self.timer.start()

    def _render(self, files):
        return render(self.kind.currentData(), files, parse_columns(self.columns.toPlainText()),
                      self.line_fmt.currentText(), self.title.text())

    def refresh(self):
        try:
            self.preview.setPlainText(self._render(self.files()[:30]))
            self.error.setText("")
        except ScriptError as e:
            self.preview.setPlainText("")
            self.error.setText(f"⚠ {e}")

    def save(self):
        kind = self.kind.currentData()
        try:
            text = self._render(self.files())
        except ScriptError as e:
            self.error.setText(f"⚠ {e}")
            return
        ext = {"csv": ".csv", "html": ".html", "txt": ".txt"}[kind]
        path, _ = QFileDialog.getSaveFileName(
            self, "내보내기", os.path.join(self.start_dir, "음악 목록" + ext), f"*{ext}")
        if not path:
            return
        try:
            # CSV는 엑셀에서 한글이 깨지지 않도록 BOM을 붙인다
            with open(path, "w", encoding="utf-8-sig" if kind == "csv" else "utf-8", newline="") as fh:
                fh.write(text)
        except OSError as e:
            QMessageBox.warning(self, "내보내기", str(e))
            return
        s = self.settings
        s.setValue("export/kind", kind)
        s.setValue("export/columns", self.columns.toPlainText())
        s.setValue("export/title", self.title.text())
        s.setValue("export/open_after", self.open_after.isChecked())
        self.line_fmt.remember()
        if self.open_after.isChecked():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.saved_path = path
        self.accept()


# ------------------------------------------------------------------ 재생목록

def build_m3u(files, playlist_path, relative=True, extended=True) -> str:
    base = os.path.dirname(playlist_path)
    lines = ["#EXTM3U"] if extended else []
    for f in files:
        if extended:
            name = " - ".join(x for x in (f.get("artist"), f.get("title")) if x) or f.stem
            lines.append(f"#EXTINF:{int(round(f.length))},{name}")
        lines.append(os.path.relpath(f.path, base) if relative else f.path)
    return "\n".join(lines) + "\n"


def common_folder(files):
    try:
        return os.path.commonpath([os.path.dirname(f.path) for f in files])
    except ValueError:
        return os.path.dirname(files[0].path)


class PlaylistDialog(QDialog):
    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.files, self.settings = files, settings
        self.setWindowTitle("재생목록 만들기")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"선택한 {len(files)}개 파일을 목록에 보이는 순서대로 M3U 재생목록에 넣습니다."))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        folder = common_folder(files)
        name = os.path.basename(folder) or "playlist"
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(os.path.join(folder, name + ".m3u8"))
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self.browse)
        h.addWidget(self.path, 1)
        h.addWidget(browse)
        form.addRow("저장 위치:", row)
        self.relative = QCheckBox("상대 경로로 쓰기 (폴더째 옮겨도 재생됨)")
        self.relative.setChecked(settings.value("playlist/relative", True, bool))
        self.extended = QCheckBox("곡 정보 넣기 (#EXTINF: 길이, 아티스트 - 제목)")
        self.extended.setChecked(settings.value("playlist/extended", True, bool))
        form.addRow("", self.relative)
        form.addRow("", self.extended)
        v.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("만들기")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "재생목록 저장", self.path.text(), "재생목록 (*.m3u8 *.m3u)")
        if path:
            self.path.setText(path)

    def save(self):
        path = self.path.text().strip()
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".m3u8"
        if os.path.exists(path) and QMessageBox.question(
                self, "재생목록", f"{os.path.basename(path)} 파일이 이미 있습니다. 덮어쓸까요?") != QMessageBox.Yes:
            return
        text = build_m3u(self.files, path, self.relative.isChecked(), self.extended.isChecked())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # .m3u 는 옛 플레이어 호환을 위해 BOM 없는 UTF-8, .m3u8 은 표준대로 UTF-8
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as e:
            QMessageBox.warning(self, "재생목록", str(e))
            return
        self.settings.setValue("playlist/relative", self.relative.isChecked())
        self.settings.setValue("playlist/extended", self.extended.isChecked())
        self.saved_path = path
        self.accept()
