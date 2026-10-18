"""태그 가져오기: Apple Music 카탈로그에서 작곡가·ISRC·레이블·바코드·발매일·컴필레이션까지 (가짜 응답).

실행: python tests/test_tagsource_details.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag import tagsource as ts  # noqa: E402
from localtag.tagsourcedialog import FIELDS, TagSourceDialog  # noqa: E402

calls = []
state = {"kr": 404}


def base_lookup(album_id, country="kr"):
    rel = ts.Release(album_id, "Get Up", "NewJeans", "2023", "K-Pop", 2, country=country, copyright="℗ 2023 ADOR")
    rel.tracks = [ts.Track(1, 1, 1, 2, "New Jeans", "NewJeans", 108), ts.Track(1, 1, 2, 2, "Super Shy", "NewJeans", 154)]
    return rel


def fake_json(url, headers=None):
    calls.append(url)
    check(headers and headers.get("Authorization") == "Bearer TOKEN", "카탈로그 요청에 토큰")
    if "/kr/" in url and state["kr"] == 404:
        raise ts.SourceError("서버가 요청을 거절했습니다 (HTTP 404).")
    return {"data": [{"attributes": {"recordLabel": "ADOR", "upc": "00602458230871", "releaseDate": "2023-07-21",
                                     "isCompilation": False, "copyright": "℗ 2023 ADOR"},
                      "relationships": {"tracks": {"data": [
                          {"attributes": {"discNumber": 1, "trackNumber": 1, "composerName": "Jinsu Park", "isrc": "USA1"}},
                          {"attributes": {"discNumber": 1, "trackNumber": 2, "composerName": "Frankie Scoca", "isrc": "USA2"}}]}}}]}


ts.lookup = base_lookup
ts._amp_headers = lambda: {"Authorization": "Bearer TOKEN"}
ts._json = fake_json
rel = ts.get(ts.Release("1696228456", "Get Up", "NewJeans"), "kr")
check(any("/kr/" in u for u in calls) and any("/jp/" in u for u in calls), f"한국 스토어에 없으면 다른 스토어로 {calls}")
check((rel.date, rel.record_label, rel.upc, rel.compilation, rel.details) == ("2023-07-21", "ADOR", "00602458230871", False, ""),
      f"앨범 정보 {rel}")
check([(t.composer, t.isrc) for t in rel.tracks] == [("Jinsu Park", "USA1"), ("Frankie Scoca", "USA2")], "곡마다 작곡가 · ISRC")


class F:
    tags = {"composer": ["옛 작곡가"]}


every = {k for k, _ in FIELDS}
ch = ts.changes_for(F(), rel, rel.tracks[1], every, True)
check(ch.get("composer") == ["Frankie Scoca"] and ch.get("ISRC") == ["USA2"] and ch.get("PUBLISHER") == ["ADOR"]
      and ch.get("BARCODE") == ["00602458230871"] and ch.get("year") == ["2023-07-21"] and "COMPILATION" not in ch,
      f"넣을 태그 {ch}")
check(ts.changes_for(F(), rel, rel.tracks[1], every - {"date"}, True).get("year") == ["2023"], "발매일 전체를 끄면 연도만")

# 자세한 정보를 못 받아도 곡 목록은 그대로, 이유를 남긴다
ts._amp_headers = lambda: (_ for _ in ()).throw(ts.SourceError("토큰 없음"))
rel2 = ts.get(ts.Release("1", "x", "y"), "kr")
check(len(rel2.tracks) == 2 and rel2.details == "토큰 없음" and not rel2.tracks[0].composer, "자세한 정보 실패 → 기본 정보만")

# 창: 예전 설정(새 필드를 모르던 때)이 있어도 새 필드는 켜고, 발매일 전체는 끔
app = QApplication.instance()
w = window()
w.load_paths([os.path.join(samples(), "아이유 - 꽃갈피")], replace=True)
app.processEvents()
w.settings.setValue("source/fields", ["title", "artist", "cover"])
w.settings.remove("source/fields_known")
dlg = TagSourceDialog(w, list(w.model.files), w.settings)
on = {k for k, cb in dlg.checks.items() if cb.isChecked()}
check({"title", "artist", "composer", "ISRC", "PUBLISHER"} <= on and not ({"album", "date"} & on), f"새 필드 기본값 {on}")
dlg._set_release(rel)
dlg.pair_ordered()
check(dlg.table.item(0, 6).text() in ("Jinsu Park", "Frankie Scoca"), "작곡가 칸 미리 보기")
check("ADOR" in dlg.album_label.text() and "2023-07-21" in dlg.album_label.text(), "레이블 · 발매일 표시")
dlg.checks["composer"].setChecked(False)
dlg._remember()
dlg2 = TagSourceDialog(w, list(w.model.files), w.settings)
check(not dlg2.checks["composer"].isChecked() and dlg2.checks["ISRC"].isChecked(), "끈 필드는 다음에도 꺼짐")

# 새 필드가 모든 형식에 써지고 다시 읽히는지 (MP3 · FLAC · M4A · OGG)
from localtag.plan import Plan  # noqa: E402

vals = {"composer": ["Frankie Scoca"], "COPYRIGHT": ["℗ 2023 ADOR"], "PUBLISHER": ["ADOR"], "ISRC": ["USA2"],
        "BARCODE": ["00602458230871"], "COMPILATION": ["1"], "year": ["2023-07-21"]}
w.apply_plans([Plan(f, changes=dict(vals)) for f in w.model.files], "자세한 정보")
app.processEvents()
for f in w.model.files:
    got = {k: f.tags.get(k) for k in vals}
    check(got == vals, f"{f.filename}: {got}")
finish()
