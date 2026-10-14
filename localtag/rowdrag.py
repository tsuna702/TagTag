"""표에서 고른 줄을 끌어다 다른 위치에 놓아 순서를 바꾼다 (본 목록 · 자동 번호 매기기 창).

Qt 의 표 끌어다 놓기는 칸을 옮기거나 덮어쓰므로, 끌기 시작·놓을 자리 표시·놓기를 직접 처리한다.
놓을 자리는 줄 사이의 가로선으로 보여 준다. 바깥(Finder 등)에서 끌어온 것은 건드리지 않는다.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QFrame

MIME = "application/x-localtag-rows"


class RowDragger(QObject):
    """view: QTableView/QTableWidget. can_drag() → bool, on_drop(target) — target 은 놓을 자리(그 줄 앞) 행 번호."""

    def __init__(self, view, on_drop, can_drag=lambda: True):
        super().__init__(view)
        self.view = view
        self.on_drop = on_drop
        self.can_drag = can_drag
        self._press = None
        self.line = QFrame(view.viewport())
        self.line.setFrameShape(QFrame.HLine)
        self.line.setStyleSheet("background: palette(highlight); border: none;")
        self.line.setFixedHeight(2)
        self.line.hide()
        view.viewport().installEventFilter(self)
        view.viewport().setAcceptDrops(True)

    def _target(self, y):
        """y 위치에서 놓을 자리 (줄 위쪽 절반이면 그 줄 앞, 아래쪽이면 다음 줄 앞)."""
        n = self.view.model().rowCount()
        row = self.view.rowAt(y)
        if row < 0:
            return n if y > 0 else 0
        top = self.view.rowViewportPosition(row)
        return row + (1 if y > top + self.view.rowHeight(row) / 2 else 0)

    def _show_line(self, target):
        n = self.view.model().rowCount()
        if target < n:
            y = self.view.rowViewportPosition(target)
        else:
            y = self.view.rowViewportPosition(n - 1) + self.view.rowHeight(n - 1) if n else 0
        self.line.setGeometry(0, max(0, y - 1), self.view.viewport().width(), 2)
        self.line.show()
        self.line.raise_()

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton and not e.modifiers():
            ix = self.view.indexAt(e.position().toPoint())
            sm = self.view.selectionModel()
            # 이미 고른 줄을 눌렀을 때만 끌기 후보 (빈 곳·다른 줄은 평소처럼 선택)
            self._press = e.position().toPoint() if ix.isValid() and sm.isRowSelected(ix.row(), ix.parent()) else None
            if self._press is not None:
                return True  # 누르는 순간 선택이 하나로 줄어들지 않게
        elif t == QEvent.MouseMove and self._press is not None and e.buttons() & Qt.LeftButton:
            if (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                if self.can_drag():
                    drag = QDrag(self.view)
                    md = QMimeData()
                    md.setData(MIME, b"1")
                    drag.setMimeData(md)
                    drag.exec(Qt.MoveAction)
                    self.line.hide()
                return True
        elif t == QEvent.MouseButtonRelease and self._press is not None:
            # 끌지 않고 뗐으면 그 줄만 고른다 (평소 클릭과 같게)
            ix = self.view.indexAt(self._press)
            self._press = None
            if ix.isValid():
                self.view.selectRow(ix.row())
            return True
        elif t in (QEvent.DragEnter, QEvent.DragMove) and e.mimeData().hasFormat(MIME):
            if e.source() is self.view:
                e.acceptProposedAction()
                self._show_line(self._target(int(e.position().y())))
                return True
        elif t == QEvent.DragLeave:
            self.line.hide()
        elif t == QEvent.Drop and e.mimeData().hasFormat(MIME):
            self.line.hide()
            if e.source() is self.view:
                e.acceptProposedAction()
                self.on_drop(self._target(int(e.position().y())))
            return True
        return False


def move_to(items, picked_ids, target):
    """items 에서 picked 를 빼서 target(원래 목록의 행 번호) 앞에 순서대로 넣은 새 목록."""
    picked = [x for x in items if id(x) in picked_ids]
    before = sum(1 for x in items[:target] if id(x) in picked_ids)
    rest = [x for x in items if id(x) not in picked_ids]
    at = target - before
    return rest[:at] + picked + rest[at:]
