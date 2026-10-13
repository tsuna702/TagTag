"""앨범아트 가져오기와 크기 조정 (앨범태그 artwork_search.py · core.resize_artwork 이식).

인터넷은 사용자가 검색·가져오기를 눌렀을 때만 쓴다.
- Apple Music 공개 검색 페이지 (실패하면 iTunes 공개 검색 API)
- YouTube 공개 oEmbed + 썸네일 (동영상·음원은 내려받지 않음)
- https 이미지 주소
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
from html.parser import HTMLParser
from urllib import error, parse, request

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter

from .tagio import Picture

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
SIZES = [0, 500, 600, 1000]          # 0 = 원본
FITS = {"crop": "중앙 자르기", "contain": "비율 유지 · 여백"}
COUNTRIES = ["KR", "US", "JP", "GB", "DE", "FR", "ES", "IT", "CA", "AU", "TW", "HK"]
USER_AGENT = "LocalTag/0.5"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}


class ArtworkError(Exception):
    pass


# ------------------------------------------------------------------ 이미지 처리

def load_image(data: bytes) -> QImage:
    """EXIF 회전을 적용해 읽는다. 너무 크거나 이미지가 아니면 ArtworkError."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ArtworkError("20 MB보다 큰 이미지는 쓸 수 없습니다.")
    ba = QByteArray(data)
    buf = QBuffer(ba)
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader(buf)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() * size.height() > MAX_IMAGE_PIXELS:
        raise ArtworkError("2,500만 화소보다 큰 이미지는 쓸 수 없습니다.")
    img = reader.read()
    if img.isNull():
        raise ArtworkError("이미지를 읽지 못했습니다. JPEG·PNG·WebP 같은 그림 파일인지 확인해 주세요.")
    return img


def image_format(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:3] == b"GIF":
        return "GIF"
    return "이미지"


def _encode(img: QImage, fmt="JPEG", quality=95) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, fmt, quality)
    return bytes(ba)


def _on_white(img: QImage) -> QImage:
    canvas = QImage(img.size(), QImage.Format_RGB32)
    canvas.fill(QColor("white"))
    p = QPainter(canvas)
    p.drawImage(0, 0, img)
    p.end()
    return canvas


def trim_borders(img: QImage, threshold=24) -> QImage:
    """YouTube 썸네일 위아래·양옆의 검은 띠를 잘라낸다."""
    w, h = img.width(), img.height()
    if w < 16 or h < 16:
        return img

    def dark_row(y):
        step = max(1, w // 64)
        return all(max(QColor(img.pixel(x, y)).getRgb()[:3]) <= threshold for x in range(0, w, step))

    def dark_col(x):
        step = max(1, h // 64)
        return all(max(QColor(img.pixel(x, y)).getRgb()[:3]) <= threshold for y in range(0, h, step))

    top = 0
    while top < h // 3 and dark_row(top):
        top += 1
    bottom = h - 1
    while bottom > h * 2 // 3 and dark_row(bottom):
        bottom -= 1
    left = 0
    while left < w // 3 and dark_col(left):
        left += 1
    right = w - 1
    while right > w * 2 // 3 and dark_col(right):
        right -= 1
    if (top, left, bottom, right) == (0, 0, h - 1, w - 1):
        return img
    return img.copy(QRect(left, top, right - left + 1, bottom - top + 1))


def process(data: bytes, size: int = 0, fit: str = "crop", trim: bool = False) -> Picture:
    """원본(size=0) 또는 size×size 정사각형 JPEG으로 만든다. 원본 데이터는 바꾸지 않는다."""
    img = load_image(data)
    if trim:
        img = trim_borders(img)
    if not size:
        fmt = image_format(data)
        if fmt in ("JPEG", "PNG") and not trim:
            return Picture(data, "image/jpeg" if fmt == "JPEG" else "image/png", 3, "")
        if img.hasAlphaChannel():
            return Picture(_encode(img, "PNG"), "image/png", 3, "")
        return Picture(_encode(img), "image/jpeg", 3, "")
    if size not in SIZES:
        raise ArtworkError("크기는 원본, 500, 600, 1000 중에서 고르세요.")
    img = img.convertToFormat(QImage.Format_ARGB32)
    if fit == "crop":
        scaled = img.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = (scaled.width() - size) // 2
        y = (scaled.height() - size) // 2
        scaled = scaled.copy(QRect(x, y, size, size))
        out = _on_white(scaled)
    else:
        scaled = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        out = QImage(size, size, QImage.Format_RGB32)
        out.fill(QColor("white"))
        p = QPainter(out)
        p.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        p.end()
    return Picture(_encode(out), "image/jpeg", 3, "")


def describe(data: bytes) -> str:
    try:
        img = load_image(data)
    except ArtworkError:
        return ""
    return f"{img.width()}×{img.height()} {image_format(data)}"


# ------------------------------------------------------------------ 네트워크

def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class _HttpsOnly(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl).scheme != "https":
            raise ArtworkError("https가 아닌 주소로 이동하려고 해서 중단했습니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int) -> bytes:
    parts = parse.urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        raise ArtworkError("https 주소만 쓸 수 있습니다.")
    opener = request.build_opener(_HttpsOnly(), request.HTTPSHandler(context=_ssl_context()))
    try:
        with opener.open(request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=15) as resp:
            chunks, count, started = [], 0, time.monotonic()
            while True:
                data = resp.read(min(65536, limit + 1 - count))
                if not data:
                    break
                count += len(data)
                if count > limit or time.monotonic() - started > 45:
                    raise ArtworkError("이미지가 너무 크거나 받는 데 너무 오래 걸립니다.")
                chunks.append(data)
            return b"".join(chunks)
    except error.HTTPError as e:
        raise ArtworkError(f"서버가 요청을 거절했습니다 (HTTP {e.code}).") from e
    except (error.URLError, TimeoutError, OSError) as e:
        raise ArtworkError(f"서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.\n{e}") from e


def _is_apple_image(url):
    host = parse.urlsplit(url).hostname or ""
    return host == "mzstatic.com" or host.endswith(".mzstatic.com")


def normalize_query(query, country="kr"):
    query = query.strip()
    country = country.lower().strip()
    if query.startswith(("https://", "http://")):
        url = parse.urlsplit(query)
        if url.hostname in ("music.apple.com", "itunes.apple.com"):
            m = re.search(r"^/([a-z]{2})/album/(?:[^/]+/)?(?:id)?(\d+)", url.path)
            if m:
                return m[2], m[1], True
            raise ArtworkError("Apple Music 앨범 주소를 넣어 주세요.")
        raise ArtworkError("앨범·아티스트 이름 또는 Apple Music 앨범 주소를 넣어 주세요.")
    if not query or len(query) > 300 or not re.fullmatch(r"[a-z]{2}", country):
        raise ArtworkError("검색어와 국가를 확인해 주세요.")
    return query, country, False


def itunes_albums(query, country="kr"):
    term, region, lookup = normalize_query(query, country)
    params = {"country": region, "media": "music", "entity": "album", "limit": 50}
    params["id" if lookup else "term"] = term
    url = "https://itunes.apple.com/" + ("lookup?" if lookup else "search?") + parse.urlencode(params)
    try:
        resp = json.loads(fetch(url, 4 * 1024 * 1024))
    except (ValueError, UnicodeError) as e:
        raise ArtworkError("검색 결과를 읽지 못했습니다. 잠시 뒤 다시 검색해 주세요.") from e
    out, seen = [], set()
    for item in resp.get("results", []):
        image = item.get("artworkUrl100", "").replace("http://", "https://", 1)
        album_id = item.get("collectionId")
        if not image or not _is_apple_image(image) or not album_id or album_id in seen:
            continue
        seen.add(album_id)
        out.append({"album": item.get("collectionName", ""), "artist": item.get("artistName", ""),
                    "year": item.get("releaseDate", "")[:4], "image": image, "source": "iTunes"})
    return out


class _AlbumPageReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks, self.meta, self.current = [], {}, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and (attrs.get("id") == "serialized-server-data"
                                or attrs.get("type") == "application/ld+json"):
            self.current = []
        if tag == "meta" and attrs.get("property"):
            self.meta[attrs["property"]] = attrs.get("content", "")

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            try:
                self.blocks.append(json.loads("".join(self.current)))
            except ValueError:
                pass
            self.current = None


def page_albums(html: bytes, album_id=None):
    reader = _AlbumPageReader()
    reader.feed(html.decode("utf-8", "replace"))
    if not reader.blocks:
        raise ArtworkError("Apple Music 페이지를 읽지 못했습니다.")
    out, seen = [], set()

    def add(identity, title, artist, image, year=""):
        if not identity or str(identity) in seen or not title:
            return
        if album_id and str(identity) != str(album_id):
            return
        if not image or not _is_apple_image(image):
            return
        seen.add(str(identity))
        out.append({"album": title, "artist": artist, "year": year, "image": image, "source": "Apple Music"})

    def walk(node):
        if isinstance(node, dict):
            desc = node.get("contentDescriptor", {})
            if isinstance(desc, dict) and desc.get("kind") == "album" and node.get("titleLinks"):
                identity = desc.get("identifiers", {}).get("storeAdamID")
                title = node["titleLinks"][0].get("title", "")
                artist = " / ".join(l.get("title", "") for l in node.get("subtitleLinks", []))
                art = node.get("artwork", {}).get("dictionary", {})
                image = art.get("url", "").replace("{w}", "100").replace("{h}", "100").replace("{f}", "jpg")
                add(identity, title, artist, image)
            if album_id and node.get("@type") == "MusicAlbum":
                artist = node.get("byArtist", {})
                if isinstance(artist, list):
                    artist = " / ".join(a.get("name", "") for a in artist if isinstance(a, dict))
                else:
                    artist = artist.get("name", "") if isinstance(artist, dict) else ""
                add(album_id, node.get("name", ""), artist, reader.meta.get("og:image", ""),
                    node.get("datePublished", "")[:4])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for block in reader.blocks:
        walk(block)
    return out


def search_albums(query, country="kr"):
    """Apple Music 공개 페이지에서 검색하고, 실패하면 iTunes 검색으로 대체."""
    term, region, lookup = normalize_query(query, country)
    if lookup:
        url = f"https://music.apple.com/{region}/album/{term}"
    else:
        url = f"https://music.apple.com/{region}/search?" + parse.urlencode({"term": term})
    try:
        results = page_albums(fetch(url, 4 * 1024 * 1024), term if lookup else None)
        if results:
            return results[:1] if lookup else results[:50]
    except (ArtworkError, ValueError, KeyError, TypeError):
        pass
    return itunes_albums(query, country)


def high_res(url: str) -> str:
    """Apple 이미지 주소를 가장 큰 크기로 (서버가 원본 크기까지만 돌려준다)."""
    return re.sub(r"/\d+x\d+[^/]*\.(?:jpg|png|webp)(?:\?.*)?$", "/10000x10000bb.jpg", url)


def youtube_video_id(url: str) -> str:
    parts = parse.urlsplit(url.strip())
    host = parts.hostname or ""
    segs = parts.path.strip("/").split("/")
    vid = ""
    if host in ("youtu.be", "www.youtu.be") and len(segs) == 1:
        vid = segs[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parts.path == "/watch":
            vid = parse.parse_qs(parts.query).get("v", [""])[0]
        elif len(segs) == 2 and segs[0] in ("shorts", "embed", "live"):
            vid = segs[1]
    elif host in ("youtube-nocookie.com", "www.youtube-nocookie.com") and len(segs) == 2 and segs[0] == "embed":
        vid = segs[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ArtworkError("영상 주소를 넣어 주세요. 채널·재생목록 주소로는 썸네일을 가져올 수 없습니다.")
    return vid


def youtube_artwork(url: str) -> tuple[bytes, str]:
    """(이미지 데이터, 영상 제목)"""
    vid = youtube_video_id(url)
    endpoint = "https://www.youtube.com/oembed?" + parse.urlencode(
        {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
    try:
        meta = json.loads(fetch(endpoint, 1024 * 1024))
        title = meta.get("title", "")
    except (ArtworkError, ValueError) as e:
        raise ArtworkError("YouTube 영상 정보를 가져오지 못했습니다. 공개 영상인지 확인해 주세요.") from e
    # 큰 썸네일이 없는 영상도 있다. 없으면 120×90 대체 이미지를 주므로 실제 크기도 확인한다.
    for name in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "mqdefault.jpg"):
        try:
            data = fetch(f"https://i.ytimg.com/vi/{vid}/{name}", MAX_IMAGE_BYTES)
        except ArtworkError:
            continue
        try:
            img = load_image(data)
        except ArtworkError:
            continue
        if img.width() > 120 and img.height() > 90:
            return data, title
    thumb = meta.get("thumbnail_url", "")
    if thumb.startswith("https://"):
        return fetch(thumb, MAX_IMAGE_BYTES), title
    raise ArtworkError("이 영상의 썸네일을 찾지 못했습니다.")


def download(url: str) -> tuple[bytes, str]:
    """주소 종류에 맞게 이미지를 받는다. (데이터, 출처 설명)"""
    url = url.strip()
    host = parse.urlsplit(url).hostname or ""
    if host in YOUTUBE_HOSTS:
        data, title = youtube_artwork(url)
        return data, f"YouTube: {title}" if title else "YouTube"
    if host in ("music.apple.com", "itunes.apple.com"):
        found = search_albums(url)
        if not found:
            raise ArtworkError("Apple Music에서 이 앨범을 찾지 못했습니다.")
        return fetch(high_res(found[0]["image"]), MAX_IMAGE_BYTES), f"Apple Music: {found[0]['album']}"
    if _is_apple_image(url):
        url = high_res(url)
    data = fetch(url, MAX_IMAGE_BYTES)
    load_image(data)  # 그림이 맞는지 확인
    return data, host


# ------------------------------------------------------------------ 최근 이미지

class RecentImages:
    """최근에 가져온 이미지 (최대 40개)."""

    def __init__(self, folder: str, limit: int = 40):
        self.folder, self.limit = folder, limit
        os.makedirs(folder, exist_ok=True)

    def add(self, data: bytes, source: str):
        key = hashlib.sha1(data).hexdigest()[:16]
        path = os.path.join(self.folder, key + ".img")
        with open(path, "wb") as f:
            f.write(data)
        with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
            json.dump({"source": source, "time": time.time()}, f, ensure_ascii=False)
        for old in self.items()[self.limit:]:
            for ext in (".img", ".json"):
                try:
                    os.unlink(old["path"][:-4] + ext)
                except OSError:
                    pass

    def items(self):
        out = []
        for name in os.listdir(self.folder):
            if not name.endswith(".img"):
                continue
            path = os.path.join(self.folder, name)
            try:
                with open(path[:-4] + ".json", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {"source": "", "time": os.path.getmtime(path)}
            out.append({"path": path, **meta})
        return sorted(out, key=lambda x: x.get("time", 0), reverse=True)
