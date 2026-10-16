"""필터에서 그냥 단어로 찾을 때 볼 곳 고르기 (모든 필드 / 제목·파일명 / 아티스트 / 앨범).

앨범 이름에만 단어가 든 곡은 '제목 · 파일명'으로 찾으면 빠져야 한다.
실행: python tests/test_filter_scope.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag.filterexpr import compile_filter  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
d = samples()
w = window()
w.load_paths([os.path.join(d, "아이유 - 꽃갈피")], replace=True)
app.processEvents()
by = {f.filename: f for f in w.model.files}
# 앨범에만 'undertale', 제목에도 든 곡은 하나
w.apply_plans([Plan(f, changes={"album": ["Undertale / Deltarune Covers"]}) for f in w.model.files], "앨범")
w.apply_plans([Plan(next(f for f in w.model.files if f.filename == "02 곡 2.flac"),
                    changes={"title": ["Bonetrousle (From Undertale)"]})], "제목")
app.processEvents()


def shown():
    return sorted(w.proxy.index(r, 0).data() for r in range(w.proxy.rowCount()))


w.set_filter_text("undertale")
w.apply_filter()
check(len(shown()) == 4, f"모든 필드: 앨범에 든 곡도 모두 {shown()}")
w.filter_scope.setCurrentIndex(w.filter_scope.findData("name"))
app.processEvents()
check(shown() == ["02 곡 2.flac"], f"제목 · 파일명: 제목에 든 곡만 {shown()}")
check(w.settings.value("filter_scope") == "name", "고른 곳 기억")
w.filter_scope.setCurrentIndex(w.filter_scope.findData("artist"))
check(shown() == [], "아티스트: 없음")
w.filter_scope.setCurrentIndex(w.filter_scope.findData("album"))
check(len(shown()) == 4, "앨범: 모두")
# 조건식에는 찾을 곳을 쓰지 않음
w.filter_scope.setCurrentIndex(w.filter_scope.findData("name"))
w.set_filter_text("album HAS undertale")
w.apply_filter()
check(len(shown()) == 4, "조건식은 그대로")
f = by["01 곡 1.mp3"]
check(compile_filter("곡 1", "name") is not None and compile_filter("곡", "artist")(w.model.files[0]) is False, "함수 직접")
finish()
