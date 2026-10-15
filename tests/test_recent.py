"""최근 연 폴더 · 시작할 때 마지막 목록 다시 열기."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402

app = QApplication.instance()
d = samples()
a, b = os.path.join(d, "아이유 - 꽃갈피"), os.path.join(d, "Other")
w = window()
labels = lambda: [x.text() for x in w.recent_menu.actions()]  # noqa: E731
w._fill_recent()
check(labels() == ["(아직 없음)"], f"처음엔 비어 있음 {labels()}")
w.load_paths([a], replace=True)
w.load_paths([b], replace=True)
app.processEvents()
check(w.settings.value("recent_folders") == [b, a], "최근 순서 (나중에 연 것이 위)")
acts = [x for x in w.recent_menu.actions() if x.toolTip() in (a, b)]
check([x.toolTip() for x in acts] == [b, a] and acts[0].shortcut().toString() != "", "메뉴 항목 · 단축키")
w.load_paths([a], replace=True)
app.processEvents()
check(w.settings.value("recent_folders") == [a, b], "다시 연 폴더는 맨 위로 (겹치지 않음)")
# ⇧ 누르고 고르면 추가
QApplication.keyboardModifiers = staticmethod(lambda: Qt.ShiftModifier)
w.open_recent(b)
QApplication.keyboardModifiers = staticmethod(lambda: Qt.NoModifier)
app.processEvents()
check(len(w.model.files) == 5, f"⇧ 로 목록에 추가 ({len(w.model.files)})")
check(w.settings.value("last_list") == [a, b], f"마지막 목록 {w.settings.value('last_list')}")
# 없어진 폴더는 고를 수 없음
os.rename(b, b + "-moved")
w._fill_recent()
gone = [x for x in w.recent_menu.actions() if x.toolTip() == b][0]
check(not gone.isEnabled() and "찾을 수 없음" in gone.text(), "없어진 폴더는 흐리게")
os.rename(b + "-moved", b)
# 다시 켜면 마지막 목록 (옵션 켰을 때만)
w2 = window()
w2.reopen_last()
check(len(w2.model.files) == 0, "옵션이 꺼져 있으면 다시 열지 않음")
w2.settings.setValue("reopen_last", True)
w2.reopen_last()
check(len(w2.model.files) == 5, f"마지막 목록 다시 열기 ({len(w2.model.files)})")
w2.clear_list()
check(w2.settings.value("last_list") in ([], None), "목록 비우면 마지막 목록도 비움")
w._clear_recent()
app.processEvents()
check(labels() == ["(아직 없음)"], "최근 목록 지우기")
finish()
