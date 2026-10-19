"""도구 막대 사용자화: 단추·순서를 설정에 저장하고 다시 만들기, 뺀 단추도 켜짐 상태 갱신이 깨지지 않기, 사용자화 창.

실행: python tests/test_toolbar.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtWidgets import QPushButton  # noqa: E402

from localtag import toolbar as tbm  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
w = window()


def shown():
    out = []
    for a in w.toolbar.actions():
        wd = w.toolbar.widgetForAction(a)
        out.append(wd.text() if isinstance(wd, QPushButton) else "|" if a.isSeparator() else "?")
    return out


check(shown() == [tbm.label_of(k) if k != "|" else "|" for k in tbm.DEFAULT], f"처음엔 기본 배치 {shown()}")

# 바꿔 저장 → 다시 만들기
w.settings.setValue("toolbar_items", ["tag_source", "|", "save", "convert", "없는단추", "undo"])
w._fill_toolbar()
check(shown() == ["태그 가져오기…", "|", "저장", "형식 변환…", "되돌리기"], f"고른 단추만 · 순서대로 · 모르는 키는 뺌 {shown()}")
check(w.tb_redo is not None and w.tb_redo.parent() is None and w.tb_finder is not None, "뺀 단추는 숨은 단추로 남음")

# 켜짐 상태: 곡을 불러오고 저장하면 되돌리기·다시 실행(숨음)이 함께 바뀜
w.load_paths([os.path.join(samples(), "아이유 - 꽃갈피")], replace=True)
app.processEvents()
check(not w.tb_buttons["tag_source"].isEnabled(), "고른 곡이 없으면 태그 가져오기 꺼짐")
w.table.selectRow(0)
app.processEvents()
check(w.tb_buttons["tag_source"].isEnabled() and w.tb_buttons["convert"].isEnabled(), "고르면 켜짐")
w.apply_plans([Plan(w.model.files[0], changes={"comment": ["x"]})], "코멘트")
w.undo()
app.processEvents()
check(w.tb_redo.isEnabled() and "코멘트" in w.tb_redo.toolTip(), "숨은 다시 실행 단추도 갱신")

# 단추를 누르면 그 기능
called = []
w.tag_source = lambda: called.append("tag_source")
w.tb_buttons["tag_source"].click()
check(called == ["tag_source"], "단추 → 기능")

# 배치 바꾸기 (순수 함수)
check(tbm.place(["a", "b", "c"], "c", 2, 0) == ["c", "a", "b"], "도구 막대 안에서 앞으로")
check(tbm.place(["a", "b", "c"], "a", 0, 3) == ["b", "c", "a"], "도구 막대 안에서 끝으로")
check(tbm.place(["save", "undo"], "play", None, 1) == ["save", "play", "undo"], "팔레트에서 가운데로")
check(tbm.place(["save", "undo"], "save", None, 2) == ["undo", "save"], "이미 있는 단추는 옮김")
check(tbm.place(["save", "|"], "|", None, 0) == ["|", "save", "|"], "구분선은 여러 개")
check(tbm.place(["save"], tbm.DEFAULT_SET, None, 0) == tbm.DEFAULT, "기본 세트")
check(tbm.tidy(["|", "save", "|", "|", "undo", "|"]) == ["save", "|", "undo"], "구분선 정리")

# 터치 바처럼: 사용자화 모드에서 실제 도구 막대에 끌어다 놓기
from PySide6.QtCore import QPoint, QPointF, Qt as _Qt  # noqa: E402
import warnings  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
warnings.filterwarnings("ignore", category=DeprecationWarning)

w.settings.setValue("toolbar_items", ["save", "|", "undo"])
w._fill_toolbar()
w.resize(1400, 600)
w.show()
w.customize_toolbar()
app.processEvents()
cust = w._customizer
check(cust is not None and cust.palette.isVisible(), "팔레트가 뜸")
check(all(b.isEnabled() for b in w.tb_buttons.values()), "사용자화하는 동안 단추는 모두 켜짐 (끌 수 있게)")


def drop_at(key, src, x):
    """실제로 끌 때처럼 들어옴 → 움직임 → 놓기 (Qt 는 들어옴을 받아 준 곳에만 놓기를 보낸다)."""
    tb = w.toolbar
    m = tbm._mime(key, src)
    y = tb.height() // 2
    enter = QDragEnterEvent(QPoint(int(x), y), _Qt.MoveAction, m, _Qt.LeftButton, _Qt.NoModifier)
    app.sendEvent(tb, enter)
    move = QDragMoveEvent(QPoint(int(x), y), _Qt.MoveAction, m, _Qt.LeftButton, _Qt.NoModifier)
    app.sendEvent(tb, move)
    check(enter.isAccepted() and move.isAccepted() and cust.indicator.isVisible(), "끌어 오면 들어갈 자리 표시")
    app.sendEvent(tb, QDropEvent(QPointF(x, y), _Qt.MoveAction, m, _Qt.LeftButton, _Qt.NoModifier))
    app.processEvents()
    app.processEvents()


def slots():
    return [ws.text() if hasattr(ws, "text") else "?" for ws in cust.slot_widgets(w.toolbar)]


first = cust.slot_widgets(w.toolbar)[0]
drop_at("play", None, first.geometry().left() + 1)  # 맨 앞에
check(cust.items == ["play", "save", "|", "undo"], f"팔레트 → 도구 막대 맨 앞 {cust.items}")
check(slots()[:2] == ["재생", "저장"], f"도구 막대가 바로 바뀜 {slots()}")
drop_at("play", 0, 10000)  # 도구 막대 안에서 끝으로
check(cust.items == ["save", "|", "undo", "play"], f"순서 바꾸기 {cust.items}")
# 사용자화하는 동안 단추를 눌러도 기능은 돌지 않음
from PySide6.QtTest import QTest  # noqa: E402

played = []
w.play = lambda: played.append(1)
QTest.mouseClick(w.tb_buttons["play"], _Qt.LeftButton)
check(not played, "사용자화 중에는 단추가 눌리지 않음")
# 팔레트 단추를 그냥 누르면 맨 끝에
pal = [b for b in cust.palette.findChildren(tbm._PaletteItem) if b.key == "tag_source"][0]
QTest.mouseClick(pal, _Qt.LeftButton)
app.processEvents()
check(cust.items[-1] == "tag_source", f"팔레트 단추 누르기 → 맨 끝 {cust.items}")
cust.items = cust.items[:-1]
cust.rebuild()
# 도구 막대 밖으로 끌어내기 → 빠짐 (끄는 동작은 흉내)
tbm._start_drag = lambda source, key, src=None: _Qt.IgnoreAction
cust.drag_from_bar(cust.slot_widgets(w.toolbar)[0], 0)
app.processEvents()
check(cust.items == ["|", "undo", "play"], f"밖으로 끌어내면 빠짐 {cust.items}")
# 단추를 눌러도 동작하지 않음 (사용자화 중)
cust.finish(save=True)
app.processEvents()
check(w._customizer is None and w.settings.value("toolbar_items") == ["undo", "play"], f"완료 → 정리해 저장 {w.settings.value('toolbar_items')}")
check(shown() == ["되돌리기", "재생"], f"보통 도구 막대로 {shown()}")

# 취소하면 원래대로
w.customize_toolbar()
w._customizer.items = ["save"]
w._customizer.rebuild()
w._customizer.finish(save=False)
check(w.settings.value("toolbar_items") == ["undo", "play"] and shown() == ["되돌리기", "재생"], "취소 → 그대로")
finish()
