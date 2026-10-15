"""태그 소스: 한국 스토어를 고르면 일본 스토어에서 찾았더라도 한국 스토어 이름으로 가져온다.

한국 스토어는 공개 API 의 앨범 검색·앨범 곡 목록이 비어 오지만, 곡 ID 로 찾으면 한국어 제목을 준다.
인터넷 대신 가짜 응답으로 시험한다.
"""
import os
import sys
import time
from urllib import parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish  # noqa: E402

from localtag import tagsource as ts  # noqa: E402

ALBUM = {"wrapperType": "collection", "collectionId": 1, "releaseDate": "2014-05-16", "trackCount": 2}
STORE = {
    "jp": {"artistName": "IU", "collectionName": "A Flower Bookmark",
           "tracks": [("나의 옛날이야기", "My Old Story"), ("꽃", "Flower")]},
    "kr": {"artistName": "아이유", "collectionName": "A Flower Bookmark",
           "tracks": [("나의 옛날이야기", "나의 옛날이야기"), ("꽃", "꽃")]},
}
calls = []


def track(c, i):
    return {"wrapperType": "track", "trackId": 10 + i, "collectionId": 1, "trackNumber": i + 1, "discNumber": 1,
            "trackName": STORE[c]["tracks"][i][1], "artistName": STORE[c]["artistName"],
            "collectionName": "꽃갈피" if c == "kr" else "A Flower Bookmark", "trackTimeMillis": 200000}


def fake_json(url):
    q = dict(parse.parse_qsl(parse.urlsplit(url).query))
    c = q["country"]
    if c not in STORE:  # 다른 스토어에는 없음
        return {"results": []}
    calls.append((url.split("?")[0].rsplit("/", 1)[1], c))
    album = dict(ALBUM, artistName=STORE[c]["artistName"], collectionName=STORE[c]["collectionName"])
    if "search" in url:
        return {"results": [] if c == "kr" else [album]}
    ids = q["id"].split(",")
    if q.get("entity") == "song":  # 앨범 + 곡 목록: 한국은 앨범만
        return {"results": [album] + ([] if c == "kr" else [track(c, i) for i in range(2)])}
    out = [album] if "1" in ids else []
    return {"results": out + [track(c, int(x) - 10) for x in ids if x != "1"]}


ts._json = fake_json

rs = ts.search("아이유 꽃갈피", "kr")
check(len(rs) == 1 and rs[0].country == "kr", f"일본에서 찾은 앨범을 한국 스토어로 표시 ({rs[0].country})")
check(rs[0].artist == "아이유", f"검색 결과 아티스트 한국어 ({rs[0].artist})")

rel = ts.lookup(rs[0].id, "kr")
check(rel.country == "kr", f"곡 목록 국가 = 한국 ({rel.country})")
check([t.title for t in rel.tracks] == ["나의 옛날이야기", "꽃"], f"곡 제목 한국어 {[t.title for t in rel.tracks]}")
check(rel.album == "꽃갈피" and rel.artist == "아이유", f"앨범·아티스트 한국어 ({rel.album}, {rel.artist})")

rel = ts.lookup(rs[0].id, "jp")
check(rel.country == "jp" and rel.tracks[0].title == "My Old Story", "일본을 고르면 일본 스토어 그대로")

# 한국 스토어에 없는 앨범은 일본 것으로 (국가 표시도 일본)
ts._json = lambda url: {"results": []} if "country=kr" in url else fake_json(url)
rel = ts.lookup("1", "kr")
check(rel.country == "jp" and rel.tracks[0].title == "My Old Story", "한국에 없는 앨범 → 일본 것 그대로, 일본으로 표시")

# 스토어 여러 곳·MusicBrainz 를 하나씩 기다리지 않고 동시에 찾는다 (요청마다 0.3초 걸린다고 치고)
def slow(url):
    time.sleep(0.3)
    return fake_json(url)


ts._json = slow
ts.mb_search = lambda term: (time.sleep(0.3), [])[1]
t = time.perf_counter()
rs = ts.find("아이유 꽃갈피", "kr")
took = time.perf_counter() - t
check(rs and rs[0].country == "kr" and took < 0.9, f"스토어 3곳 + 이름 바꾸기 + MusicBrainz 를 {took:.2f}초에 (하나씩이면 1.5초)")
t = time.perf_counter()
rel = ts.lookup("1", "kr")
took = time.perf_counter() - t
check(rel.tracks[0].title == "나의 옛날이야기" and took < 0.8, f"곡 목록 {took:.2f}초 (하나씩이면 0.9초)")
finish()
