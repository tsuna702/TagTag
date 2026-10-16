"""태그 소스: MusicBrainz 앨범에 커버가 없으면 ① 같은 앨범의 다른 판 ② Apple Music 의 같은 앨범에서 빌려 온다.
인터넷 대신 가짜 응답으로 시험한다.

실행: python tests/test_tagsource_cover_fallback.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish  # noqa: E402

from localtag import tagsource as ts  # noqa: E402

GROUP = "9e45832c-dec8-42b0-ac2d-3878eadeb3a8"
state = {"group_images": [], "apple": []}
calls = []


def fake_json(url, headers=None):
    calls.append(url)
    if url.startswith(ts.CAA + "release-group/"):
        if not state["group_images"]:
            raise ts.SourceError("서버가 요청을 거절했습니다 (HTTP 404).")
        return {"images": state["group_images"]}
    if "itunes.apple.com/search" in url:
        return {"results": state["apple"]}
    return {"id": "r1", "title": "Dragon Ball Kai Complete Song Collection", "artist-credit": [{"name": "Various Artists"}],
            "release-group": {"id": GROUP}, "cover-art-archive": {"front": False},
            "media": [{"position": 1, "tracks": [{"position": i, "title": f"곡 {i}", "length": 200000}
                                                 for i in range(1, 16)]}]}


ts._json = fake_json
ts._mb_last[0] = -10.0
mb = ts.Release("r1", "Dragon Ball Kai Complete Song Collection", "Various Artists", source="mb")

# ① 다른 판에 앞 커버가 있으면 그것
state["group_images"] = [{"front": True}]
rel = ts.get(mb, "kr")
check(rel.group == GROUP and rel.artwork == f"{ts.CAA}release-group/{GROUP}/front", f"다른 판 커버 {rel.artwork}")
check("다른 판" in rel.artwork_from and ts.cover_url(rel, 1000).endswith("/front-1200"), "출처 표시 · 크기")
check(any("term=Dragon+Ball+Kai" in u for u in calls), "Various Artists 는 검색어에서 뺌")

# ② 없으면 Apple Music 에서 이름이 거의 같은 앨범 (곡 수가 같으면 더 믿음)
state["group_images"] = []
state["apple"] = [
    {"collectionName": "Dragon Ball Z Hits", "trackCount": 15, "artworkUrl100": "https://is1.mzstatic.com/a/100x100bb.jpg"},
    {"collectionName": "Dragon Ball Kai Complete Song Collection", "trackCount": 15,
     "artworkUrl100": "https://is1.mzstatic.com/b/100x100bb.jpg"},
]
rel = ts.get(mb, "kr")
check(rel.artwork.endswith("/b/100x100bb.jpg") and "Apple Music" in rel.artwork_from, f"Apple Music 커버 {rel.artwork_from}")
check(ts.cover_url(rel, 1000).endswith("/b/1000x1000bb.jpg"), f"Apple 커버 크기 {ts.cover_url(rel, 1000)}")

# ③ 비슷한 이름이 없으면 빌려 오지 않음
state["apple"] = state["apple"][:1]
rel = ts.get(mb, "kr")
check(not rel.artwork and not rel.artwork_from, "닮은 앨범이 없으면 커버 없음")

# 원래 커버가 있는 MusicBrainz 앨범은 찾지 않음
calls.clear()
has = ts.Release("r2", "x", "y", artwork=ts.CAA + "release/r2/front", source="mb")
check(ts.cover_url(has, 600).endswith("/front-500"), "제 커버는 그대로")
finish()
