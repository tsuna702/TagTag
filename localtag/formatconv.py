"""형식 변환 (macOS 전용): FLAC·WAV·AIFF 등 → ALAC(무손실) 또는 AAC, 맥에 들어 있는 afconvert 로.

Music 이 못 읽는 FLAC 을 Music 에 넣을 수 있게 한다. 태그와 커버는 LocalTag 가 옮겨 쓴다.
원본은 그대로 두거나 휴지통으로 옮긴다 (지우지 않는다). 새 파일을 만드는 작업이라 실행 취소에는 들어가지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QFile
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .tagio import AudioFile, Options

AFCONVERT = "/usr/bin/afconvert"
TARGETS = {
    "alac": ("ALAC (무손실, .m4a)", "ALAC"),
    "aac256": ("AAC 256kbps (.m4a, Apple Music 과 같은 음질)", "AAC"),
    "aac320": ("AAC 320kbps (.m4a)", "AAC"),
}
ORIGINALS = {"keep": "그대로 두기", "trash": "휴지통으로 옮기기"}
# afconvert 가 읽는 형식 (실제 코덱 이름 기준)
READABLE = {"FLAC", "ALAC", "AAC", "MP3", "WAV", "AIFF"}
ALAC_BITS = {16: 1, 20: 2, 24: 3, 32: 4}  # afconvert 의 alac 깊이 표시


def available() -> bool:
    return os.path.exists(AFCONVERT)


@dataclass
class Job:
    f: AudioFile
    dst: str = ""
    skip: str = ""  # 건너뛰는 이유 (비었으면 변환)


def plan(files, target: str) -> list[Job]:
    """곡마다 만들 파일 이름과 건너뛸 이유."""
    want = TARGETS[target][1]
    jobs, taken = [], set()
    for f in files:
        j = Job(f)
        jobs.append(j)
        if f.codec not in READABLE:
            j.skip = f"{f.codec or f.ext.lstrip('.').upper()} 은(는) 바꿀 수 없음"
            continue
        if f.codec == want and (want == "ALAC" or abs((f.bitrate or 0) - _bitrate(target)) < 16000):
            j.skip = f"이미 {want}"
            continue
        if want == "ALAC" and not f.lossless:
            j.skip = f"{f.codec} → 무손실로 바꿔도 음질이 좋아지지 않음 (AAC 를 고르세요)"
            continue
        stem = os.path.splitext(f.path)[0]
        for cand in (stem + ".m4a", f"{stem} ({want}).m4a"):
            if cand.lower() not in taken and not os.path.exists(cand):
                j.dst = cand
                taken.add(cand.lower())
                break
        else:
            j.skip = "같은 이름의 .m4a 가 이미 있음"
    return jobs


def _bitrate(target):
    return {"aac256": 256000, "aac320": 320000}.get(target, 0)


def _args(f: AudioFile, target: str) -> list[str]:
    if target == "alac":
        return ["-f", "m4af", "-d", f"alac/{ALAC_BITS.get(f.bits, 1)}"]
    return ["-f", "m4af", "-d", "aac", "-s", "2", "-b", str(_bitrate(target)), "-q", "127"]


def convert_one(job: Job, target: str, opts: Options) -> AudioFile:
    """작업 사본으로 바꾸고 태그·커버를 옮긴 뒤 길이가 같은지 확인하고 제자리로."""
    f = job.f
    tmp = os.path.join(os.path.dirname(job.dst), f".localtag-conv-{uuid.uuid4().hex[:8]}.m4a")
    try:
        r = subprocess.run([AFCONVERT, *_args(f, target), f.path, tmp], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError((r.stderr or r.stdout or "afconvert 가 실패했습니다").strip().splitlines()[-1])
        new = AudioFile.load(tmp, opts)
        if f.length and abs((new.length or 0) - f.length) > 1.0:
            raise RuntimeError(f"길이가 다름 ({f.length:.1f}초 → {new.length or 0:.1f}초)")
        changes = {k: list(v) for k, v in f.tags.items() if v}
        new = new.save(changes, list(f.pictures), opts)
        os.replace(tmp, job.dst)
        return AudioFile.load(job.dst, opts)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def run(jobs, target: str, originals: str, opts: Options, progress=lambda *_: None, cancelled=lambda: False):
    """→ (만든 파일 [(원본 경로, 새 AudioFile)], 실패 [(파일명, 이유)])"""
    todo = [j for j in jobs if not j.skip]
    made, failed, done = [], [], [0]

    def one(j):
        if cancelled():
            return
        try:
            new = convert_one(j, target, opts)
        except Exception as e:  # noqa: BLE001 — 곡마다 이유를 모아 보여 준다
            failed.append((j.f.filename, str(e)))
        else:
            if originals == "trash" and not QFile.moveToTrash(j.f.path):
                failed.append((j.f.filename, "변환은 했지만 원본을 휴지통으로 옮기지 못함"))
            made.append((j.f.path, new))
        done[0] += 1
        progress(done[0], len(todo), f"형식 변환 중 · {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(max(1, min(4, os.cpu_count() or 2))) as ex:
        list(ex.map(one, todo))
    return made, failed


class FormatConvertDialog(QDialog):
    """결과: self.target, self.originals, self.jobs"""

    def __init__(self, parent, files, settings):
        super().__init__(parent)
        self.files, self.settings = files, settings
        self.setWindowTitle("형식 변환")
        self.resize(820, 520)
        v = QVBoxLayout(self)
        intro = QLabel("맥에 들어 있는 afconvert 로 바꿉니다. 태그와 커버는 그대로 옮기고, 새 파일은 원본과 같은 폴더에 "
                       "<b>.m4a</b> 로 만듭니다. 새 파일을 만드는 작업이라 실행 취소로는 되돌리지 않습니다.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.target_box = QComboBox()
        for k, (label, _c) in TARGETS.items():
            self.target_box.addItem(label, k)
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(settings.value("convert/target", "alac", str))))
        self.orig_box = QComboBox()
        for k, label in ORIGINALS.items():
            self.orig_box.addItem(label, k)
        self.orig_box.setCurrentIndex(max(0, self.orig_box.findData(settings.value("convert/originals", "keep", str))))
        self.orig_box.setToolTip("휴지통으로 옮긴 원본은 Finder 의 휴지통에서 다시 꺼낼 수 있습니다")
        form.addRow("바꿀 형식:", self.target_box)
        form.addRow("원본 파일:", self.orig_box)
        v.addLayout(form)
        self.table = QTableWidget(len(files), 3)
        self.table.setHorizontalHeaderLabels(["파일", "지금", "결과"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 120)
        hh.setStretchLastSection(True)
        v.addWidget(self.table, 1)
        self.summary = QLabel()
        v.addWidget(self.summary)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.target_box.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_):
        self.target = self.target_box.currentData()
        self.jobs = plan(self.files, self.target)
        for i, j in enumerate(self.jobs):
            f = j.f
            now = f"{f.codec or f.ext.lstrip('.').upper()}" + (f" {f.bits}bit" if f.lossless and f.bits else
                                                                (f" {f.bitrate // 1000}k" if f.bitrate else ""))
            cells = [f.filename, now, f"→ {os.path.basename(j.dst)}" if not j.skip else f"건너뜀 — {j.skip}"]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if j.skip:
                    it.setForeground(QColor(140, 140, 140))
                self.table.setItem(i, c, it)
        n = sum(1 for j in self.jobs if not j.skip)
        self.summary.setText(f"{len(self.jobs)}곡 중 {n}곡을 바꿉니다.")
        self.ok_btn.setText(f"{n}곡 변환")
        self.ok_btn.setEnabled(n > 0)

    def _accept(self):
        self.originals = self.orig_box.currentData()
        self.settings.setValue("convert/target", self.target)
        self.settings.setValue("convert/originals", self.originals)
        self.accept()
