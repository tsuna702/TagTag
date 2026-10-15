"""태그 소스: MusicBrainz 로 보충 — 검색 합치기, 곡 목록 읽기, 커버 주소, 링크 (인터넷 대신 가짜 응답).

실행: python tests/test_tagsource_musicbrainz.py
"""
import os
import sys
from urllib import parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish  # noqa: E402

from localtag import tagsource as ts  # noqa: E402

MBID = "8ee55e5a-6501-41da-8593-14ed01e5d1b2"
calls = []
apple_down = [False]


def credit(*names):
    return [{"name": n, "joinphrase": " & " if i < len(names) - 1 else ""} for i, n in enumerate(names)]


def fake_json(url, headers=None):
    calls.append((url.split("?")[0], (headers or {}).get("User-Agent", "")))
    q = dict(parse.parse_qsl(parse.urlsplit(url).query))
    if "itunes.apple.com" in url:
        if apple_down[0]:
            raise ts.SourceError("연결 안 됨")
        return {"results": [{"wrapperType": "collection", "collectionId": 9, "collectionName": "Dragon Soul - Single",
                             "artistName": "Someone", "releaseDate": "2014-01-01", "trackCount": 1}]}
    if url.startswith(ts.MB_ROOT + "release/?"):
        check(q["fmt"] == "json" and "Dragon Ball" in q["query"], f"MB 검색어 {q}")
        return {"releases": [
            {"id": MBID, "title": "Dragon Ball Kai Complete Song Collection", "score": 100, "track-count": 3,
             "artist-credit": credit("Various Artists"), "country": "JP", "date": "2011-02-23"},
            {"id": "x", "title": "Dragon Ball Pseudo", "score": 90, "status": "Pseudo-Release"},
            {"id": "y", "title": "Unrelated", "score": 20},
        ]}
    check(url.startswith(ts.MB_ROOT + "release/" + MBID) and "recordings" in q["inc"], f"MB 곡 목록 요청 {url}")
    return {"id": MBID, "title": "Dragon Ball Kai Complete Song Collection", "artist-credit": credit("Various Artists"),
            "country": "JP", "release-group": {"first-release-date": "2011-02-23", "genres": [{"name": "anime", "count": 3}]},
            "genres": [{"name": "j-pop", "count": 1}], "cover-art-archive": {"front": True},
            "media": [{"position": 1, "tracks": [
                {"position": 1, "title": "Dragon Soul", "length": 265533, "artist-credit": credit("Takayoshi Tanimoto")},
                {"position": 2, "title": "Yeah! Break!", "length": 223000, "artist-credit": credit("A", "B")}]},
                      {"position": 2, "tracks": [{"position": 1, "title": "Bonus", "length": None, "artist-credit": []}]}]}


ts._json = fake_json
ts._mb_last[0] = -10.0  # 기다리지 않게 (같은 실행 안에서는 1초 간격을 지킨다)

rels = ts.find("Dragon Ball Kai Complete Song Collection", "kr")
check([r.source for r in rels] == ["mb", "apple"], f"이름이 거의 같은 MB 앨범을 맨 위로 {[r.label() for r in rels]}")
mb = rels[0]
check((mb.album, mb.artist, mb.year, mb.country_name(), mb.count) ==
      ("Dragon Ball Kai Complete Song Collection", "Various Artists", "2011", "일본", 3), f"MB 검색 결과 {mb}")
check(len([r for r in rels if r.source == "mb"]) == 1, "가짜 발매·점수 낮은 결과는 뺌")
check(all("TagTag" in ua for u, ua in calls if "musicbrainz" in u), "MusicBrainz 에는 연락처 User-Agent")

rel = ts.get(mb)
check([(t.disc, t.discs, t.num, t.nums, t.title) for t in rel.tracks] ==
      [(1, 2, 1, 2, "Dragon Soul"), (1, 2, 2, 2, "Yeah! Break!"), (2, 2, 1, 1, "Bonus")], f"디스크·트랙 {rel.tracks}")
check(rel.tracks[1].artist == "A & B" and rel.tracks[2].artist == "Various Artists", "곡 아티스트 (없으면 앨범 아티스트)")
check(abs(rel.tracks[0].length - 265.533) < 0.01 and rel.tracks[2].length == 0, "곡 길이")
check(rel.genre == "Anime", f"장르는 표가 많은 것 {rel.genre}")
check(ts.cover_url(rel, 600).endswith(MBID + "/front-500") and ts.cover_url(rel, 1000).endswith("/front-1200")
      and ts.cover_url(rel, 3000).endswith("/front"), "커버 크기 → Cover Art Archive 크기")
check(ts.changes_for(type("F", (), {"tags": {}})(), rel, rel.tracks[2], {"track", "disc", "album"}, True) ==
      {"album": ["Dragon Ball Kai Complete Song Collection"], "track": ["1/1"], "disc": ["2/2"]}, "적용할 태그")

check(ts.mbid_from(f"https://musicbrainz.org/release/{MBID}/discids") == MBID and ts.mbid_from(MBID.upper()) == MBID
      and ts.mbid_from("Dragon Ball") is None, "MusicBrainz 링크·MBID 알아보기")
calls.clear()
check([r.source for r in ts.find(f"https://musicbrainz.org/release/{MBID}")] == ["mb"]
      and not any("itunes" in u for u, _ in calls), "MB 링크는 그 앨범 하나만")

calls.clear()
check([r.source for r in ts.find("Dragon Ball Kai", "kr", musicbrainz=False)] == ["apple"]
      and not any("musicbrainz" in u for u, _ in calls), "MusicBrainz 끄면 Apple 만")
apple_down[0] = True
check([r.source for r in ts.find("Dragon Ball Kai", "kr")] == ["mb"], "Apple 이 안 돼도 MB 결과는 보여 줌")
finish()
