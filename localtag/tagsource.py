"""Apple Music(iTunes 공개 API)·MusicBrainz 에서 앨범 정보를 가져와 곡과 짝짓기 (Mp3tag 의 '태그 소스').

둘 다 키가 필요 없다. MusicBrainz 는 Apple Music 에 없는 앨범(애니·게임 OST 등)을 보충한다.
인터넷은 사용자가 검색·앨범을 고를 때만 쓴다.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib import parse

from . import artwork as art
from .covermatch import similarity
from .tagio import AudioFile
from .ytclean import strip_noise

COUNTRIES = {"kr": "한국", "jp": "일본", "us": "미국", "gb": "영국"}
MB_COUNTRIES = {"xw": "전 세계", "xe": "유럽", "de": "독일", "fr": "프랑스", "tw": "대만", "cn": "중국", "hk": "홍콩"}
SOURCES = {"apple": "Apple Music", "mb": "MusicBrainz"}
MB_ROOT = "https://musicbrainz.org/ws/2/"
# MusicBrainz 는 연락처가 든 User-Agent 를 요구하고 초당 1번까지만 받는다
MB_HEADERS = {"User-Agent": "LocalTag ( https://github.com/tsuna702/TagTag )", "Accept": "application/json"}


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
    composer: str = ""
    isrc: str = ""

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
    country: str = ""  # 찾은 스토어 (곡 목록도 먼저 여기서) · MusicBrainz 는 발매 국가
    tracks: list = field(default_factory=list)
    source: str = "apple"  # apple / mb
    group: str = ""  # MusicBrainz release group (같은 앨범의 여러 판)
    artwork_from: str = ""  # 커버를 다른 곳에서 빌려 왔으면 그 설명 (MusicBrainz 에 커버가 없을 때)
    record_label: str = ""  # 레이블
    upc: str = ""  # 바코드
    date: str = ""  # 발매일 (연-월-일)
    compilation: bool = False
    details: str = ""  # 작곡가 등 자세한 정보를 못 받았으면 그 이유

    def country_name(self) -> str:
        return COUNTRIES.get(self.country) or MB_COUNTRIES.get(self.country) or self.country.upper()

    def label(self) -> str:
        bits = [x for x in (self.year, f"{self.count}곡" if self.count else "",
                            self.country_name() if self.country else "") if x]
        src = "  · MusicBrainz" if self.source == "mb" else ""
        return f"{self.album} — {self.artist}" + (f"  ({', '.join(bits)})" if bits else "") + src


def _json(url, headers=None):
    try:
        return json.loads(art.fetch(url, 8 * 1024 * 1024, headers))
    except ValueError as e:
        raise SourceError("서버 응답을 읽지 못했습니다. 잠시 뒤 다시 해 보세요.") from e
    except art.ArtworkError as e:
        raise SourceError(str(e)) from e


def _together(*calls) -> list:
    """인터넷 요청 여러 개를 동시에 (기다리는 시간이 더해지지 않게). 결과 또는 SourceError 를 순서대로."""
    def run(fn):
        try:
            return fn()
        except SourceError as e:
            return e
    with ThreadPoolExecutor(len(calls)) as ex:
        return list(ex.map(run, calls))


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
    # 고른 스토어에도 있는 앨범은 그 스토어 것으로 표시한다 (곡 목록도 그 스토어 이름으로 받는다).
    # 세 스토어를 동시에 찾고 앞 순서의 결과를 쓴다. 곡 검색도 함께 — 앨범 정보의 이름은 로마자인 경우가 있어
    # (일본 스토어 등) 곡에 붙은 앨범 이름(원래 글자)을 쓴다
    stores = list(dict.fromkeys([country, "jp", "us"]))
    find = lambda c, entity, limit: _json("https://itunes.apple.com/search?" + parse.urlencode(  # noqa: E731
        {"term": term.strip(), "country": c, "media": "music", "entity": entity, "limit": limit}))
    song_stores = [c for c in stores if c != "kr"]  # 한국 스토어는 곡 검색도 비어 온다
    answers = _together(*[lambda c=c: find(c, "album", 50) for c in stores],
                        *[lambda c=c: find(c, "song", 200) for c in song_stores])
    albums, songs = answers[:len(stores)], dict(zip(song_stores, answers[len(stores):]))
    if all(isinstance(x, SourceError) for x in albums):
        raise albums[0]
    for c, r in zip(stores, albums):
        if isinstance(r, SourceError):
            continue
        out, seen = [], set()
        for x in r.get("results", []):
            rel = _release(x)
            rel.country = c
            if rel.id and rel.id not in seen:
                seen.add(rel.id)
                out.append(rel)
        if out:
            got = songs.get(c)
            names = _album_names(out, c, got.get("results", []) if isinstance(got, dict) else [])
            if c != country:
                _localize_releases(out, country, names)
            for rel in out:
                if rel.country == c and rel.id in names:
                    rel.album = names[rel.id][0]
            return out
    return []


def _lookup_ids(ids, country, **extra) -> list:
    """ID 여러 개를 한 스토어에서 (한 번에 너무 많지 않게 나눠서)."""
    res = []
    for i in range(0, len(ids), 100):
        res += _json("https://itunes.apple.com/lookup?" + parse.urlencode(
            {"id": ",".join(ids[i:i + 100]), "country": country, **extra})).get("results", [])
    return res


def _album_names(rels, store, songs) -> dict:
    """앨범 ID → (곡에 붙은 앨범 이름, 그 곡 ID). 곡 검색에 없던 앨범은 앨범마다 곡 하나씩 받아서."""
    names = {}
    for x in songs:
        if x.get("wrapperType") == "track" and x.get("collectionName"):
            names.setdefault(str(x.get("collectionId")), (x["collectionName"], str(x.get("trackId"))))
    missing = [r.id for r in rels if r.id not in names]
    if missing:
        try:
            for x in _lookup_ids(missing, store, entity="song", limit=1):
                if x.get("wrapperType") == "track" and x.get("collectionName"):
                    names.setdefault(str(x.get("collectionId")), (x["collectionName"], str(x.get("trackId"))))
        except SourceError:
            pass
    return names


def _localize_releases(rels, country, names=None):
    """다른 스토어에서 찾은 앨범 중 고른 스토어에도 있는 것은 그 스토어 이름·국가로.
    names(_album_names)가 있으면 그 곡 ID 로 고른 스토어의 앨범 이름도 받는다 (한국 스토어는 곡으로만 한국어 이름을 준다)."""
    tids = [t for t in ((names or {}).get(r.id, (None, None))[1] for r in rels) if t and t != "None"]
    albums, tracks = _together(lambda: _lookup_ids([r.id for r in rels], country),
                               lambda: _lookup_ids(tids, country) if tids else [])
    if isinstance(albums, SourceError):
        return
    got = {str(x.get("collectionId")): x for x in albums if x.get("wrapperType") == "collection"}
    local = {} if isinstance(tracks, SourceError) else {
        str(x.get("collectionId")): x.get("collectionName") for x in tracks
        if x.get("wrapperType") == "track" and x.get("collectionName")}
    for r in rels:
        x = got.get(r.id)
        if x:
            r.artist = x.get("artistName") or r.artist
            r.album = local.get(r.id) or (names or {}).get(r.id, (r.album,))[0]
            r.country = country


def lookup(album_id: str, country: str = "kr") -> Release:
    """앨범 하나와 곡 목록. 한국 등 일부 국가는 곡 목록이 비어 오므로 다른 국가에서 곡 ID 를 받아
    고른 국가로 그 곡들을 다시 찾는다 (곡 ID 로 찾으면 한국 스토어도 한국어 제목을 준다)."""
    album, tracks, home = None, [], None
    stores = list(dict.fromkeys([country, "jp", "us", "gb"]))
    get = lambda c: _json("https://itunes.apple.com/lookup?" + parse.urlencode(  # noqa: E731
        {"id": album_id, "entity": "song", "country": c, "limit": 200}))
    first = dict(zip(stores[:2], _together(*[lambda c=c: get(c) for c in stores[:2]])))  # 앞 두 스토어는 동시에
    for c in stores:
        r = first.get(c) or get(c)
        if isinstance(r, SourceError):
            if c == stores[-1]:
                raise r
            continue
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
            album, c = home, country
    # 앨범 정보의 이름은 로마자일 때가 있다 (일본 스토어 등) → 곡에 붙은 앨범 이름(원래 글자)을 쓴다
    names = Counter(x.get("collectionName") for x in tracks if x.get("collectionName"))
    if names:
        album = dict(album, collectionName=names.most_common(1)[0][0])
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


# ------------------------------------------------------------------ Apple Music 카탈로그 (작곡가 등)

AMP = "https://amp-api.music.apple.com/v1/catalog/"
_amp_lock = threading.Lock()
_amp_token = {"tok": "", "at": 0.0}


def _amp_headers() -> dict:
    """Apple Music 웹 플레이어가 쓰는 공개 토큰 (iTunes 검색 API 에는 작곡가 등이 없다). 30분 동안 다시 쓴다."""
    from . import extract as ex
    with _amp_lock:
        if not _amp_token["tok"] or time.monotonic() - _amp_token["at"] > 1800:
            try:
                html = art.fetch("https://music.apple.com/us/browse", ex.PAGE_LIMIT, ex.BROWSER).decode("utf-8", "replace")
                m = re.search(r'src="(/assets/index[^"]*\.js)"', html)
                js = art.fetch("https://music.apple.com" + m.group(1), ex.JS_LIMIT, ex.BROWSER).decode("utf-8", "replace")
                tok = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", js).group(0)
            except (art.ArtworkError, AttributeError) as e:
                raise SourceError("Apple Music 의 자세한 정보(작곡가 등)를 받지 못했습니다.") from e
            _amp_token.update(tok=tok, at=time.monotonic())
        return {**ex.BROWSER, "Authorization": f"Bearer {_amp_token['tok']}", "Origin": "https://music.apple.com"}


def _amp_album(album_id: str, country: str) -> dict:
    """카탈로그의 앨범과 곡 (작곡가·ISRC·레이블·바코드·발매일·컴필레이션).
    고른 스토어에 없는 앨범이면(404) 일본·미국 스토어에서 찾는다."""
    h = _amp_headers()
    d, err = None, None
    for c in dict.fromkeys([country, "jp", "us"]):
        try:
            d = _json(f"{AMP}{c}/albums/{album_id}?include=tracks", h)
            break
        except SourceError as e:
            err = e
            if "404" not in str(e):
                raise
    if d is None:
        raise SourceError("Apple Music 카탈로그에서 이 앨범의 자세한 정보(작곡가 등)를 찾지 못했습니다.") from err
    album = (d.get("data") or [{}])[0]
    tracks = ((album.get("relationships") or {}).get("tracks") or {})
    songs, nxt = list(tracks.get("data") or []), tracks.get("next")
    while nxt:  # 곡이 많은 앨범은 나눠서 온다
        more = _json("https://amp-api.music.apple.com" + nxt, h)
        songs += more.get("data") or []
        nxt = more.get("next")
    return {"album": album.get("attributes") or {}, "songs": [x.get("attributes") or {} for x in songs]}


def _add_details(rel: Release, got):
    """iTunes 에서 받은 앨범에 카탈로그의 자세한 정보를 붙인다 (디스크·트랙 번호로 짝짓기)."""
    if isinstance(got, SourceError) or not got.get("album"):
        rel.details = str(got) if isinstance(got, SourceError) else "자세한 정보 없음"
        return
    a = got["album"]
    rel.record_label = a.get("recordLabel", "")
    rel.upc = a.get("upc", "")
    rel.date = a.get("releaseDate", "")
    rel.compilation = bool(a.get("isCompilation"))
    rel.copyright = a.get("copyright") or rel.copyright
    by_num = {(int(x.get("discNumber") or 1), int(x.get("trackNumber") or 0)): x for x in got["songs"]}
    for t in rel.tracks:
        x = by_num.get((t.disc, t.num), {})
        t.composer = x.get("composerName", "")
        t.isrc = x.get("isrc", "")


# ------------------------------------------------------------------ MusicBrainz

_mb_lock = threading.Lock()
_mb_last = [0.0]


def _mb_json(path, **params):
    with _mb_lock:  # 초당 1번
        wait = _mb_last[0] + 1.05 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return _json(MB_ROOT + path + "?" + parse.urlencode({**params, "fmt": "json"}), MB_HEADERS)
        finally:
            _mb_last[0] = time.monotonic()


def mbid_from(text: str):
    """MusicBrainz 발매(release) 링크나 MBID → MBID 또는 None."""
    m = re.fullmatch(r"\s*(?:https?://(?:\w+\.)?musicbrainz\.org/release/)?"
                     r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\S*\s*", text.strip(), re.I)
    return m.group(1).lower() if m else None


def _credit(credits) -> str:
    return "".join(a.get("name", "") + a.get("joinphrase", "") for a in credits or [])


def _mb_release(x) -> Release:
    rg = x.get("release-group") or {}
    date = x.get("date") or rg.get("first-release-date") or ""
    cover = (x.get("cover-art-archive") or {}).get("front")
    return Release(x.get("id", ""), x.get("title", ""), _credit(x.get("artist-credit")), date[:4], "",
                   int(x.get("track-count") or sum(m.get("track-count") or 0 for m in x.get("media") or [])),
                   f"https://coverartarchive.org/release/{x.get('id')}/front" if cover else "",
                   country=(x.get("country") or "").lower(), source="mb")


def mb_search(term: str, limit: int = 25) -> list[Release]:
    mbid = mbid_from(term)
    if mbid:
        return [mb_lookup(mbid)]
    q = term.strip().replace('"', " ")
    out = []
    for x in _mb_json("release/", query=q, limit=limit).get("releases", []):
        if (x.get("status") or "") == "Pseudo-Release" or int(x.get("score") or 0) < 40:
            continue
        out.append(_mb_release(x))
    return out


def mb_lookup(mbid: str) -> Release:
    x = _mb_json("release/" + mbid, inc="recordings artist-credits release-groups genres isrcs labels")
    if not x.get("id"):
        raise SourceError("MusicBrainz 에서 앨범 정보를 찾지 못했습니다.")
    rel = _mb_release(x)
    rg = x.get("release-group") or {}
    genres = sorted((x.get("genres") or []) + (rg.get("genres") or []), key=lambda g: -int(g.get("count") or 0))
    rel.genre = genres[0]["name"].title() if genres else ""
    media = [m for m in x.get("media") or [] if m.get("tracks")]
    for m in media:
        for i, t in enumerate(m["tracks"], 1):
            num = int(t["position"]) if str(t.get("position", "")).isdigit() else i
            isrcs = (t.get("recording") or {}).get("isrcs") or []
            rel.tracks.append(Track(int(m.get("position") or 1), len(media), num, len(m["tracks"]),
                                    t.get("title", ""), _credit(t.get("artist-credit")) or rel.artist,
                                    (t.get("length") or 0) / 1000, isrc=isrcs[0] if isrcs else ""))
    rel.count = len(rel.tracks) or rel.count
    rel.group = rg.get("id", "")
    labels = [li.get("label", {}).get("name", "") for li in x.get("label-info") or [] if li.get("label")]
    rel.record_label = next((n for n in labels if n), "")
    rel.upc = x.get("barcode") or ""
    rel.date = x.get("date") or rg.get("first-release-date") or ""
    rel.compilation = (rg.get("secondary-types") or []).count("Compilation") > 0
    return rel


CAA = "https://coverartarchive.org/"


def find_cover(rel: Release, country: str = "kr") -> bool:
    """MusicBrainz 앨범에 커버가 없으면 ① 같은 앨범의 다른 판(release group) 커버
    ② Apple Music 에서 이름이 거의 같은 앨범의 커버를 빌려 온다. 찾으면 True."""
    group = rel.group
    term = rel.album if rel.artist.lower() in ("", "various artists") else f"{rel.artist} {rel.album}"

    def caa():
        if not group:
            return None
        try:
            imgs = _json(f"{CAA}release-group/{group}").get("images", [])
        except SourceError:  # 커버가 하나도 없으면 404
            return None
        return any(i.get("front") for i in imgs)

    def store(c):
        return lambda: _json("https://itunes.apple.com/search?" + parse.urlencode(
            {"term": term, "country": c, "media": "music", "entity": "album", "limit": 25})).get("results", [])

    got = _together(caa, *[store(c) for c in dict.fromkeys([country, "jp", "us"])])
    if got[0] is True:
        rel.artwork = f"{CAA}release-group/{group}/front"
        rel.artwork_from = "MusicBrainz 의 같은 앨범 다른 판"
        return True
    best = None
    for res in got[1:]:
        if isinstance(res, SourceError):
            continue
        for x in res:
            if not x.get("artworkUrl100"):
                continue
            s = similarity(x.get("collectionName", ""), rel.album)
            if rel.count and int(x.get("trackCount") or 0) == rel.count:
                s += 0.1
            if best is None or s > best[0]:
                best = (s, x)
    if best and best[0] >= 0.8:
        rel.artwork = best[1]["artworkUrl100"]
        rel.artwork_from = f"Apple Music 「{best[1].get('collectionName', '')}」"
        return True
    return False


def find(term: str, country: str = "kr", musicbrainz: bool = True) -> list[Release]:
    """Apple Music 결과 뒤에 MusicBrainz 결과를 붙인다 (한쪽이 실패해도 다른 쪽 결과는 보여 준다)."""
    if mbid_from(term):
        return [mb_lookup(mbid_from(term))]
    if album_id_from(term) or not musicbrainz:
        return search(term, country)
    apple, mb = _together(lambda: search(term, country), lambda: mb_search(term))  # 두 곳을 동시에
    if isinstance(mb, SourceError):
        if isinstance(apple, SourceError):
            raise apple
        return apple
    if isinstance(apple, SourceError):
        apple = []
    found = apple + mb
    # 검색어와 앨범 이름이 거의 같은 것을 출처에 상관없이 맨 위로 (나머지는 원래 순서)
    return sorted(found, key=lambda r: similarity(r.album, term) < 0.85)


def get(rel: Release, country: str = "kr") -> Release:
    """검색 결과 하나의 곡 목록까지."""
    if rel.source != "mb":
        # 곡 목록(iTunes)과 작곡가 등 자세한 정보(카탈로그)를 동시에 받는다 — 자세한 정보는 못 받아도 그만
        base, more = _together(lambda: lookup(rel.id, country), lambda: _amp_album(rel.id, country))
        if isinstance(base, SourceError):
            raise base
        _add_details(base, more)
        return base
    got = mb_lookup(rel.id)
    if not got.artwork:
        find_cover(got, country)
    return got


def cover_url(rel: Release, size: int) -> str:
    if rel.artwork.startswith(CAA):  # Cover Art Archive 는 250·500·1200 과 원본만
        return rel.artwork + ("-250" if size <= 250 else "-500" if size <= 600 else "-1200" if size <= 1500 else "")
    from .extract import hi_res
    return hi_res(rel.artwork, size)


# ------------------------------------------------------------------ 검색어

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
    if "date" in fields and rel.date:  # 연도 칸에 발매일 전체 (연도보다 먼저 고르면 이것)
        want["year"] = rel.date
    if "composer" in fields:
        want["composer"] = t.composer
    if "COPYRIGHT" in fields:
        want["COPYRIGHT"] = rel.copyright
    if "PUBLISHER" in fields:
        want["PUBLISHER"] = rel.record_label
    if "ISRC" in fields:
        want["ISRC"] = t.isrc
    if "BARCODE" in fields:
        want["BARCODE"] = rel.upc
    if "COMPILATION" in fields and rel.compilation:
        want["COMPILATION"] = "1"
    if "track" in fields and t.num:
        want["track"] = f"{t.num}/{t.nums}" if with_totals and t.nums else str(t.num)
    if "disc" in fields:
        want["disc"] = f"{t.disc}/{t.discs}" if with_totals else str(t.disc)
    return {k: [v] for k, v in want.items() if v and f.tags.get(k) != [v]}
