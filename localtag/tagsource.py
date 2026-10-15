"""Apple Music(iTunes 공개 API)에서 앨범 정보를 가져와 곡과 짝짓기 (Mp3tag 의 '태그 소스').

키가 필요 없는 iTunes Search/Lookup API 를 쓴다. 인터넷은 사용자가 검색·앨범을 고를 때만 쓴다.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib import parse

from . import artwork as art
from .covermatch import similarity
from .tagio import AudioFile
from .ytclean import strip_noise

COUNTRIES = {"kr": "한국", "jp": "일본", "us": "미국", "gb": "영국"}


class SourceError(Exception):
    pass


@dataclass
class Track:
    disc: int
    discs: int
    num: int
    nums: int
    title: str
    artist: str
    length: float  # 초

    def label(self) -> str:
        n = f"{self.disc}-{self.num:02d}" if self.discs > 1 else f"{self.num:02d}"
        m, s = divmod(int(round(self.length)), 60)
        return f"{n}  {self.title}  ({m}:{s:02d})"


@dataclass
class Release:
    id: str
    album: str
    artist: str
    year: str = ""
    genre: str = ""
    count: int = 0
    artwork: str = ""
    copyright: str = ""
    country: str = ""  # 찾은 스토어 (곡 목록도 먼저 여기서)
    tracks: list = field(default_factory=list)

    def label(self) -> str:
        bits = [x for x in (self.year, f"{self.count}곡" if self.count else "",
                            COUNTRIES.get(self.country, self.country.upper()) if self.country else "") if x]
        return f"{self.album} — {self.artist}" + (f"  ({', '.join(bits)})" if bits else "")


def _json(url):
    try:
        return json.loads(art.fetch(url, 8 * 1024 * 1024))
    except ValueError as e:
        raise SourceError("서버 응답을 읽지 못했습니다. 잠시 뒤 다시 해 보세요.") from e
    except art.ArtworkError as e:
        raise SourceError(str(e)) from e


def album_id_from(text: str):
    """Apple Music 앨범 링크나 숫자 ID → (ID, 국가) 또는 None."""
    s = text.strip()
    if s.isdigit():
        return s, None
    m = re.search(r"music\.apple\.com/([a-z]{2})/album/(?:[^/?]+/)?(?:id)?(\d+)", parse.unquote(s))
    return (m.group(2), m.group(1)) if m else None


def _release(x) -> Release:
    return Release(str(x.get("collectionId", "")), x.get("collectionName", ""), x.get("artistName", ""),
                   (x.get("releaseDate") or "")[:4], x.get("primaryGenreName", ""), int(x.get("trackCount") or 0),
                   x.get("artworkUrl100", ""), x.get("copyright", ""))


def search(term: str, country: str = "kr") -> list[Release]:
    """앨범 검색 (앨범 링크·ID 를 넣으면 그 앨범 하나)."""
    found = album_id_from(term)
    if found:
        return [lookup(found[0], found[1] or country)]
    if not term.strip():
        raise SourceError("검색어를 넣어 주세요.")
    # 한국 스토어는 공개 API 의 앨범 검색이 비어 온다 → 일본·미국 스토어에서 찾고,
    # 고른 스토어에도 있는 앨범은 그 스토어 것으로 표시한다 (곡 목록도 그 스토어 이름으로 받는다)
    for c in dict.fromkeys([country, "jp", "us"]):
        r = _json("https://itunes.apple.com/search?" + parse.urlencode(
            {"term": term.strip(), "country": c, "media": "music", "entity": "album", "limit": 50}))
        out, seen = [], set()
        for x in r.get("results", []):
            rel = _release(x)
            rel.country = c
            if rel.id and rel.id not in seen:
                seen.add(rel.id)
                out.append(rel)
        if out:
            if c != country:
                _localize_releases(out, country)
            return out
    return []


def _lookup_ids(ids, country) -> list:
    """ID 여러 개를 한 스토어에서 (한 번에 너무 많지 않게 나눠서)."""
    res = []
    for i in range(0, len(ids), 100):
        res += _json("https://itunes.apple.com/lookup?" + parse.urlencode(
            {"id": ",".join(ids[i:i + 100]), "country": country})).get("results", [])
    return res


def _localize_releases(rels, country):
    """다른 스토어에서 찾은 앨범 중 고른 스토어에도 있는 것은 그 스토어 이름·국가로."""
    try:
        got = {str(x.get("collectionId")): x for x in _lookup_ids([r.id for r in rels], country)
               if x.get("wrapperType") == "collection"}
    except SourceError:
        return
    for r in rels:
        x = got.get(r.id)
        if x:
            r.artist = x.get("artistName") or r.artist
            r.country = country


def lookup(album_id: str, country: str = "kr") -> Release:
    """앨범 하나와 곡 목록. 한국 등 일부 국가는 곡 목록이 비어 오므로 다른 국가에서 곡 ID 를 받아
    고른 국가로 그 곡들을 다시 찾는다 (곡 ID 로 찾으면 한국 스토어도 한국어 제목을 준다)."""
    album, tracks, home = None, [], None
    for c in dict.fromkeys([country, "jp", "us", "gb"]):
        r = _json("https://itunes.apple.com/lookup?" + parse.urlencode(
            {"id": album_id, "entity": "song", "country": c, "limit": 200}))
        res = r.get("results", [])
        found = next((x for x in res if x.get("wrapperType") == "collection"), None)
        if found and c == country:
            home = found
        album = album or found
        tracks = [x for x in res if x.get("wrapperType") == "track"]
        if album and tracks:
            break
    if not album:
        raise SourceError("앨범 정보를 찾지 못했습니다.")
    if home and c != country:  # 고른 스토어에 있는 앨범 → 곡 이름을 그 스토어 것으로
        try:
            local = {str(x.get("trackId")): x for x in _lookup_ids([str(t.get("trackId")) for t in tracks], country)
                     if x.get("wrapperType") == "track"}
        except SourceError:
            local = {}
        if local:
            tracks = [local.get(str(t.get("trackId")), t) for t in tracks]
            album, c = dict(home), country
            names = Counter(x.get("collectionName") for x in local.values() if x.get("collectionName"))
            if names:
                album["collectionName"] = names.most_common(1)[0][0]
    rel = _release(album)
    rel.country = c
    for t in sorted(tracks, key=lambda t: (t.get("discNumber", 1), t.get("trackNumber", 0))):
        rel.tracks.append(Track(int(t.get("discNumber") or 1), int(t.get("discCount") or 1),
                                int(t.get("trackNumber") or 0), int(t.get("trackCount") or 0),
                                t.get("trackName", ""), t.get("artistName", ""),
                                (t.get("trackTimeMillis") or 0) / 1000))
        if not rel.genre:
            rel.genre = t.get("primaryGenreName", "")
    rel.count = rel.count or len(rel.tracks)
    return rel


def guess_query(files) -> str:
    """고른 곡으로 검색어 만들기: '앨범 아티스트(또는 아티스트) 앨범', 없으면 폴더 이름."""
    pick = lambda name: Counter(f.get(name) for f in files if f.get(name)).most_common(1)  # noqa: E731
    album = pick("album")
    artist = pick("albumartist") or pick("artist")
    if album:
        return " ".join(x[0][0] for x in (artist, album) if x)
    import os
    return os.path.basename(files[0].folder) if files else ""


# ------------------------------------------------------------------ 짝짓기

def _title_of(f: AudioFile) -> str:
    t = f.get("title") or re.sub(r"^\s*\d{1,3}[\s.\-_)]+", "", f.stem)
    return strip_noise(t, keep_cover=True)


def score(f: AudioFile, t: Track) -> float:
    s = similarity(_title_of(f), t.title)
    num = f.get("track").split("/")[0].strip()
    if num.isdigit() and int(num) == t.num:
        disc = f.get("disc").split("/")[0].strip()
        if not disc or (disc.isdigit() and int(disc) == t.disc):
            s += 0.25
    # 제목이 전혀 달라도(번역 제목 등) 트랙 번호와 길이가 맞으면 짝으로 본다
    if f.length and t.length:
        gap = abs(f.length - t.length)
        s += 0.25 if gap <= 3 else (-0.2 if gap > 20 else 0)
    return s


def auto_pairs(files, tracks, threshold=0.45) -> list:
    """곡마다 짝지을 트랙 번호(목록 위치) 또는 None. 점수 높은 짝부터 겹치지 않게 고른다."""
    cands = sorted(((score(f, t), i, j) for i, f in enumerate(files) for j, t in enumerate(tracks)), reverse=True)
    out = [None] * len(files)
    used = set()
    for s, i, j in cands:
        if s < threshold:
            break
        if out[i] is None and j not in used:
            out[i] = j
            used.add(j)
    return out


def ordered_pairs(files, tracks) -> list:
    return [i if i < len(tracks) else None for i in range(len(files))]


def changes_for(f: AudioFile, rel: Release, t: Track, fields: set, with_totals: bool) -> dict:
    """곡 하나에 넣을 태그 (바뀌는 것만)."""
    want = {}
    if "title" in fields:
        want["title"] = t.title
    if "artist" in fields:
        want["artist"] = t.artist
    if "album" in fields:
        want["album"] = rel.album
    if "albumartist" in fields:
        want["albumartist"] = rel.artist
    if "year" in fields and rel.year:
        want["year"] = rel.year
    if "genre" in fields and rel.genre:
        want["genre"] = rel.genre
    if "track" in fields and t.num:
        want["track"] = f"{t.num}/{t.nums}" if with_totals and t.nums else str(t.num)
    if "disc" in fields:
        want["disc"] = f"{t.disc}/{t.discs}" if with_totals else str(t.disc)
    return {k: [v] for k, v in want.items() if v and f.tags.get(k) != [v]}
