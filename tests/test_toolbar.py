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

# 사용자화 창: 넣기 · 빼기 · 구분선 정리 · 기본값
dlg = tbm.ToolbarDialog(w, tbm.load(w.settings))
check("tag_source" not in [dlg.pool.item(i).data(0x0100) for i in range(dlg.pool.count())], "이미 넣은 단추는 왼쪽에 없음")
dlg.bar.setCurrentRow(dlg.bar.count() - 1)
dlg.add_key("play")
dlg.add_key("play")
check(dlg.items().count("play") == 1 and dlg.items()[-1] == "play", f"넣기 (겹치지 않게) {dlg.items()}")
dlg.bar.setCurrentRow(0)
dlg.remove_selected()
dlg.bar.setCurrentRow(0)
dlg.move(1)
check(dlg.items()[:2] == ["save", "|"], f"빼기 · 순서 바꾸기 {dlg.items()}")
dlg.add_separator()
dlg._accept()
check(dlg.result_items[0] != "|" and "||" not in "".join(dlg.result_items) and dlg.result_items[-1] != "|",
      f"구분선 정리 {dlg.result_items}")
dlg2 = tbm.ToolbarDialog(w, ["save"])
dlg2.set_items(tbm.DEFAULT)
check(dlg2.items() == tbm.DEFAULT, "기본값으로")

# 확인 → 저장 · 다시 만들기
tbm.ToolbarDialog.exec = lambda self: (self.set_items(["save", "|", "play"]), self._accept(), 1)[2]
w.customize_toolbar()
check(w.settings.value("toolbar_items") == ["save", "|", "play"] and shown() == ["저장", "|", "재생"], f"사용자화 적용 {shown()}")
finish()
