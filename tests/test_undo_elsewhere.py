"""실행 취소 · 다시 실행 전에 Music 등 다른 곳에서 바뀐 곡을 찾아 묻는다 (모두 / 바뀐 곡은 빼고 / 취소).

다른 곳에서 바꾼 것은 mutagen 으로 파일을 직접 고쳐 흉내 낸다.
실행: python tests/test_undo_elsewhere.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

import mutagen  # noqa: E402

from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
w = window()
w.load_paths([album], replace=True)
app.processEvents()
by = lambda: {f.filename: f for f in w.model.files}  # noqa: E731
asked = []
answer = [None]


def ask(verb, text, can_skip):
    asked.append((verb, text, can_skip))
    return answer[0]


w._ask_elsewhere = ask
orig = {n: f.get("title") for n, f in by().items()}


def save():
    w.apply_plans([Plan(f, changes={"title": [f"새 {f.get('title')}"]}) for f in w.model.files], "제목 바꾸기")
    app.processEvents()


def music_edit(name, title):
    mf = mutagen.File(os.path.join(album, name), easy=True)
    mf["title"] = [title]
    mf.save()


# 1) 아무도 안 바꿨으면 묻지 않고 되돌린다
save()
w.undo()
app.processEvents()
check(not asked and {n: f.get("title") for n, f in by().items()} == orig, "바뀐 곡 없음 → 묻지 않고 되돌림")
w.redo()
app.processEvents()
check(not asked and all(f.get("title").startswith("새 ") for f in w.model.files), "다시 실행도 묻지 않음")
w.undo()
app.processEvents()

# 2) 다른 곳에서 바꾼 곡이 있으면 묻는다 — 취소
save()
music_edit("02 곡 2.flac", "Music 에서 고침")
answer[0] = None
w.undo()
app.processEvents()
check(len(asked) == 1 and "1곡" in asked[0][1] and "02 곡 2.flac" in asked[0][1] and "제목" in asked[0][1],
      f"묻는 글 {asked[-1][1] if asked else ''}")
check(asked[0][2], "일부만 바뀌었으면 '바뀐 곡은 빼고' 고를 수 있음")
check(w.undo_stack and w.undo_stack[-1].label == "제목 바꾸기", "취소 → 기록 그대로")
check(by()["01 곡 1.mp3"].get("title") == "새 곡 1", "취소 → 아무것도 안 되돌림")

# 3) 바뀐 곡은 빼고
answer[0] = "some"
w.undo()
app.processEvents()
import mutagen as _m  # noqa: E402
flac_title = _m.File(os.path.join(album, "02 곡 2.flac"))["title"]
check(by()["01 곡 1.mp3"].get("title") == "곡 1" and flac_title == ["Music 에서 고침"],
      f"바뀐 곡은 그대로, 나머지는 되돌림 ({flac_title})")
check(len(w.redo_stack[-1].items) == 3, "다시 실행 기록에도 3곡만")

# 4) 모두 되돌리기
asked.clear()
w.load_paths([album], replace=True)
app.processEvents()
save()
music_edit("01 곡 1.mp3", "또 고침")
answer[0] = "all"
w.undo()
app.processEvents()
check(len(asked) == 1 and by()["01 곡 1.mp3"].get("title") != "또 고침", "모두 → 다른 곳의 변경도 되돌림")
finish()
