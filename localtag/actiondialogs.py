"""액션 그룹 관리 / 편집 대화상자."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QVBoxLayout,
)

from .actions import (
    ACTION_TYPES, ALL, CASE_MODES, COVER, FILENAME, YT_ORDERS, describe, field_label, new_action,
)
from .converters import ConverterDialog, HelpDialog
from .tagio import FIELDS, field_key


def field_combo(names, current):
    """기본 필드 목록 + 직접 입력(확장 필드)."""
    w = QComboBox()
    w.setEditable(True)
    w.setInsertPolicy(QComboBox.NoInsert)
    for n in names:
        w.addItem(field_label(n), n)
    if current in names:
        w.setCurrentIndex(names.index(current))
    else:
        w.setEditText(current or "")
    w.setToolTip("목록에 없는 필드는 이름을 직접 입력하세요 (예: LYRICIST, MOOD)")

    def value():
        i = w.findText(w.currentText())
        if i >= 0:
            return w.itemData(i)
        return field_key(w.currentText()) if w.currentText().strip() else names[0]
    return w, value

# 액션 종류별 입력 항목: (키, 제목, 종류)
#   종류: field / field_all / field_fn / text / format / bool / mode / fields
FORMS = {
    "replace": [("field", "필드", "field_all"), ("find", "찾을 문자열", "text"),
                ("replace", "바꿀 문자열", "text"), ("case", "대소문자 구분", "bool"),
                ("word", "단어 전체만", "bool")],
    "regex": [("field", "필드", "field_all"), ("pattern", "정규식", "text"),
              ("replace", "바꿀 내용 ($1 = 첫 번째 그룹)", "text"), ("case", "대소문자 구분", "bool")],
    "case": [("field", "필드", "field_all"), ("mode", "변환", "mode"),
             ("exceptions", "예외 단어 (그대로 둠, 공백으로 구분)", "text")],
    "format": [("field", "필드", "field_fn"), ("format", "포맷 문자열", "format")],
    "remove_fields": [("fields", "지울 필드", "fields")],
    "keep_fields": [("fields", "남길 필드", "fields")],
    "split": [("field", "필드", "field"), ("sep", "구분자", "text")],
    "merge": [("field", "필드", "field"), ("sep", "구분자", "text")],
    "guess": [("source", "원본 (포맷 문자열)", "format"), ("pattern", "추측 패턴", "format")],
    "import_cover": [("filename", "이미지 파일 (* 사용 가능, 포맷 문자열)", "format"),
                     ("replace", "이미 커버가 있으면 바꾸기", "bool")],
    "export_cover": [("filename", "파일명 (확장자 제외, 포맷 문자열)", "format"),
                     ("overwrite", "이미 있으면 덮어쓰기", "bool")],
    "remove_cover": [],
    "resize_cover": [("size", "크기", "choice_size"), ("fit", "맞춤", "choice_fit"),
                     ("trim", "가장자리 검은 띠 없애기", "bool")],
    "youtube": [("split", "제목에서 아티스트를 떼어 아티스트 칸으로", "bool"),
                ("order", "제목 모양", "choice_order"),
                ("overwrite_artist", "이미 있는 아티스트(채널 이름 등)도 바꾸기", "bool"),
                ("keep_cover", "'歌ってみた'·'Cover' 같은 커버곡 표시는 남기기", "bool"),
                ("remove_regex", "더 지울 말 (정규식, 여러 개는 | 로)", "text"),
                ("split_regex", "나누기 정규식 ((?<artist>…) (?<title>…))", "text")],
}


# 액션 편집 창 맨 위에 보여 줄 설명
NOTES = {
    "youtube": ("제목(없으면 파일명)에서 【MV】, (Official Music Video), [4K], (128kbit_AAC) 같은 꼬리말을 떼고, "
                "'아티스트 - 제목'이면 아티스트 칸으로 옮깁니다. '曲名 / 歌手' 모양도 알아봅니다. "
                "채널 이름(… - Topic, …VEVO)은 아티스트 이름으로 다듬습니다. 적용 전에 미리 보여 줍니다."),
}


def _choices(kind):
    from .artwork import FITS
    if kind == "choice_size":
        return {s: f"{s} × {s}" for s in (500, 600, 1000)}
    if kind == "choice_order":
        return dict(YT_ORDERS)
    return dict(FITS)


class ActionEditDialog(QDialog):
    def __init__(self, parent, action: dict):
        super().__init__(parent)
        self.action = copy.deepcopy(action)
        self.setWindowTitle("액션: " + ACTION_TYPES[action["type"]])
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        if action["type"] in NOTES:
            note = QLabel(NOTES[action["type"]])
            note.setWordWrap(True)
            note.setStyleSheet("color: gray;")
            v.addWidget(note)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        v.addLayout(form)
        self.getters = {}
        for key, label, kind in FORMS[action["type"]]:
            val = self.action.get(key)
            if kind in ("field", "field_all", "field_fn"):
                names = ([ALL] if kind == "field_all" else []) + FIELDS + ([FILENAME] if kind == "field_fn" else [])
                w, getter = field_combo(names, val)
                self.getters[key] = getter
                form.addRow(label + ":", w)
            elif kind.startswith("choice_"):
                w = QComboBox()
                opts = _choices(kind)
                for k, t in opts.items():
                    w.addItem(t, k)
                w.setCurrentIndex(max(0, w.findData(val)))
                self.getters[key] = w.currentData
                form.addRow(label + ":", w)
            elif kind == "mode":
                w = QComboBox()
                for k, t in CASE_MODES.items():
                    w.addItem(t, k)
                w.setCurrentIndex(list(CASE_MODES).index(val) if val in CASE_MODES else 0)
                self.getters[key] = w.currentData
                form.addRow(label + ":", w)
            elif kind in ("text", "format"):
                w = QLineEdit(val or "")
                w.setMinimumWidth(320)
                self.getters[key] = w.text
                form.addRow(label + ":", w)
            elif kind == "bool":
                w = QCheckBox(label)
                w.setChecked(bool(val))
                self.getters[key] = w.isChecked
                form.addRow("", w)
            elif kind == "fields":
                lw = QListWidget()
                for n in FIELDS + [COVER]:
                    it = QListWidgetItem(field_label(n))
                    it.setData(Qt.UserRole, n)
                    it.setCheckState(Qt.Checked if n in (val or []) else Qt.Unchecked)
                    lw.addItem(it)
                lw.setMaximumHeight(240)
                self.getters[key] = lambda lw=lw: [lw.item(i).data(Qt.UserRole) for i in range(lw.count())
                                                   if lw.item(i).checkState() == Qt.Checked]
                form.addRow(label + ":", lw)
        if not FORMS[action["type"]]:
            v.addWidget(QLabel("설정할 항목이 없습니다."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("확인")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.addButton("도움말", QDialogButtonBox.HelpRole).clicked.connect(lambda: HelpDialog.show_help(self))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def result_action(self) -> dict:
        a = {"type": self.action["type"]}
        for k, g in self.getters.items():
            a[k] = g()
        return a


def choose_action_type(parent, pos) -> str | None:
    m = QMenu(parent)
    acts = {m.addAction(t): k for k, t in ACTION_TYPES.items()}
    chosen = m.exec(pos)
    return acts.get(chosen)


class GroupEditDialog(QDialog):
    def __init__(self, parent, group: dict):
        super().__init__(parent)
        self.group = copy.deepcopy(group)
        self.setWindowTitle("액션 그룹 편집")
        self.resize(640, 420)
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self.group.get("name", ""))
        form.addRow("이름:", self.name)
        v.addLayout(form)
        h = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        h.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.btn_add = QPushButton("추가…")
        self.btn_add.clicked.connect(self.add)
        side.addWidget(self.btn_add)
        for text, slot in (("편집…", self.edit), ("삭제", self.delete), ("위로", lambda: self.move(-1)),
                           ("아래로", lambda: self.move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            side.addWidget(b)
        side.addStretch(1)
        h.addLayout(side)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("확인")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.reload()

    def reload(self, select=None):
        self.list.clear()
        for a in self.group.get("actions", []):
            self.list.addItem(describe(a))
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)

    def add(self):
        kind = choose_action_type(self, self.btn_add.mapToGlobal(self.btn_add.rect().bottomLeft()))
        if not kind:
            return
        dlg = ActionEditDialog(self, new_action(kind))
        if dlg.exec():
            self.group.setdefault("actions", []).append(dlg.result_action())
            self.reload(len(self.group["actions"]) - 1)

    def edit(self):
        r = self.list.currentRow()
        if r < 0:
            return
        dlg = ActionEditDialog(self, self.group["actions"][r])
        if dlg.exec():
            self.group["actions"][r] = dlg.result_action()
            self.reload(r)

    def delete(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.group["actions"][r]
            self.reload(min(r, len(self.group["actions"]) - 1))

    def move(self, d):
        r = self.list.currentRow()
        acts = self.group.get("actions", [])
        if 0 <= r < len(acts) and 0 <= r + d < len(acts):
            acts[r], acts[r + d] = acts[r + d], acts[r]
            self.reload(r + d)

    def accept(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "액션 그룹", "이름을 입력하세요.")
            return
        self.group["name"] = name
        super().accept()


class ActionsDialog(QDialog):
    """액션 그룹 목록. 체크한 그룹(없으면 선택한 그룹)을 차례로 실행한다."""

    def __init__(self, parent, store):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("액션")
        self.resize(560, 440)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("실행할 액션 그룹을 체크하세요. 체크한 순서가 아니라 목록 순서대로 실행됩니다."))
        h = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        h.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, slot in (("새로 만들기…", self.new), ("편집…", self.edit), ("복제", self.duplicate),
                           ("삭제", self.delete), ("위로", lambda: self.move(-1)),
                           ("아래로", lambda: self.move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            side.addWidget(b)
        side.addStretch(1)
        h.addLayout(side)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("실행")
        bb.button(QDialogButtonBox.Cancel).setText("닫기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.reload()

    def reload(self, select=None):
        checked = self._checked_names()
        self.list.clear()
        for g in self.store.groups:
            it = QListWidgetItem(f"{g['name']}  ({len(g.get('actions', []))}개)")
            it.setCheckState(Qt.Checked if g["name"] in checked else Qt.Unchecked)
            it.setToolTip("\n".join(describe(a) for a in g.get("actions", [])))
            self.list.addItem(it)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)

    def _checked_names(self):
        return {self.store.groups[i]["name"] for i in range(self.list.count())
                if i < len(self.store.groups) and self.list.item(i).checkState() == Qt.Checked}

    def _unique_name(self, base):
        names = {g["name"] for g in self.store.groups}
        name, n = base, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        return name

    def new(self):
        name, ok = QInputDialog.getText(self, "새 액션 그룹", "이름:")
        if not ok or not name.strip():
            return
        dlg = GroupEditDialog(self, {"name": self._unique_name(name.strip()), "actions": []})
        if dlg.exec():
            self.store.groups.append(dlg.group)
            self.store.save()
            self.reload(len(self.store.groups) - 1)

    def edit(self):
        r = self.list.currentRow()
        if r < 0:
            return
        dlg = GroupEditDialog(self, self.store.groups[r])
        if dlg.exec():
            self.store.groups[r] = dlg.group
            self.store.save()
            self.reload(r)

    def duplicate(self):
        r = self.list.currentRow()
        if r < 0:
            return
        g = copy.deepcopy(self.store.groups[r])
        g["name"] = self._unique_name(g["name"] + " 복사본")
        self.store.groups.insert(r + 1, g)
        self.store.save()
        self.reload(r + 1)

    def delete(self):
        r = self.list.currentRow()
        if r < 0:
            return
        name = self.store.groups[r]["name"]
        if QMessageBox.question(self, "액션 그룹 삭제", f"'{name}' 그룹을 삭제할까요?") != QMessageBox.Yes:
            return
        del self.store.groups[r]
        self.store.save()
        self.reload(min(r, len(self.store.groups) - 1))

    def move(self, d):
        r = self.list.currentRow()
        g = self.store.groups
        if 0 <= r < len(g) and 0 <= r + d < len(g):
            g[r], g[r + d] = g[r + d], g[r]
            self.store.save()
            self.reload(r + d)

    def selected_actions(self) -> tuple[str, list[dict]]:
        idx = [i for i in range(self.list.count()) if self.list.item(i).checkState() == Qt.Checked]
        if not idx and self.list.currentRow() >= 0:
            idx = [self.list.currentRow()]
        groups = [self.store.groups[i] for i in idx]
        actions = [a for g in groups for a in g.get("actions", [])]
        label = ", ".join(g["name"] for g in groups)
        return label, actions

    def accept(self):
        label, actions = self.selected_actions()
        if not actions:
            QMessageBox.information(self, "액션", "실행할 액션 그룹을 체크하거나 선택하세요.")
            return
        super().accept()


class PlansPreview(ConverterDialog):
    """미리 계산한 작업 목록을 보여 주고 확인받는 대화상자."""
    ok_text = "적용"

    def __init__(self, parent, files, settings, title, make):
        self.title = title
        self._make = make
        super().__init__(parent, files, settings)

    def headers(self):
        return ["파일명", "바뀌는 내용", "상태"]

    def make_plans(self):
        return self._make()
