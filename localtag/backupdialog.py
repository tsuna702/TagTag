"""백업 관리자."""
from __future__ import annotations

import subprocess

from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .model import fmt_size

STATUS = {"finished": "되돌릴 수 있음", "restored": "되돌림", "running": "중단됨"}


class BackupDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.backups
        self.setWindowTitle("백업")
        self.resize(760, 480)
        v = QVBoxLayout(self)

        opts = QHBoxLayout()
        self.enabled = QCheckBox("저장할 때 원본 파일 백업")
        self.enabled.setChecked(window.backup_enabled)
        self.enabled.toggled.connect(self._set_enabled)
        opts.addWidget(self.enabled)
        opts.addStretch(1)
        opts.addWidget(QLabel("보관 기간:"))
        self.days = QSpinBox()
        self.days.setRange(1, 3650)
        self.days.setSuffix("일")
        self.days.setValue(window.settings.value("backup_days", 30, int))
        self.days.valueChanged.connect(lambda x: window.settings.setValue("backup_days", x))
        opts.addWidget(self.days)
        opts.addWidget(QLabel("최대 용량:"))
        self.gb = QSpinBox()
        self.gb.setRange(1, 1000)
        self.gb.setSuffix(" GB")
        self.gb.setValue(window.settings.value("backup_max_gb", 5, int))
        self.gb.valueChanged.connect(lambda x: window.settings.setValue("backup_max_gb", x))
        opts.addWidget(self.gb)
        v.addLayout(opts)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["날짜", "작업", "파일", "크기", "상태"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._show_detail)
        v.addWidget(self.table, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: gray;")
        v.addWidget(self.detail)
        self.summary = QLabel()
        v.addWidget(self.summary)

        row = QHBoxLayout()
        self.btn_restore = QPushButton("이 작업 되돌리기…")
        self.btn_restore.clicked.connect(self.restore)
        b_open = QPushButton("Finder에서 보기")
        b_open.clicked.connect(self.reveal)
        b_del = QPushButton("삭제")
        b_del.clicked.connect(self.delete)
        b_prune = QPushButton("오래된 백업 정리")
        b_prune.clicked.connect(self.prune)
        for b in (self.btn_restore, b_open, b_del, b_prune):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("닫기")
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.reload()

    def _set_enabled(self, on):
        self.window.backup_enabled = on
        self.window.settings.setValue("backup_enabled", on)

    def reload(self):
        self.jobs = self.store.jobs()
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            cells = [j.get("created", ""), j.get("label", ""), f"{len(j.get('entries', []))}개",
                     fmt_size(j["bytes"]), STATUS.get(j.get("status"), j.get("status", ""))]
            for c, text in enumerate(cells):
                self.table.setItem(r, c, QTableWidgetItem(text))
        total = sum(j["bytes"] for j in self.jobs)
        self.summary.setText(f"백업 {len(self.jobs)}개 · 전체 {fmt_size(total)} · 위치: {self.store.root}")
        if self.jobs:
            self.table.selectRow(0)
        self._show_detail()

    def current(self):
        r = self.table.currentRow()
        return self.jobs[r] if 0 <= r < len(self.jobs) else None

    def _show_detail(self):
        j = self.current()
        self.btn_restore.setEnabled(bool(j) and j.get("status") != "restored")
        if not j:
            self.detail.setText("")
            return
        names = [e["before"].rsplit("/", 1)[-1] for e in j.get("entries", [])]
        more = f" 외 {len(names) - 8}개" if len(names) > 8 else ""
        self.detail.setText("파일: " + ", ".join(names[:8]) + more)

    def restore(self):
        j = self.current()
        if not j:
            return
        changed = self.store.check(j)
        force = False
        if changed:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("백업 되돌리기")
            box.setText(f"{len(changed)}개 파일이 백업 이후 다시 바뀌었거나 없습니다.")
            box.setInformativeText("\n".join(changed[:10]) + ("\n…" if len(changed) > 10 else ""))
            skip = box.addButton("바뀌지 않은 파일만 되돌리기", QMessageBox.AcceptRole)
            over = box.addButton("모두 덮어쓰고 되돌리기", QMessageBox.DestructiveRole)
            box.addButton("취소", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() not in (skip, over):
                return
            force = box.clickedButton() is over
        elif QMessageBox.question(
                self, "백업 되돌리기",
                f"'{j.get('label')}' 작업 전 상태로 {len(j.get('entries', []))}개 파일을 되돌릴까요?") != QMessageBox.Yes:
            return
        done, errors = self.store.restore(j, force=force)
        self.window.after_backup_restore(done)
        if errors:
            QMessageBox.warning(self, "백업 되돌리기",
                                f"{len(done)}개를 되돌렸고 {len(errors)}개는 되돌리지 못했습니다.\n\n" + "\n".join(errors[:20]))
        else:
            QMessageBox.information(self, "백업 되돌리기", f"{len(done)}개 파일을 되돌렸습니다.")
        self.reload()

    def reveal(self):
        j = self.current()
        subprocess.run(["open", j["folder"] if j else self.store.root], check=False)

    def delete(self):
        j = self.current()
        if j and QMessageBox.question(self, "백업 삭제",
                                      f"'{j.get('label')}' 백업({fmt_size(j['bytes'])})을 지울까요?\n"
                                      "지우면 이 작업은 백업으로 되돌릴 수 없습니다.") == QMessageBox.Yes:
            self.store.delete(j)
            self.reload()

    def prune(self):
        n = self.store.prune(self.days.value(), self.gb.value() * 1024 ** 3)
        QMessageBox.information(self, "백업 정리", f"백업 {n}개를 지웠습니다." if n else "지울 백업이 없습니다.")
        self.reload()
