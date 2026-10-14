"""YouTube에서 받은 곡의 제목 정리 (JDownloader 등으로 받은 m4a).

  【MV】아티스트 - 제목 (Official Music Video) [4K]   →  아티스트: 아티스트 · 제목: 제목
  제목 / アーティスト【歌ってみた】                     →  그대로 두는 꼬리말은 keep_cover 로 정한다
  채널 이름 "아티스트 - Topic", "아티스트VEVO"          →  아티스트
"""
from __future__ import annotations

import re
import unicodedata

# 괄호 안에 이 말이 들어 있으면 통째로 지운다 (대소문자 무시)
NOISE = [
    r"official", r"music\s*video", r"m\s*/?\s*v", r"mv", r"pv", r"video", r"audio", r"lyrics?",
    r"lyric\s*video", r"visuali[sz]er", r"hd", r"hq", r"4k", r"8k", r"\d{3,4}p", r"full\s*(ver(sion)?\.?)?",
    r"short\s*ver(sion)?\.?", r"color\s*coded", r"eng\s*sub", r"sub(title)?s?", r"고화질", r"가사", r"공식",
    r"뮤직\s*비디오", r"뮤비", r"公式", r"歌詞(付き)?", r"字幕", r"高音質", r"高画質", r"ミュージックビデオ",
    r"オフィシャル", r"topic", r"free\s*download", r"download", r"jdownloader",
]
# JDownloader 가 파일명에 붙이는 화질 꼬리말 — 괄호 안에 있을 때만 지운다
DOWNLOADER = [r"\d+\s*kbit_\w+", r"bq", r"mq", r"sd", r"maxres"]
# 커버곡 표시 — keep_cover 를 끄면 이것도 지운다
COVER = [r"cover(ed)?(\s*by\s*[^)\]】]*)?", r"歌ってみた", r"弾いてみた", r"커버"]

_OPEN, _CLOSE = "([【「『〔［（<", ")]】」』〕］）>"
_PAIRS = dict(zip(_OPEN, _CLOSE))
# 「」『』는 일본 곡 제목 자체에 자주 쓰여서 안에 잡음 말만 있을 때만 지운다
_ARTIST_SEPS = [" - ", " – ", " — ", " ― ", " ｰ ", " ー ", "－"]


def _is_noise(inner: str, keep_cover: bool) -> bool:
    words = NOISE + DOWNLOADER + ([] if keep_cover else COVER)
    t = unicodedata.normalize("NFKC", inner).strip().casefold()
    if not t:
        return True
    # 꼬리말만으로 이루어졌으면 지운다 ("Official Music Video / 4K" 처럼 여러 개여도)
    rest = re.sub(r"(?:" + "|".join(words) + r")", " ", t, flags=re.I)
    return not re.sub(r"[\W_]+", "", rest)


def strip_noise(s: str, keep_cover: bool = True) -> str:
    """괄호 속 꼬리말과 끝에 붙은 'MV', 'Official Video' 같은 말을 지운다."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c in _PAIRS:
            j = s.find(_PAIRS[c], i + 1)
            if j > 0 and _is_noise(s[i + 1:j], keep_cover):
                out.append(" ")
                i = j + 1
                continue
        out.append(c)
        i += 1
    s = "".join(out)
    tail = NOISE + ([] if keep_cover else COVER)
    # 괄호 없이 끝에 붙은 꼬리말 ("제목 MV", "제목 - Official Video")
    for _ in range(3):
        s2 = re.sub(r"[\s\-|/]+(?:" + "|".join(tail) + r")\s*$", "", s, flags=re.I)
        if s2 == s:
            break
        s = s2
    # 앞에 붙은 "MV" 같은 말 ("MV 제목")
    s = re.sub(r"^\s*(?:mv|m/v|pv|official)\s*[:：\-]?\s+", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s.replace("　", " "))
    return s.strip(" -–—|/·")


def clean_channel(name: str) -> str:
    """채널 이름을 아티스트 이름으로 ("아이유 - Topic" → "아이유", "IUVEVO" → "IU")."""
    n = re.sub(r"\s*-\s*topic\s*$", "", name.strip(), flags=re.I)
    n = re.sub(r"\s*(official\s*(youtube\s*)?(channel)?|vevo)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*(?:公式|オフィシャル)\s*(?:チャンネル|channel)?\s*$", "", n, flags=re.I)
    return n.strip() or name.strip()


def split_title(s: str, order: str = "artist_title"):
    """'아티스트 - 제목' (order="title_artist" 면 '제목 - 아티스트') 또는 '제목 / 아티스트'
    → (아티스트, 제목). 나눌 수 없으면 ("", s)."""
    for sep in _ARTIST_SEPS:
        if sep in s:
            a, t = s.split(sep, 1)
            if order == "title_artist":
                a, t = t, a
            if a.strip() and t.strip():
                return a.strip(), t.strip()
    # 일본 곡에 흔한 '曲名 / 歌手' (가운데 ' / ' 하나만 있을 때)
    if s.count(" / ") == 1:
        t, a = s.split(" / ")
        if a.strip() and t.strip():
            return a.strip(), t.strip()
    return "", s


# 앞뒤에 붙은 괄호 덩어리 (【歌ってみた】 처럼 남기기로 한 것) — 나눌 때 제목 쪽에 붙여 둔다
_EDGE = r"(?:[【\[(（〔][^】\])）〕]*[】\])）〕]\s*)+"


def compile_rx(pattern: str, what: str):
    """사용자가 넣은 정규식. (?<이름>…) 도 받는다 (파이썬은 (?P<이름>…)). 틀리면 ValueError."""
    if not pattern.strip():
        return None
    py = re.sub(r"\(\?<(?![=!])(\w+)>", r"(?P<\1>", pattern)
    try:
        return re.compile(py, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"{what} 정규식 오류: {e}") from e


def _tidy(s: str) -> str:
    return re.sub(r"\s{2,}", " ", s).strip(" -–—|/·")


def regex_split(rx, s: str, order: str = "artist_title"):
    """나누기 정규식: (?P<artist>…) (?P<title>…) 이름 그룹, 없으면 1·2번 그룹을 '제목 모양' 순서로.
    맞지 않으면 None."""
    m = rx.search(s)
    if not m:
        return None
    g = m.groupdict()
    if "artist" in g or "title" in g:
        a, t = g.get("artist") or "", g.get("title") or ""
    elif len(m.groups()) >= 2:
        a, t = m.group(1) or "", m.group(2) or ""
        if order == "title_artist":
            a, t = t, a
    else:
        return None
    return a.strip(), t.strip()


def clean(title: str, artist: str = "", split: bool = True, keep_cover: bool = True,
          overwrite_artist: bool = True, order: str = "artist_title", remove_rx=None, split_rx=None):
    """(새 제목, 새 아티스트). 제목에서 아티스트를 찾지 못하면 원래 아티스트(채널 이름 정리).
    remove_rx: 더 지울 말 (컴파일한 정규식), split_rx: 아티스트·제목 나누기 정규식 (맞으면 기본 규칙보다 먼저)."""
    t = strip_noise(title, keep_cover)
    if remove_rx is not None:
        t = _tidy(remove_rx.sub(" ", t))
    art = clean_channel(artist) if artist else ""
    if split and split_rx is not None:
        r = regex_split(split_rx, t, order)
        if r:
            a, rest = r
            rest = strip_noise(rest, keep_cover)
            if rest and (not a or not artist or overwrite_artist or _same(a, art) or _looks_channel(artist)):
                return rest, (strip_noise(a, keep_cover) if a else art)
    if split:
        head = re.match(r"^\s*" + _EDGE, t)
        pre = head.group(0).strip() if head else ""
        body = t[head.end():] if head else t
        tail = re.search(r"\s*" + _EDGE + r"$", body)
        post = tail.group(0).strip() if tail else ""
        body = body[:tail.start()] if tail else body
        a, rest = split_title(body, order)
        if a:
            rest = " ".join(x for x in (pre, strip_noise(rest, keep_cover), post) if x)
            a = strip_noise(a, keep_cover)
            # 아티스트가 이미 있고 바꾸지 않기로 했으면, 나눈 아티스트가 그 아티스트일 때만 제목에서 뗀다
            if not artist or overwrite_artist or _same(a, art) or _looks_channel(artist):
                return rest, a
    return t, art


def _same(a: str, b: str) -> bool:
    f = lambda x: re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", x).casefold())  # noqa: E731
    return bool(a) and f(a) == f(b)


def _looks_channel(artist: str) -> bool:
    return bool(re.search(r"(-\s*topic|vevo|official|channel|公式|チャンネル|records|music|entertainment)\s*$",
                          artist.strip(), re.I))
