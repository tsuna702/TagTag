"""클래식 작품·악장 나누기 창: 제목을 작품 · 악장 번호 · 악장 이름으로 나눠 미리 보고 한 번에 적용한다."""
from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QPushButton

from .classical import TOTAL_MODES, changes_for, split_title, totals
from .converters import ConverterDialog
from .osutil import keys
from .plan import Plan


class ClassicalDialog(ConverterDialog):
    title = "클래식 작품·악장 나누기"
    description = ("제목의 첫 ': ' 앞을 작품, 뒤를 악장으로 나눕니다 "
                   "(예: 'Waltzes, Op. 64: 2. Tempo Giusto' → 작품 'Waltzes, Op. 64' · 2악장 'Tempo Giusto'). "
                   "Music 은 m4a 파일의 작품·악장을 읽어 같은 작품의 악장을 묶어 보여 줍니다. "
                   "나누지 않을 줄은 골라서 빼 두기.")
    ok_text = "모두 적용"

    def build(self):
        self.skipped = set()
        self.total = self.add_choice("악장 수:", "classical_total", TOTAL_MODES, "none")
        self.show = self.add_check("Music 에서 '작품 및 악장 보기' 켜기", "classical_show", True)
        self.rename = self.add_check("제목을 악장 이름으로 바꾸기 (보통은 그대로 두기)", "classical_rename", False)
        row = QHBoxLayout()
        skip = QPushButton("고른 줄 빼기 / 다시 넣기")
        skip.setShortcut(QKeySequence("Ctrl+D"))
        skip.setToolTip(keys("고른 줄은 적용하지 않습니다. 한 번 더 누르면 다시 넣습니다 (⌘D)"))
        skip.setAutoDefault(False)
        skip.clicked.connect(self._toggle)
        row.addWidget(skip)
        row.addStretch(1)
        self.form.addRow("고른 줄:", row)

    def headers(self):
        return ["원래 제목", "작품", "번호", "악장 수", "악장 이름", "상태"]

    def make_plans(self):
        parsed = {id(f): split_title(f.get("title")) for f in self.files}
        rows = [((f.get("album"), p[0]), p[1]) for f in self.files if (p := parsed[id(f)]) and id(f) not in self.skipped]
        total_of = totals(rows, self.total.currentData())
        plans = []
        for f in self.files:
            title, p = f.get("title"), parsed[id(f)]
            if p is None:
                plan = Plan(f, cells=[title, f.get("WORK"), f.get("MOVEMENT"), f.get("MOVEMENTTOTAL"),
                                      f.get("MOVEMENTNAME"), "나눌 수 없음 (': ' 없음)"], unchanged=True)
                plans.append(plan)
                continue
            work, num, name = p
            total = total_of.get((f.get("album"), work), 0) if num else 0
            total = max(total, num) if total else 0
            plan = Plan(f, cells=[title, work, str(num or ""), str(total or ""), name])
            if id(f) in self.skipped:
                plan.unchanged = True
                plan.cells.append("빼 둠")
            else:
                plan.changes = changes_for(f.tags, work, num, name, total, self.show.isChecked(),
                                           name if self.rename.isChecked() else None)
                plan.unchanged = not plan.changes
            plans.append(plan)
        return plans

    def _toggle(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        picked = {id(self.plans[r].file) for r in rows if r < len(self.plans)}
        if not picked:
            return
        if picked <= self.skipped:
            self.skipped -= picked
        else:
            self.skipped |= picked
        self.refresh()
        sm = self.table.selectionModel()
        for r, p in enumerate(self.plans):
            if id(p.file) in picked:
                sm.select(self.table.model().index(r, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
