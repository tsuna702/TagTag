"""클래식 작품·악장 나누기: 제목 나누기 규칙, 미리 보기 창, m4a 에 Music 이 읽는 칸(©wrk·©mvn·©mvi·©mvc·shwm)으로 저장."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import app, check, finish, samples, window  # noqa: E402, F401

from mutagen.mp4 import MP4  # noqa: E402

from localtag.classical import changes_for, roman, split_title, totals  # noqa: E402
from localtag.classicaldialog import ClassicalDialog  # noqa: E402
from localtag.tagio import AudioFile, Options  # noqa: E402

CASES = {
    "Waltzes, Op. 64: 2. Tempo Giusto in C-Sharp Minor": ("Waltzes, Op. 64", 2, "Tempo Giusto in C-Sharp Minor"),
    "Harpsichord Concerto No. 5 in F minor, BWV 1056: II. Largo": ("Harpsichord Concerto No. 5 in F minor, BWV 1056", 2, "Largo"),
    "Liebesträume, S. 541: No. 3, Oh Lieb": ("Liebesträume, S. 541", 3, "Oh Lieb"),
    "The Seasons, Op. 37a: No. 6 June: Barcarolle": ("The Seasons, Op. 37a", 6, "June: Barcarolle"),
    "3 Sonetti del Petrarca, S. 270a: III. I' vidi": ("3 Sonetti del Petrarca, S. 270a", 3, "I' vidi"),
    "Symphonische Etüden, Op. 13: VIII. Variation 5 (post.)": ("Symphonische Etüden, Op. 13", 8, "Variation 5 (post.)"),
    "The Goldberg Variations, BWV 988: Aria": ("The Goldberg Variations, BWV 988", 0, "Aria"),
    "Four Impromptus, Op. 90, D. 899: No. 3 in G-Flat Major": ("Four Impromptus, Op. 90, D. 899", 3, "No. 3 in G-Flat Major"),
    "Suite: 10 Pieces": ("Suite", 0, "10 Pieces"),
    "Sonata: I Will": ("Sonata", 0, "I Will"),
    "Milonga Del Angel": None,
    "": None,
}
for title, want in CASES.items():
    check(split_title(title) == want, f"나누기 {title!r} → {split_title(title)}")
check((roman("IV"), roman("XIV"), roman("ab")) == (4, 14, 0), "로마 숫자")
check(totals([("A", 2), ("A", 3), ("B", 1)], "max") == {"A": 3, "B": 1}, "악장 수: 가장 큰 번호")
check(totals([("A", 2), ("A", 3)], "count") == {"A": 2} and totals([("A", 2)], "none") == {}, "악장 수: 곡 수 · 비워 두기")
ch = changes_for({"WORK": ["W"]}, "W", 2, "Largo", 3, True)
check(ch == {"MOVEMENTNAME": ["Largo"], "MOVEMENT": ["2"], "MOVEMENTTOTAL": ["3"], "SHOWMOVEMENT": ["1"]},
      f"이미 같은 값은 빼고 {ch}")
check(changes_for({}, "W", 0, "Aria", 5, False) == {"WORK": ["W"], "MOVEMENTNAME": ["Aria"]}, "번호가 없으면 악장 수도 없음")

# 창: m4a 한 곡에 적용해 Music 이 읽는 칸으로 저장되는지
d = samples()
path = os.path.join(d, "아이유 - 꽃갈피", "03 곡 3.m4a")
opts = Options()
AudioFile.load(path, opts).save({"title": ["Waltzes, Op. 64: 2. Tempo Giusto"]}, None, opts)
w = window()
w.settings.setValue("conv/classical_total", "max")
w.settings.setValue("conv/classical_show", True)
w.settings.setValue("conv/classical_rename", False)
w.load_paths([path], replace=True)
f = next(x for x in w.model.files if x.path == path)
dlg = ClassicalDialog(w, [f], w.settings)
plans = dlg.result_plans()
check(len(plans) == 1 and plans[0].cells[1:5] == ["Waltzes, Op. 64", "2", "2", "Tempo Giusto"], f"미리 보기 {plans[0].cells}")
dlg.skipped.add(id(f))
dlg.refresh()
check(not dlg.result_plans() and dlg.plans[0].cells[-1] == "빼 둠", "고른 줄 빼기")
dlg.skipped.clear()
dlg.refresh()
warned = []
import harness  # noqa: E402
harness.QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[1:3]))
w.apply_plans(dlg.result_plans(), "클래식 작품·악장 나누기")
check(not warned, f"저장 오류 없음 {warned}")
t = MP4(path).tags
check(t.get("©wrk") == ["Waltzes, Op. 64"] and t.get("©mvn") == ["Tempo Giusto"], f"작품·악장 이름 {t.get('©wrk')} {t.get('©mvn')}")
check(t.get("©mvi") == [2] and t.get("©mvc") == [2] and t.get("shwm") == [1], f"번호·악장 수·보기 {t.get('©mvi')} {t.get('©mvc')} {t.get('shwm')}")
check(t.get("©nam") == ["Waltzes, Op. 64: 2. Tempo Giusto"], "제목은 그대로")
g = AudioFile.load(path, opts)
check(g.get("MOVEMENT") == "2" and g.get("SHOWMOVEMENT") == "1", "다시 읽기")
check(not ClassicalDialog(w, [g], w.settings).result_plans(), "두 번 적용해도 바뀌는 것 없음")
finish()
