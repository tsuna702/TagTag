"""앨범아트 추출 (앨범아트 추출기 앱 이식).

곡마다 앨범아트를 트랙 순서대로 이미지 파일로 저장한다.
- 링크: Apple Music 앨범(공개 iTunes Lookup API)·플레이리스트(웹 플레이어의 공개 토큰, 실패하면 페이지 데이터)
- 내 보관함: Music 앱 보관함의 앨범을 골라 Music이 가진 앨범아트를 저장
- 파일: LocalTag 목록의 곡 파일에 들어 있는 앨범아트
- YouTube: 영상·재생목록 섬네일 (가운데 정사각형 자르기, 검은 띠 제거)
인터넷은 사용자가 추출을 눌렀을 때만 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from urllib import parse

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage

from . import artwork as art
from .tagio import SEP

LINK_SIZES = [600, 1000, 1500, 3000]
YT_QUALITIES = [("maxresdefault", "1280×720"), ("sddefault", "640×480"),
                ("hqdefault", "480×360"), ("mqdefault", "320×180")]
YT_SIDES = [300, 500, 720, 1000]
YT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Cookie": "CONSENT=YES+1; SOCS=CAI",
}
BROWSER = {"User-Agent": "Mozilla/5.0"}
PAGE_LIMIT = 8 * 1024 * 1024
JS_LIMIT = 20 * 1024 * 1024


class ExtractError(Exception):
    pass


@dataclass
class Item:
    """저장할 이미지 하나 (곡 하나)."""
    num: str                 # 파일 이름 앞 번호 ('03', '2-05', '')
    name: str
    url: str | None = None   # 내려받을 주소 (링크·YouTube)
    data: bytes | None = None  # 이미 가진 이미지 (파일)
    pid: str | None = None   # Music persistent ID (보관함)
    status: str = "waiting"  # waiting / done / failed / skipped / duplicate

    def filename(self, ext: str) -> str:
        stem = f"{self.num} {safe_name(self.name)}" if self.num else safe_name(self.name)
        return f"{stem}.{ext}"


@dataclass
class Album:
    """한 폴더에 저장할 묶음."""
    title: str
    artist: str = ""
    items: list = field(default_factory=list)
    key: str = ""

    @property
    def folder(self) -> str:
        return safe_name(f"{self.artist} - {self.title}" if self.artist else self.title)


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip()[:80] or "이름 없음"


def image_ext(data: bytes) -> str:
    b = data[:4]
    if b.startswith(b"\x89PNG"):
        return "png"
    if b.startswith(b"GIF"):
        return "gif"
    if b.startswith(b"BM"):
        return "bmp"
    if b in (b"II*\x00", b"MM\x00*"):
        return "tif"
    return "jpg"


def hi_res(url: str, size: int) -> str:
    """Apple 아트워크 주소의 크기 부분을 size 로 (원본보다 크면 서버가 원본 크기로 준다)."""
    u = url.replace("{w}x{h}bb.{f}", f"{size}x{size}bb.jpg").replace("{w}", str(size)).replace("{h}", str(size))
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", f"/{size}x{size}bb.jpg", u)


def _json(url, headers=None, data=None):
    try:
        return json.loads(art.fetch(url, PAGE_LIMIT, headers, data))
    except ValueError as e:
        raise ExtractError("서버 응답을 읽지 못했습니다.") from e


# ------------------------------------------------------------------ Apple Music 링크

def parse_apple(text: str) -> tuple[str, str, str]:
    """(album|playlist, ID, 국가 코드)"""
    s = text.strip()
    if s.isdigit():
        return "album", s, "us"
    if s.startswith("pl."):
        return "playlist", s, "us"
    m = re.search(r"music\.apple\.com/([a-z]{2})/(album|playlist)/(?:[^/?]+/)?([\w.-]+)", parse.unquote(s))
    if not m:
        raise ExtractError("Apple Music 앨범 또는 플레이리스트 링크가 아닙니다.")
    return m.group(2), m.group(3), m.group(1)


def apple_album(album_id: str, country: str) -> Album:
    album, tracks = None, []
    # 한국 등 일부 국가는 iTunes 스토어 곡 목록이 비어 오므로 다른 국가로 다시 찾는다
    for c in dict.fromkeys([country, "us", "jp", "gb"]):
        r = _json("https://itunes.apple.com/lookup?" + parse.urlencode(
            {"id": album_id, "entity": "song", "country": c, "limit": 200}))
        results = r.get("results", [])
        album = next((x for x in results if x.get("wrapperType") == "collection"), None)
        tracks = [x for x in results if x.get("wrapperType") == "track"]
        if album and tracks:
            break
    if not album or not tracks:
        raise ExtractError("앨범 정보를 찾지 못했습니다.")
    tracks.sort(key=lambda t: (t.get("discNumber", 1), t.get("trackNumber", 0)))
    multi = max(t.get("discCount", 1) for t in tracks) > 1
    a = Album(album.get("collectionName", album_id), album.get("artistName", ""))
    for t in tracks:
        num = f"{t.get('trackNumber', 0):02d}"
        if multi:
            num = f"{t.get('discNumber', 1)}-{num}"
        a.items.append(Item(num, t.get("trackName", "?"), url=t.get("artworkUrl100") or album.get("artworkUrl100")))
    return a


def apple_playlist(pl_id: str, country: str) -> Album:
    html = art.fetch(f"https://music.apple.com/{country}/playlist/{pl_id}", PAGE_LIMIT, BROWSER).decode("utf-8", "replace")
    try:
        title, raw = _playlist_api(html, pl_id, country)
    except (ExtractError, art.ArtworkError):
        title, raw = _playlist_page(html, pl_id)
    width = max(2, len(str(len(raw))))
    a = Album(title)
    for i, (artist, name, url) in enumerate(raw):
        a.items.append(Item(str(i + 1).zfill(width), f"{artist} - {name}" if artist else name, url=url))
    return a


def _playlist_api(html, pl_id, country):
    m = re.search(r'src="(/assets/index[^"]*\.js)"', html)
    if not m:
        raise ExtractError("스크립트 없음")
    js = art.fetch("https://music.apple.com" + m.group(1), JS_LIMIT, BROWSER).decode("utf-8", "replace")
    tok = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", js)
    if not tok:
        raise ExtractError("토큰 없음")
    headers = {**BROWSER, "Authorization": f"Bearer {tok.group(0)}", "Origin": "https://music.apple.com"}
    base = "https://amp-api.music.apple.com"
    meta = _json(f"{base}/v1/catalog/{country}/playlists/{pl_id}", headers)
    name = ((meta.get("data") or [{}])[0].get("attributes") or {}).get("name", pl_id)
    out, path = [], f"/v1/catalog/{country}/playlists/{pl_id}/tracks?limit=100"
    while path:
        d = _json(base + path, headers)
        for t in d.get("data", []):
            at = t.get("attributes", {})
            out.append((at.get("artistName", ""), at.get("name", "?"), (at.get("artwork") or {}).get("url")))
        nxt = d.get("next")
        path = nxt + "&limit=100" if nxt else None
    if not out:
        raise ExtractError("트랙 없음")
    return name, out


def _playlist_page(html, pl_id):
    m = re.search(r'id="serialized-server-data">(.*?)</script>', html, re.S)
    try:
        page = json.loads(m.group(1))["data"][0]["data"]
        sections = page["sections"]
    except (AttributeError, ValueError, KeyError, IndexError, TypeError) as e:
        raise ExtractError("플레이리스트 정보를 찾지 못했습니다. 공개 플레이리스트인지 확인해 보세요.") from e
    title, out = pl_id, []
    for s in sections:
        items = s.get("items") or []
        if str(s.get("id", "")).startswith("playlist-detail-header"):
            title = (items[0].get("title") if items else None) or pl_id
        elif s.get("itemKind") == "trackLockup":
            for t in items:
                artist = ", ".join(x.get("title", "") for x in t.get("subtitleLinks") or [] if x.get("title"))
                url = ((t.get("artwork") or {}).get("dictionary") or {}).get("url")
                out.append((artist, t.get("title", "?"), url))
    if not out:
        raise ExtractError("플레이리스트에서 트랙을 찾지 못했습니다.")
    return title, out


def link_album(text: str) -> Album:
    kind, ident, country = parse_apple(text)
    return apple_album(ident, country) if kind == "album" else apple_playlist(ident, country)


def download_item(item: Item, size: int) -> bytes:
    return art.fetch(hi_res(item.url, size), art.MAX_IMAGE_BYTES)


# ------------------------------------------------------------------ 파일 (LocalTag 목록)

def _num(s: str) -> int:
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0


def file_albums(files) -> list[Album]:
    """곡 파일들을 앨범별로 묶는다. 앨범 태그가 없으면 폴더로, 앨범 아티스트가 없으면 폴더+앨범명으로."""
    groups: dict[str, list] = {}
    for f in files:
        folder = os.path.dirname(f.path)
        album, aa = f.get("album").split(SEP)[0], f.get("albumartist").split(SEP)[0]
        key = f"📁{folder}" if not album else (f"{folder}\x1f{album}" if not aa else f"{aa}\x1f{album}")
        groups.setdefault(key, []).append(f)
    out = []
    for key, fs in groups.items():
        fs.sort(key=lambda f: (_num(f.get("disc")) or 1, _num(f.get("track")), f.filename.lower()))
        first = fs[0]
        title = first.get("album").split(SEP)[0] or os.path.basename(os.path.dirname(first.path))
        aa = first.get("albumartist").split(SEP)[0]
        artists = {f.get("artist") for f in fs}
        artist = aa or (first.get("artist").split(SEP)[0] if len(artists) == 1 else "여러 아티스트")
        multi = len({_num(f.get("disc")) or 1 for f in fs}) > 1
        a = Album(title, artist, key=key)
        for i, f in enumerate(fs):
            n = _num(f.get("track")) or i + 1
            num = f"{n:02d}"
            if multi:
                num = f"{_num(f.get('disc')) or 1}-{num}"
            cover = f.cover
            a.items.append(Item(num, f.get("title").split(SEP)[0] or f.stem, data=cover.data if cover else None))
        out.append(a)
    out.sort(key=lambda a: (a.artist.lower(), a.title.lower()))
    return out


# ------------------------------------------------------------------ Music 보관함

LIBRARY_JS = '''
var m = Application("Music");
var t = m.libraryPlaylists[0].tracks;
JSON.stringify({pid: t.persistentID(), name: t.name(), album: t.album(), aa: t.albumArtist(),
                artist: t.artist(), disc: t.discNumber(), num: t.trackNumber()});
'''

# 곡의 앨범아트 원본을 <폴더>/<순번> 파일로 쓴다 (AppleScript 결과로 큰 데이터를 주고받지 않게)
ARTWORK_SCRIPT = '''
on saveData(d, p)
    set f to open for access (POSIX file p) with write permission
    try
        set eof f to 0
        write d to f
        close access f
    on error e
        close access f
        error e
    end try
end saveData

on run arguments
    set outDir to item 1 of arguments
    set pids to rest of arguments
    set i to 0
    tell application "Music"
        repeat with pidRef in pids
            set i to i + 1
            try
                set t to (first track of library playlist 1 whose persistent ID is (contents of pidRef))
                if (count of artworks of t) > 0 then
                    set d to raw data of artwork 1 of t
                    my saveData(d, outDir & "/" & i)
                end if
            on error errText number errNum
                if errNum is -1743 then error errText number errNum
            end try
            log "LOCALTAG_PROGRESS:" & i
        end repeat
    end tell
    return "DONE"
end run
'''


def library_albums() -> list[Album]:
    from . import music
    music.ensure_access()
    out = music._osascript_js(LIBRARY_JS, timeout=300)
    try:
        d = json.loads(out)
    except ValueError as e:
        raise ExtractError("Music 보관함 목록을 읽지 못했습니다.") from e
    order, by_key = [], {}
    for i in range(len(d["pid"])):
        title, aa, artist = d["album"][i] or "", d["aa"][i] or "", d["artist"][i] or ""
        # 앨범 아티스트가 없으면 앨범 이름만으로 묶는다 (컴필레이션이 곡마다 갈라지지 않게)
        key = f"{aa}\x1f{title}"
        a = by_key.get(key)
        if a is None:
            a = by_key[key] = Album(title or "(앨범 이름 없음)", aa or artist, key=key)
            order.append(key)
        elif not aa and a.artist != artist:
            a.artist = "여러 아티스트"
        a.items.append((max(1, d["disc"][i] or 0), d["num"][i] or 0, len(a.items), d["pid"][i], d["name"][i] or "?"))
    albums = []
    for key in order:
        a = by_key[key]
        rows = sorted(a.items)
        multi = len({r[0] for r in rows}) > 1
        a.items = []
        for k, (disc, num, _, pid, name) in enumerate(rows):
            n = f"{num if num > 0 else k + 1:02d}"
            a.items.append(Item(f"{disc}-{n}" if multi else n, name, pid=pid))
        albums.append(a)
    albums.sort(key=lambda a: (a.artist.lower(), a.title.lower()))
    return albums


def library_artworks(pids, progress=lambda n: None, cancelled=lambda: False) -> list[bytes | None]:
    """곡(persistent ID)들의 앨범아트 원본. 없는 곡은 None."""
    from . import music
    out: list[bytes | None] = []
    for start in range(0, len(pids), 40):
        batch = pids[start:start + 40]
        with tempfile.TemporaryDirectory(prefix="localtag-art-") as tmp:
            music._run_script(ARTWORK_SCRIPT, [tmp, *batch], lambda n, s=start: progress(s + n), cancelled)
            for i in range(1, len(batch) + 1):
                p = os.path.join(tmp, str(i))
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                except OSError:
                    data = None
                out.append(data if data and len(data) > 16 else None)
    return out


# ------------------------------------------------------------------ YouTube

def parse_youtube(text: str) -> tuple[str | None, str | None]:
    """(영상 ID, 재생목록 ID)"""
    s = text.strip()
    if re.fullmatch(r"[\w-]{11}", s):
        return s, None
    v = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", s)
    lst = re.search(r"[?&]list=([\w-]+)", s)
    lst = lst.group(1) if lst else None
    if lst and lst.startswith("RD"):
        lst = None  # 자동 믹스는 끝이 없는 목록이라 뺀다
    return (v.group(1) if v else None), lst


def youtube_title(vid: str) -> str:
    try:
        j = _json("https://www.youtube.com/oembed?" + parse.urlencode(
            {"format": "json", "url": f"https://www.youtube.com/watch?v={vid}"}), YT_HEADERS)
        return j.get("title") or vid
    except (ExtractError, art.ArtworkError):
        return vid


def _find_all(o, key, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                acc.append(v)
            _find_all(v, key, acc)
    elif isinstance(o, list):
        for v in o:
            _find_all(v, key, acc)
    return acc


def youtube_playlist(list_id: str) -> tuple[str, list[tuple[str, str]]]:
    """(제목, [(영상 ID, 제목)]) — 100개씩 이어서 끝까지."""
    html = art.fetch(f"https://www.youtube.com/playlist?list={list_id}&hl=ko", PAGE_LIMIT, YT_HEADERS).decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    try:
        data = json.loads(m.group(1))
    except (AttributeError, ValueError) as e:
        raise ExtractError("재생목록을 읽지 못했습니다. 공개(또는 일부 공개) 재생목록인지 확인해 보세요.") from e
    meta = _find_all(data, "playlistMetadataRenderer", [])
    title = (meta[0].get("title") if meta and isinstance(meta[0], dict) else None) or list_id
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    version = ver.group(1) if ver else "2.20250101.00.00"
    videos, seen = [], set()

    def collect(o):
        for lv in _find_all(o, "lockupViewModel", []):
            if not isinstance(lv, dict) or lv.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
                continue
            vid = lv.get("contentId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            t = (((lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title") or {}).get("content")
            videos.append((vid, t or vid))

    def next_token(o):
        toks = _find_all(_find_all(o, "continuationItemViewModel", []), "token", [])
        return next((t for t in toks if isinstance(t, str)), None)

    collect(data)
    token = next_token(data)
    while token:
        body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": version, "hl": "ko"}},
                           "continuation": token}).encode()
        try:
            more = _json("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                         {**YT_HEADERS, "Content-Type": "application/json"}, body)
        except (ExtractError, art.ArtworkError):
            break
        before = len(videos)
        collect(more)
        token = next_token(more) if len(videos) > before else None
    if not videos:
        raise ExtractError("재생목록에서 영상을 찾지 못했습니다.")
    return title, videos


def youtube_items(text: str, whole_list: bool) -> tuple[Album, bool]:
    """(묶음, 영상 하나인지)"""
    vid, lst = parse_youtube(text)
    if lst and (whole_list or not vid):
        title, videos = youtube_playlist(lst)
        width = max(2, len(str(len(videos))))
        a = Album(title)
        a.items = [Item(str(i + 1).zfill(width), t, url=v) for i, (v, t) in enumerate(videos)]
        return a, False
    if vid:
        title = youtube_title(vid)
        return Album(title, items=[Item("", title, url=vid)]), True
    raise ExtractError("YouTube 영상 또는 재생목록 링크가 아닙니다.")


def youtube_thumbnail(vid: str, quality: int) -> bytes:
    """고른 화질부터 시도해 있는 것 중 가장 좋은 섬네일."""
    for name, _ in YT_QUALITIES[quality:]:
        try:
            data = art.fetch(f"https://i.ytimg.com/vi/{vid}/{name}.jpg", art.MAX_IMAGE_BYTES, YT_HEADERS)
        except art.ArtworkError:
            continue
        img = QImage.fromData(data)
        if img.width() > 120:  # 없는 화질이면 120×90 대체 이미지를 준다
            return data
    raise ExtractError("섬네일 없음")


def _content_rect(img: QImage) -> QRect:
    """실제 그림 영역 (검은 띠를 걷어낸다).
    1) 4:3 섬네일(640×480, 480×360)은 16:9 영상 위아래에 정해진 높이의 검은 띠가 붙어 있다
    2) 영상 자체의 시네마 비율 검은 띠는 위아래 중 얇은 쪽 두께만큼 양쪽을 똑같이 자른다
       (어두운 장면이 띠로 잘못 잘리지 않게 완전히 검은 줄만 띠로 본다)"""
    w, h = img.width(), img.height()
    step = max(1, w // 160)

    def row(y):  # 그 줄의 밝기 (85번째 백분위 — 띠 안의 작은 로고·JPEG 잡티는 무시)
        v = sorted(max(QColor(img.pixel(x, y)).getRgb()[:3]) for x in range(0, w, step))
        return v[min(len(v) - 1, len(v) * 85 // 100)]

    top, bottom = 0, h
    band = (h - w * 9 // 16) // 2
    if abs(w * 3 - h * 4) <= 4 and band > 2 and all(
            row(y) < 24 for y in list(range(0, band - 2)) + list(range(h - band + 2, h))):
        top, bottom = band, h - band
    t = b = 0
    while t < (bottom - top) // 4 and row(top + t) < 10:
        t += 1
    while b < (bottom - top) // 4 and row(bottom - 1 - b) < 10:
        b += 1
    trim = min(t, b)
    return QRect(0, top + trim, w, bottom - top - trim * 2)


def center_square(data: bytes, side: int) -> bytes:
    img = QImage.fromData(data)
    if img.isNull():
        raise ExtractError("이미지를 읽지 못했습니다.")
    r = _content_rect(img)
    s = min(r.width(), r.height())
    cx, cy = r.x() + r.width() // 2, r.y() + r.height() // 2
    sq = img.copy(QRect(cx - s // 2, cy - s // 2, s, s))
    out = sq.scaled(side, side, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return art._encode(out.convertToFormat(QImage.Format_RGB32), "JPEG", 95)
