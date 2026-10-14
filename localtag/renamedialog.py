"""곡명·파일명 일괄 변경 창.

왼쪽에 규칙을 쌓고(위에서부터 차례로 적용), 고른 규칙의 설정을 오른쪽에서 바꾸면 아래 미리 보기가 바로 바뀐다.
규칙 묶음은 이름을 붙여 저장해 두고 다시 쓸 수 있다.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .converters import ConverterDialog, build_target_path, check_conflicts, display_path
from .plan import Plan
from .rename import FORMS, INT_RANGES, RULE_TYPES, apply_rules, compile_rules, describe, new_rule
from .script import ScriptError
from .tagio import FIELD_LABELS, field_key

FILENAME = "_filename"
TARGETS = ["title", FILENAME, "album", "artist", "albumartist", "comment"]


def _target_label(k):
    return "파일명 (확장자 빼고)" if k == FILENAME else FIELD_LABELS.get(k, k)


class RenameDialog(ConverterDialog):
    title = "곡명 · 파일명 일괄 변경"
    description = ("규칙을 위에서부터 차례로 적용합니다. 규칙을 고르면 오른쪽에서 설정을 바꿀 수 있고, "
                   "아래 미리 보기가 바로 바뀝니다. 체크를 끈 규칙은 건너뜁니다.")
    ok_text = "모두 바꾸기"

    def build(self):
        self.resize(1040, 780)
        # ---- 대상
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setInsertPolicy(QComboBox.NoInsert)
        for k in TARGETS:
            self.target.addItem(_target_label(k), k)
        cur = self.settings.value("rename/target", "title", str)
        i = self.target.findData(cur)
        if i >= 0:
            self.target.setCurrentIndex(i)
        else:
            self.target.setEditText(cur)
        self.target.setToolTip("목록에 없는 필드는 이름을 직접 입력하세요 (예: SUBTITLE)")
        self.target.currentTextChanged.connect(self._target_changed)
        self.from_file = QCheckBox("값이 비어 있으면 파일명에서 시작")
        self.from_file.setChecked(self.settings.value("rename/from_file", True, bool))
        self.from_file.toggled.connect(lambda on: self.settings.setValue("rename/from_file", on))
        self.from_file.toggled.connect(self.schedule)
        trow = QHBoxLayout()
        trow.addWidget(self.target, 1)
        trow.addWidget(self.from_file)
        self.form.addRow("바꿀 곳:", trow)

        # ---- 규칙 묶음 (저장해 둔 것)
        self.presets = self._load_presets()
        prow = QHBoxLayout()
        self.preset_box = QComboBox()
        self.preset_box.setMinimumContentsLength(24)
        self._fill_presets()
        self.preset_box.activated.connect(self._use_preset)
        prow.addWidget(self.preset_box, 1)
        for text, slot in (("저장…", self._save_preset), ("지우기", self._delete_preset)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            prow.addWidget(b)
        self.form.addRow("규칙 묶음:", prow)

        # ---- 규칙 목록 + 편집
        try:
            self.rules = json.loads(self.settings.value("rename/last", "[]", str)) or []
        except ValueError:
            self.rules = []
        if not self.rules:
            self.rules = [new_rule("replace")]
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setMinimumWidth(360)
        self.list.setMaximumHeight(200)
        self.list.currentRowChanged.connect(self._show_rule)
        self.list.itemChanged.connect(self._toggled)
        left.addWidget(self.list)
        brow = QHBoxLayout()
        add = QPushButton("＋ 규칙 추가")
        add.setAutoDefault(False)
        menu = QMenu(add)
        for k, t in RULE_TYPES.items():
            menu.addAction(t, lambda k=k: self._add(k))
        add.setMenu(menu)
        brow.addWidget(add)
        for text, slot, tip in (("－ 빼기", self._remove, "고른 규칙 지우기"), ("↑", lambda: self._move(-1), "위로"),
                                ("↓", lambda: self._move(1), "아래로"), ("복제", self._dup, "고른 규칙을 하나 더")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            brow.addWidget(b)
        brow.addStretch(1)
        left.addLayout(brow)
        h.addLayout(left, 1)
        self.editor = QGroupBox("규칙 설정")
        self.editor_form = QFormLayout(self.editor)
        self.editor_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.editor.setMinimumWidth(440)
        h.addWidget(self.editor, 1)
        self.form.addRow(box)
        self._reload_list(0)

    # ------------------------------------------------------------ 규칙 목록
    def _reload_list(self, select=None):
        self.list.blockSignals(True)
        self.list.clear()
        for i, r in enumerate(self.rules):
            it = QListWidgetItem(f"{i + 1}. {describe(r)}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r.get("on", True) else Qt.Unchecked)
            self.list.addItem(it)
        self.list.blockSignals(False)
        if select is not None and self.rules:
            self.list.setCurrentRow(max(0, min(select, len(self.rules) - 1)))
        self._show_rule(self.list.currentRow())

    def _relabel(self, row):
        it = self.list.item(row)
        if it is not None:
            self.list.blockSignals(True)
            it.setText(f"{row + 1}. {describe(self.rules[row])}")
            self.list.blockSignals(False)

    def _toggled(self, item):
        row = self.list.row(item)
        self.rules[row]["on"] = item.checkState() == Qt.Checked
        self.schedule()

    def _add(self, kind):
        row = self.list.currentRow()
        at = row + 1 if row >= 0 else len(self.rules)
        self.rules.insert(at, new_rule(kind))
        self._reload_list(at)
        self.schedule()

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.rules):
            del self.rules[row]
            self._reload_list(row)
            self.schedule()

    def _dup(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.rules):
            self.rules.insert(row + 1, json.loads(json.dumps(self.rules[row])))
            self._reload_list(row + 1)
            self.schedule()

    def _move(self, d):
        row = self.list.currentRow()
        j = row + d
        if 0 <= row < len(self.rules) and 0 <= j < len(self.rules):
            self.rules[row], self.rules[j] = self.rules[j], self.rules[row]
            self._reload_list(j)
            self.schedule()

    # ------------------------------------------------------------ 규칙 편집
    def _show_rule(self, row):
        while self.editor_form.rowCount():
            self.editor_form.removeRow(0)
        if not (0 <= row < len(self.rules)):
            self.editor.setTitle("규칙 설정")
            self.editor_form.addRow(QLabel("＋ 규칙 추가로 규칙을 넣으세요."))
            return
        r = self.rules[row]
        self.editor.setTitle(f"{row + 1}. {RULE_TYPES.get(r['type'], r['type'])}")

        def setter(key, conv=lambda v: v):
            def f(v):
                r[key] = conv(v)
                self._relabel(row)
                self.schedule()
            return f

        for spec in FORMS[r["type"]]:
            key, label, kind = spec[:3]
            if kind in ("text", "format"):
                w = QLineEdit(str(r.get(key, "")))
                if kind == "format":
                    w.setPlaceholderText("%artist% 같은 필드 사용 가능 · 대괄호는 '[' 처럼 따옴표로")
                w.textChanged.connect(setter(key))
                self.editor_form.addRow(label + ":", w)
            elif kind == "bool":
                w = QCheckBox(label)
                w.setChecked(bool(r.get(key)))
                w.toggled.connect(setter(key))
                self.editor_form.addRow("", w)
            elif kind == "int":
                w = QSpinBox()
                lo, hi = INT_RANGES.get(key, (0, 9999))
                w.setRange(lo, hi)
                w.setValue(int(r.get(key, 0)))
                w.valueChanged.connect(setter(key, int))
                self.editor_form.addRow(label + ":", w)
            elif kind == "choice":
                w = QComboBox()
                for k, t in spec[3].items():
                    w.addItem(t, k)
                w.setCurrentIndex(max(0, w.findData(r.get(key))))
                w.currentIndexChanged.connect(lambda _i, w=w, f=setter(key): f(w.currentData()))
                self.editor_form.addRow(label + ":", w)

    # ------------------------------------------------------------ 규칙 묶음
    def _load_presets(self):
        try:
            return json.loads(self.settings.value("rename/presets", "{}", str)) or {}
        except ValueError:
            return {}

    def _fill_presets(self):
        self.preset_box.clear()
        self.preset_box.addItem("— 저장한 규칙 묶음 고르기 —", None)
        for name in sorted(self.presets, key=str.casefold):
            self.preset_box.addItem(name, name)

    def _use_preset(self, i):
        name = self.preset_box.itemData(i)
        if name in self.presets:
            self.rules = json.loads(json.dumps(self.presets[name]["rules"]))
            t = self.presets[name].get("target")
            if t:
                j = self.target.findData(t)
                if j >= 0:
                    self.target.setCurrentIndex(j)
                else:
                    self.target.setEditText(t)
            self._reload_list(0)
            self.schedule()

    def _save_preset(self):
        cur = self.preset_box.currentData() or ""
        name, ok = QInputDialog.getText(self, "규칙 묶음 저장", "이름:", text=cur)
        name = name.strip()
        if not ok or not name:
            return
        self.presets[name] = {"rules": json.loads(json.dumps(self.rules)), "target": self.target_field()}
        self.settings.setValue("rename/presets", json.dumps(self.presets, ensure_ascii=False))
        self._fill_presets()
        self.preset_box.setCurrentIndex(self.preset_box.findData(name))

    def _delete_preset(self):
        name = self.preset_box.currentData()
        if name in self.presets and QMessageBox.question(
                self, "규칙 묶음", f"'{name}' 을(를) 지울까요?") == QMessageBox.Yes:
            del self.presets[name]
            self.settings.setValue("rename/presets", json.dumps(self.presets, ensure_ascii=False))
            self._fill_presets()

    # ------------------------------------------------------------ 미리 보기
    def target_field(self) -> str:
        i = self.target.findText(self.target.currentText())
        if i >= 0:
            return self.target.itemData(i)
        t = self.target.currentText().strip()
        return field_key(t) if t else "title"

    def _target_changed(self, _):
        self.settings.setValue("rename/target", self.target_field())
        self.from_file.setEnabled(self.target_field() != FILENAME)
        self.schedule()

    def headers(self):
        return ["지금", "바뀐 뒤", "상태"]

    def make_plans(self):
        compiled = compile_rules(self.rules)
        field = self.target_field()
        n = len(self.files)
        plans = []
        for i, f in enumerate(self.files):
            if field == FILENAME:
                old = [f.stem]
            else:
                old = f.tags.get(field) or ([f.stem] if self.from_file.isChecked() else [])
            shown = " · ".join(old)
            p = Plan(f, cells=[shown])
            if not old:
                p.cells.append("")
                p.unchanged = True
                plans.append(p)
                continue
            try:
                new = [apply_rules(v, compiled, f, i, n) for v in old]
            except (ValueError, ScriptError) as e:
                p.cells.append("")
                p.error = str(e)
                plans.append(p)
                continue
            p.cells.append(" · ".join(new))
            if field == FILENAME:
                try:
                    # 이름 속 '/' 는 폴더가 되지 않게 바꾼다
                    path = build_target_path(f, new[0].replace("/", "_").replace("\\", "_"), False)
                except ValueError as e:
                    p.error = str(e)
                    plans.append(p)
                    continue
                p.cells[1] = display_path(f, path)
                if path == f.path:
                    p.unchanged = True
                else:
                    p.new_path = path
            else:
                vals = [v for v in (x.strip() for x in new) if v]
                if vals == f.tags.get(field, []):
                    p.unchanged = True
                elif not vals:
                    p.error = "빈 값이 됩니다"
                else:
                    p.changes = {field: vals}
            plans.append(p)
        check_conflicts(plans)
        return plans

    def remember(self):
        self.settings.setValue("rename/last", json.dumps(self.rules, ensure_ascii=False))

    def reject(self):
        # 취소해도 쌓아 둔 규칙은 다음에 다시 쓸 수 있게 남긴다
        self.remember()
        super().reject()

