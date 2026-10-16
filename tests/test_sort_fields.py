"""정렬용 이름(정렬용 제목·아티스트·앨범·앨범 아티스트): 목록 열·칸에서 고치기·일괄 변경 대상,
ID3v2.3 에서도 Music 이 읽는 프레임(TSOP 등)으로 저장, 실행 취소.

실행: python tests/test_sort_fields.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

import mutagen  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

from localtag.model import COLUMN_KEYS, EDITABLE  # noqa: E402
from localtag.plan import Plan  # noqa: E402
from localtag.renamedialog import TARGETS  # noqa: E402
from localtag.tagio import SORT_FIELDS  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
w = window()
check(w.opts.id3_v23, "기본 저장은 ID3v2.3")
w.load_paths([album], replace=True)
app.processEvents()
by = lambda: {f.filename: f for f in w.model.files}  # noqa: E731

check(all(k in COLUMN_KEYS and k in EDITABLE and k in TARGETS for k in SORT_FIELDS), "열 · 칸에서 고치기 · 일괄 변경 대상")
check(COLUMN_KEYS.index("quality") < COLUMN_KEYS.index("TITLESORT"), "새 열은 끝에 붙임")

vals = {"ARTISTSORT": ["IU"], "ALBUMSORT": ["Kkotgalpi"], "TITLESORT": ["Song"], "ALBUMARTISTSORT": ["IU"]}
w.apply_plans([Plan(f, changes=dict(vals)) for f in w.model.files], "정렬용 이름")
app.processEvents()
for name, f in by().items():
    got = {k: f.tags.get(k) for k in vals}
    check(got == vals, f"{name}: 다시 읽은 정렬용 이름 {got}")

raw = mutagen.File(os.path.join(album, "01 곡 1.mp3"))
check(raw.tags.version[:2] == (2, 3), f"MP3 는 v2.3 그대로 {raw.tags.version}")
check(all(k in raw.tags for k in ("TSOP", "TSOA", "TSOT", "TSO2")) and not raw.tags.getall("TXXX"),
      f"v2.3 에서도 Music 이 읽는 정렬 프레임 {sorted(raw.tags.keys())}")
m4a = mutagen.File(os.path.join(album, "03 곡 3.m4a"))
check(m4a.tags.get("soar") == ["IU"] and m4a.tags.get("soal") == ["Kkotgalpi"], "M4A: soar · soal")
flac = mutagen.File(os.path.join(album, "02 곡 2.flac"))
check(flac.tags.get("ARTISTSORT") == ["IU"], "FLAC: ARTISTSORT")

# 칸에서 고치기
f = by()["01 곡 1.mp3"]
src = w.model.index(w.model.row_of(f), COLUMN_KEYS.index("ARTISTSORT"))
ix = w.proxy.mapFromSource(src)
check(bool(ix.flags() & Qt.ItemIsEditable) and ix.data() == "IU", "정렬용 아티스트 칸")
w.proxy.setData(ix, "Lee Ji-eun")
app.processEvents()
app.processEvents()
check(by()["01 곡 1.mp3"].get("ARTISTSORT") == "Lee Ji-eun", "칸에서 고친 정렬용 아티스트")

# 실행 취소: v2.3 로 되돌려도 정렬 프레임이 남는다
w.undo()
app.processEvents()
check(by()["01 곡 1.mp3"].get("ARTISTSORT") == "IU", "칸 고치기 되돌리기")
raw = mutagen.File(os.path.join(album, "01 곡 1.mp3"))
check("TSOP" in raw.tags and raw.tags.version[:2] == (2, 3), "되돌린 뒤에도 TSOP · v2.3")
w.undo()
app.processEvents()
check(not any(f.get(k) for f in w.model.files for k in SORT_FIELDS), "처음 상태로")
finish()
