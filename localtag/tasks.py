"""오래 걸리는 작업(Music 연동 등)을 백그라운드에서 돌리고 진행률을 보여 준다."""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QProgressDialog


class Task(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel_requested = False

    def run(self):
        try:
            result = self.fn(lambda cur, total, msg="": self.progress.emit(cur, total, msg),
                             lambda: self.cancel_requested)
        except Exception as e:
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(result)


def run_task(parent, title, fn, on_done, on_fail, modal=True, status=None):
    """fn(progress, cancelled) 을 백그라운드에서 실행. modal이면 진행 창을 띄운다."""
    task = Task(fn, parent)
    dlg = None
    if modal:
        dlg = QProgressDialog(title, "취소", 0, 0, parent)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(420)
        dlg.canceled.connect(lambda: setattr(task, "cancel_requested", True))

    def prog(cur, total, msg):
        if dlg:
            dlg.setMaximum(max(total, 1))
            dlg.setValue(min(cur, total))
            if msg:
                dlg.setLabelText(msg)
        elif status and msg:
            status(f"{msg} ({cur}/{total})")

    def finish(handler, value):
        if dlg:
            dlg.reset()
            dlg.close()
        handler(value)
        task.deleteLater()

    task.progress.connect(prog)
    task.succeeded.connect(lambda v: finish(on_done, v))
    task.failed.connect(lambda m: finish(on_fail, m))
    parent._tasks = getattr(parent, "_tasks", set())
    parent._tasks.add(task)
    task.finished.connect(lambda: parent._tasks.discard(task))
    task.start()
    return task
