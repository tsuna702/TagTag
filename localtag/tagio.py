"""오디오 파일 태그 읽기/쓰기 (mutagen 기반).

포맷마다 다른 태그 체계(ID3, Vorbis Comment, MP4, APEv2, ASF)를
공통 필드 이름(title, artist, ...)으로 다룰 수 있게 감싸 준다.
"""
from __future__ import annotations

import base64
import copy
import os
import shutil
import struct
import threading
import unicodedata
import weakref
from dataclasses import dataclass, field

import mutagen

from .safewrite import safe_modify
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue, APEv2File
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.flac import Picture as FlacPicture
from mutagen.id3 import APIC, COMM, ID3, TCON, TXXX, USLT, Frames
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.ogg import OggFileType
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE

# 공통 필드 (Mp3tag 기본 태그 패널과 같은 구성)
FIELDS = [
    "title", "artist", "album", "albumartist", "year",
    "track", "disc", "genre", "composer", "comment",
]

FIELD_LABELS = {
    "title": "제목",
    "artist": "아티스트",
    "album": "앨범",
    "albumartist": "앨범 아티스트",
    "year": "연도",
    "track": "트랙",
    "disc": "디스크",
    "genre": "장르",
    "composer": "작곡가",
    "comment": "코멘트",
}

# Mp3tag에서 쓰는 다른 이름도 받아 준다
FIELD_ALIASES = {
    "date": "year",
    "tracknumber": "track",
    "discnumber": "disc",
    "band": "albumartist",
    "album artist": "albumartist",
    "album_artist": "albumartist",
    "description": "comment",
}
EXT_ALIASES = {"LYRICS": "UNSYNCEDLYRICS", "UNSYNCED LYRICS": "UNSYNCEDLYRICS"}


def canonical_field(name: str) -> str | None:
    """기본 필드 이름(소문자)이면 그 이름, 아니면 None."""
    n = name.strip().lower()
    n = FIELD_ALIASES.get(n, n)
    return n if n in FIELDS else None


def field_key(name: str) -> str:
    """기본 필드는 소문자(title), 확장 필드는 대문자(LYRICIST) 키로."""
    c = canonical_field(name)
    if c:
        return c
    n = name.strip().upper()
    return EXT_ALIASES.get(n, n)


# 여러 값을 가진 필드를 한 줄로 표시할 때 쓰는 구분자 (Mp3tag과 동일)
SEP = "\\\\"

AUDIO_EXTS = {
    ".mp3", ".mp2", ".flac", ".ogg", ".oga", ".opus", ".spx",
    ".m4a", ".m4b", ".m4r", ".mp4", ".wma", ".asf",
    ".wav", ".aif", ".aiff", ".aifc", ".ape", ".wv", ".mpc",
    ".ofr", ".tta", ".dsf",
}

PICTURE_TYPES = {
    0: "기타", 1: "파일 아이콘", 2: "기타 아이콘", 3: "앞 표지", 4: "뒷 표지",
    5: "리플릿", 6: "미디어", 7: "리드 아티스트", 8: "아티스트", 9: "지휘자",
    10: "밴드", 11: "작곡가", 12: "작사가", 13: "녹음 장소", 14: "녹음 중",
    15: "공연 중", 16: "영상 캡처", 17: "물고기", 18: "일러스트", 19: "밴드 로고",
    20: "레이블 로고",
}


@dataclass
class Options:
    id3_v23: bool = True        # True: ID3v2.3 (UTF-16), False: ID3v2.4 (UTF-8)
    write_id3v1: bool = False   # ID3v1 태그도 함께 쓰기
    cp949_fallback: bool = True  # Latin-1로 저장된 ID3 텍스트를 CP949로 해석
    keep_mtime: bool = False    # 저장 후 파일 수정 시각 유지
    safe_save: bool = True      # 사본에 쓰고 오디오 데이터를 검증한 뒤 교체


@dataclass
class Picture:
    data: bytes
    mime: str = "image/jpeg"
    type: int = 3
    desc: str = ""


# 같은 앨범의 곡들은 보통 같은 커버를 갖고 있다. 내용이 같은 이미지는 메모리에 한 번만 두고 함께 쓴다
# (곡마다 1MB 커버를 따로 들고 있으면 수천 곡을 열 때 수 GB가 된다). bytes 는 바뀌지 않으므로 함께 써도 안전하다.
_shared_lock = threading.Lock()
_shared: dict[tuple, list] = {}
_dead: list = []  # 사라진 커버의 키 (weakref 콜백은 아무 스레드에서나 불리므로 여기 모아 두고 나중에 정리)


def _picture(data: bytes, mime: str, ptype: int, desc: str) -> Picture:
    data = bytes(data)
    if len(data) < 1024:
        return Picture(data, mime, ptype, desc)
    key = (len(data), data[:64], data[-64:])
    with _shared_lock:
        while _dead:
            k = _dead.pop()
            refs = _shared.get(k)
            if refs is not None:
                refs[:] = [r for r in refs if r() is not None]
                if not refs:
                    del _shared[k]
        refs = _shared.setdefault(key, [])
        for r in refs:
            other = r()
            if other is not None and (other.data is data or other.data == data):
                return Picture(other.data, mime, ptype, desc)
        pic = Picture(data, mime, ptype, desc)
        refs.append(weakref.ref(pic, lambda _r, key=key: _dead.append(key)))
        return pic


def _share(data: bytes) -> bytes:
    """메모리에 이미 같은 이미지가 있으면 그 bytes 를 돌려준다 (실행 취소 스냅샷의 커버용)."""
    if len(data) < 1024:
        return data
    with _shared_lock:
        for r in _shared.get((len(data), data[:64], data[-64:]), ()):
            other = r()
            if other is not None and (other.data is data or other.data == data):
                return other.data
    return data


def guess_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def nfc(s: str) -> str:
    # macOS 파일명은 한글이 자모 분리(NFD)되어 있는 경우가 많다
    return unicodedata.normalize("NFC", s)


def _fix_cp949(s: str) -> str:
    if not any(ord(c) > 127 for c in s):
        return s
    try:
        return s.encode("latin-1").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


# ---------------------------------------------------------------- ID3

ID3_MAP = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
    "year": "TDRC", "track": "TRCK", "disc": "TPOS", "genre": "TCON",
    "composer": "TCOM",
}
ID3_CLASSES = (MP3, TrueAudio, WAVE, AIFF, DSF)
# 확장 필드 이름 (Mp3tag 이름) ↔ ID3 프레임
ID3_EXT = {
    "LYRICIST": "TEXT", "PUBLISHER": "TPUB", "BPM": "TBPM", "COPYRIGHT": "TCOP",
    "ENCODEDBY": "TENC", "ORIGARTIST": "TOPE", "CONDUCTOR": "TPE3", "MIXARTIST": "TPE4",
    "ISRC": "TSRC", "SUBTITLE": "TIT3", "CONTENTGROUP": "TIT1", "GROUPING": "GRP1",
    "MOOD": "TMOO", "INITIALKEY": "TKEY", "LANGUAGE": "TLAN", "MEDIATYPE": "TMED",
    "ORIGYEAR": "TDOR", "RELEASETIME": "TDRL", "ALBUMSORT": "TSOA", "ARTISTSORT": "TSOP",
    "TITLESORT": "TSOT", "ALBUMARTISTSORT": "TSO2", "COMPOSERSORT": "TSOC",
    "COMPILATION": "TCMP", "ENCODERSETTINGS": "TSSE", "ORIGALBUM": "TOAL",
    "ORIGFILENAME": "TOFN", "ORIGLYRICIST": "TOLY", "RADIOSTATION": "TRSN",
    "FILEOWNER": "TOWN", "SETSUBTITLE": "TSST", "MOVEMENTNAME": "MVNM",
}
ID3_EXT_REV = {v: k for k, v in ID3_EXT.items()}
# ID3v2.3에는 없는 프레임 (v2.3으로 저장하면 mutagen이 지운다)
ID3_V24_ONLY = {"TMOO", "TDRL", "TSOA", "TSOP", "TSOT", "TSST", "TPRO", "TDEN", "TDTG", "TSO2", "TSOC"}
# 화면에 따로 보이지 않는 텍스트 프레임 (다른 필드에 포함되거나 자동 관리됨)
ID3_HIDDEN = {"TLEN", "TDTG", "TDEN", "TFLT", "TIPL", "TMCL", "TPRO", "TRSO", "TDAT", "TIME", "TYER", "TRDA", "TSIZ", "TORY"}


def _id3_text(frame, opts: Options) -> list[str]:
    vals = frame.genres if isinstance(frame, TCON) else [str(t) for t in frame.text]
    if opts.cp949_fallback and getattr(frame, "encoding", None) == 0:
        vals = [_fix_cp949(v) for v in vals]
    return vals


def _id3_read(tags: ID3, opts: Options):
    out = {}
    for name, fid in ID3_MAP.items():
        frame = tags.get(fid)
        if frame is not None:
            out[name] = _id3_text(frame, opts)
    for frame in tags.getall("COMM"):
        if frame.desc == "":
            out["comment"] = _id3_text(frame, opts)
            break
    common = set(ID3_MAP.values())
    for frame in tags.values():
        fid = frame.FrameID
        if fid == "TXXX":
            name = (frame.desc or "TXXX").upper()
        elif fid == "USLT":
            name = "UNSYNCEDLYRICS"
            if name in out:
                continue
            text = frame.text
            if opts.cp949_fallback and frame.encoding == 0:
                text = _fix_cp949(text)
            out[name] = [text]
            continue
        elif fid in ID3_EXT_REV:
            name = ID3_EXT_REV[fid]
        elif (fid.startswith("T") or fid in ("GRP1", "MVNM")) and fid not in common and fid not in ID3_HIDDEN:
            name = fid
        else:
            continue
        if hasattr(frame, "text") and name not in out:
            out[name] = _id3_text(frame, opts)
    pics = [_picture(f.data, f.mime or guess_mime(f.data), int(f.type), f.desc)
            for f in tags.getall("APIC")]
    return out, pics


def _id3_write(tags: ID3, changes, pictures, opts: Options):
    enc = 1 if opts.id3_v23 else 3
    if opts.cp949_fallback:
        # 화면에 보이는 대로 저장되도록, CP949로 해석된 Latin-1 프레임은 유니코드로 다시 쓴다
        for frame in tags.values():
            if getattr(frame, "encoding", None) == 0 and hasattr(frame, "text"):
                try:
                    old = [str(t) for t in frame.text]
                except Exception:
                    continue
                new = [_fix_cp949(v) for v in old]
                if new != old:
                    frame.text = new
                    frame.encoding = enc
    for name, vals in changes.items():
        if name == "comment":
            for key in [k for k, f in tags.items() if k.startswith("COMM") and f.desc == ""]:
                del tags[key]
            if vals:
                tags.add(COMM(encoding=enc, lang="eng", desc="", text=vals))
        elif name in ID3_MAP:
            fid = ID3_MAP[name]
            tags.delall(fid)
            if vals:
                tags.add(Frames[fid](encoding=enc, text=vals))
        elif name == "UNSYNCEDLYRICS":
            tags.delall("USLT")
            if vals:
                tags.add(USLT(encoding=enc, lang="eng", desc="", text="\n".join(vals)))
        else:
            fid = ID3_EXT.get(name) or (name if name in Frames and name[:1] in "TGM" and len(name) == 4
                                        and name != "TXXX" else None)
            if fid:
                tags.delall(fid)
            # 같은 필드가 TXXX로도 있으면 지운다 (기존 설명의 대소문자는 유지)
            desc = name
            for key in [k for k, f in tags.items() if f.FrameID == "TXXX" and f.desc.upper() == name]:
                desc = tags[key].desc
                del tags[key]
            if not vals:
                continue
            if fid and not (opts.id3_v23 and fid in ID3_V24_ONLY):
                tags.add(Frames[fid](encoding=enc, text=vals))
            else:
                # 사용자 정의 필드, 또는 v2.3에 없는 프레임 → TXXX
                tags.add(TXXX(encoding=enc, desc=desc, text=vals))
    if pictures is not None:
        tags.delall("APIC")
        for p in pictures:
            tags.add(APIC(encoding=enc, mime=p.mime, type=p.type, desc=p.desc, data=p.data))


# ---------------------------------------------------------------- Vorbis Comment (FLAC, Ogg)

VORBIS_MAP = {
    "title": "TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUMARTIST",
    "year": "DATE", "track": "TRACKNUMBER", "disc": "DISCNUMBER", "genre": "GENRE",
    "composer": "COMPOSER", "comment": "COMMENT",
}
VORBIS_TOTALS = {"track": ("TRACKTOTAL", "TOTALTRACKS"), "disc": ("DISCTOTAL", "TOTALDISCS")}
VORBIS_HIDDEN = {"METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME",
                 "TRACKTOTAL", "TOTALTRACKS", "DISCTOTAL", "TOTALDISCS"}
VORBIS_EXT = {"UNSYNCEDLYRICS": "LYRICS", "INITIALKEY": "KEY", "ORIGYEAR": "ORIGINALDATE"}
VORBIS_EXT_REV = {v: k for k, v in VORBIS_EXT.items()}


def _vorbis_read(tags):
    out = {}
    for name, key in VORBIS_MAP.items():
        vals = tags.get(key)
        if not vals and name == "comment":
            vals = tags.get("DESCRIPTION")
        if vals:
            out[name] = list(vals)
    for name, total_keys in VORBIS_TOTALS.items():
        if name in out and "/" not in out[name][0]:
            for tk in total_keys:
                if tags.get(tk):
                    out[name] = [f"{out[name][0]}/{tags[tk][0]}"]
                    break
    common = set(VORBIS_MAP.values())
    for key in tags.keys():
        k = key.upper()
        if k in common or k in VORBIS_HIDDEN or (k == "DESCRIPTION" and "COMMENT" not in tags):
            continue
        name = VORBIS_EXT_REV.get(k, k)
        if name not in out:
            out[name] = list(tags[key])
    return out


def _vorbis_write(tags, changes):
    for name, vals in changes.items():
        key = VORBIS_MAP.get(name) or VORBIS_EXT.get(name, name)
        if name in VORBIS_TOTALS:
            for tk in VORBIS_TOTALS[name]:
                if tk in tags:
                    del tags[tk]
            if vals and "/" in vals[0]:
                num, total = vals[0].split("/", 1)
                vals = [num] if num else []
                if total:
                    tags[VORBIS_TOTALS[name][0]] = [total]
        if key in tags:
            del tags[key]
        if vals:
            tags[key] = vals


def _flac_pic(p: Picture) -> FlacPicture:
    fp = FlacPicture()
    fp.data, fp.mime, fp.type, fp.desc = p.data, p.mime, p.type, p.desc
    return fp


def _ogg_read_pics(tags):
    pics = []
    for b64 in tags.get("METADATA_BLOCK_PICTURE", []):
        try:
            fp = FlacPicture(base64.b64decode(b64))
        except Exception:
            continue
        pics.append(_picture(fp.data, fp.mime or guess_mime(fp.data), int(fp.type), fp.desc))
    return pics


def _ogg_write_pics(tags, pictures):
    for key in ("METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME"):
        if key in tags:
            del tags[key]
    if pictures:
        tags["METADATA_BLOCK_PICTURE"] = [
            base64.b64encode(_flac_pic(p).write()).decode("ascii") for p in pictures
        ]


# ---------------------------------------------------------------- MP4

MP4_MAP = {
    "title": "©nam", "artist": "©ART", "album": "©alb", "albumartist": "aART",
    "year": "©day", "genre": "©gen", "composer": "©wrt", "comment": "©cmt",
}
MP4_PAIRS = {"track": "trkn", "disc": "disk"}
MP4_EXT = {
    "UNSYNCEDLYRICS": "©lyr", "GROUPING": "©grp", "BPM": "tmpo", "COPYRIGHT": "cprt",
    "ENCODEDBY": "©too", "ALBUMSORT": "soal", "ARTISTSORT": "soar", "TITLESORT": "sonm",
    "ALBUMARTISTSORT": "soaa", "COMPOSERSORT": "soco", "COMPILATION": "cpil",
    "DESCRIPTION": "desc", "WORK": "©wrk", "MOVEMENTNAME": "©mvn",
}
MP4_EXT_REV = {v: k for k, v in MP4_EXT.items()}
FREEFORM = "----:com.apple.iTunes:"


def _mp4_str(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _pair_to_str(pair) -> str:
    n, total = (tuple(pair) + (0, 0))[:2]
    if total:
        return f"{n}/{total}" if n else f"/{total}"
    return str(n) if n else ""


def _str_to_pair(s: str):
    num, _, total = s.partition("/")
    try:
        return (int(num or 0), int(total or 0))
    except ValueError:
        return None


def _mp4_read(tags):
    out = {}
    for name, key in MP4_MAP.items():
        if key in tags:
            out[name] = [str(v) for v in tags[key]]
    if "genre" not in out and "gnre" in tags:
        idx = tags["gnre"][0] - 1
        if 0 <= idx < len(TCON.GENRES):
            out["genre"] = [TCON.GENRES[idx]]
    for name, key in MP4_PAIRS.items():
        if key in tags and tags[key]:
            s = _pair_to_str(tags[key][0])
            if s:
                out[name] = [s]
    for key, vals in tags.items():
        if key in MP4_EXT_REV:
            name = MP4_EXT_REV[key]
        elif key.startswith(FREEFORM):
            name = key[len(FREEFORM):].upper()
        else:
            continue
        if name not in out:
            seq = vals if isinstance(vals, list) else [vals]
            out[name] = [_mp4_str(v) for v in seq]
    pics = []
    for c in tags.get("covr", []):
        mime = "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else guess_mime(bytes(c))
        pics.append(_picture(c, mime, 3, ""))
    return out, pics


def _mp4_write(tags, changes, pictures):
    for name, vals in changes.items():
        if name in MP4_PAIRS:
            key = MP4_PAIRS[name]
            tags.pop(key, None)
            pair = _str_to_pair(vals[0]) if vals else None
            if pair and any(pair):
                tags[key] = [pair]
        elif name in MP4_MAP:
            key = MP4_MAP[name]
            tags.pop(key, None)
            if name == "genre":
                tags.pop("gnre", None)
            if vals:
                tags[key] = vals
        elif name in MP4_EXT:
            key = MP4_EXT[name]
            tags.pop(key, None)
            if not vals:
                continue
            if key == "tmpo":
                try:
                    tags[key] = [int(float(vals[0]))]
                except ValueError:
                    pass
            elif key == "cpil":
                tags[key] = vals[0].strip().lower() in ("1", "true", "yes")
            else:
                tags[key] = vals
        else:
            key = FREEFORM + name
            for k in [k for k in tags.keys() if k.startswith(FREEFORM) and k[len(FREEFORM):].upper() == name]:
                key = k
                del tags[k]
            if vals:
                tags[key] = [MP4FreeForm(v.encode("utf-8")) for v in vals]
    if pictures is not None:
        tags.pop("covr", None)
        if pictures:
            tags["covr"] = [
                MP4Cover(p.data, MP4Cover.FORMAT_PNG if p.mime == "image/png" else MP4Cover.FORMAT_JPEG)
                for p in pictures
            ]


# ---------------------------------------------------------------- APEv2

APE_MAP = {
    "title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album Artist",
    "year": "Year", "track": "Track", "disc": "Disc", "genre": "Genre",
    "composer": "Composer", "comment": "Comment",
}
APE_COVER_KEYS = {3: "Cover Art (Front)", 4: "Cover Art (Back)"}


def _ape_read(tags):
    out, pics = {}, []
    for name, key in APE_MAP.items():
        if key in tags:
            try:
                out[name] = list(tags[key])
            except TypeError:
                pass
    common = {v.lower() for v in APE_MAP.values()}
    for key, value in tags.items():
        if key.lower() in common or isinstance(value, APEBinaryValue):
            continue
        name = "UNSYNCEDLYRICS" if key.upper() == "LYRICS" else key.upper()
        try:
            out.setdefault(name, list(value))
        except TypeError:
            pass
    for key, value in tags.items():
        if key.lower().startswith("cover art") and isinstance(value, APEBinaryValue):
            raw = bytes(value.value)
            _, _, data = raw.partition(b"\x00")
            ptype = 4 if "back" in key.lower() else 3 if "front" in key.lower() else 0
            pics.append(_picture(data, guess_mime(data), ptype, ""))
    return out, pics


def _ape_write(tags, changes, pictures):
    for name, vals in changes.items():
        key = APE_MAP.get(name) or ("Lyrics" if name == "UNSYNCEDLYRICS" else name)
        if key in tags:  # APE 키는 대소문자를 구분하지 않는다
            del tags[key]
        if vals:
            tags[key] = vals
    if pictures is not None:
        for key in [k for k in tags.keys() if k.lower().startswith("cover art")]:
            del tags[key]
        for p in pictures:
            key = APE_COVER_KEYS.get(p.type, "Cover Art (Other)")
            ext = "png" if p.mime == "image/png" else "jpg"
            tags[key] = APEBinaryValue(f"{key}.{ext}".encode() + b"\x00" + p.data)


# ---------------------------------------------------------------- ASF (WMA)

ASF_MAP = {
    "title": "Title", "artist": "Author", "album": "WM/AlbumTitle",
    "albumartist": "WM/AlbumArtist", "year": "WM/Year", "track": "WM/TrackNumber",
    "disc": "WM/PartOfSet", "genre": "WM/Genre", "composer": "WM/Composer",
    "comment": "Description",
}


ASF_EXT = {
    "UNSYNCEDLYRICS": "WM/Lyrics", "PUBLISHER": "WM/Publisher", "BPM": "WM/BeatsPerMinute",
    "COPYRIGHT": "Copyright", "ENCODEDBY": "WM/EncodedBy", "CONDUCTOR": "WM/Conductor",
    "ISRC": "WM/ISRC", "MOOD": "WM/Mood", "CONTENTGROUP": "WM/ContentGroupDescription",
    "SUBTITLE": "WM/SubTitle", "LYRICIST": "WM/Writer", "ALBUMSORT": "WM/AlbumSortOrder",
    "ARTISTSORT": "WM/ArtistSortOrder", "TITLESORT": "WM/TitleSortOrder",
    "INITIALKEY": "WM/InitialKey", "LANGUAGE": "WM/Language",
}
ASF_EXT_REV = {v.lower(): k for k, v in ASF_EXT.items()}


def _asf_parse_pic(b: bytes):
    ptype = b[0]
    pos = 5

    def read_wstr():
        nonlocal pos
        end = pos
        while b[end:end + 2] != b"\x00\x00":
            end += 2
        s = b[pos:end].decode("utf-16-le", "replace")
        pos = end + 2
        return s

    mime = read_wstr()
    desc = read_wstr()
    data = b[pos:]
    return _picture(data, mime or guess_mime(data), ptype, desc)


def _asf_pack_pic(p: Picture) -> bytes:
    return (struct.pack("<bi", p.type, len(p.data))
            + p.mime.encode("utf-16-le") + b"\x00\x00"
            + p.desc.encode("utf-16-le") + b"\x00\x00" + p.data)


def _asf_read(tags):
    out = {}
    for name, key in ASF_MAP.items():
        if key in tags:
            # 같은 값이 ASF 헤더의 두 곳에 중복 저장되는 경우가 있다
            out[name] = list(dict.fromkeys(str(v) for v in tags[key]))
    common = {v.lower() for v in ASF_MAP.values()}
    for key in dict.fromkeys(k for k, _ in tags):
        if key.lower() in common or key == "WM/Picture":
            continue
        name = ASF_EXT_REV.get(key.lower(), key.upper())
        vals = [v for v in tags[key] if not isinstance(v.value, (bytes, bytearray))]
        if vals and name not in out:
            out[name] = list(dict.fromkeys(str(v) for v in vals))
    pics = []
    for v in tags.get("WM/Picture", []):
        try:
            pics.append(_asf_parse_pic(bytes(v.value)))
        except Exception:
            pass
    return out, pics


def _asf_write(tags, changes, pictures):
    for name, vals in changes.items():
        key = ASF_MAP.get(name) or ASF_EXT.get(name)
        if key is None:
            key = next((k for k, _ in tags if k.upper() == name), name)
        if key in tags:
            del tags[key]
        if vals:
            tags[key] = vals
    if pictures is not None:
        if "WM/Picture" in tags:
            del tags["WM/Picture"]
        if pictures:
            tags["WM/Picture"] = [ASFByteArrayAttribute(_asf_pack_pic(p)) for p in pictures]


# ---------------------------------------------------------------- AudioFile

def _has_id3v1(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(-128, os.SEEK_END)
            return f.read(3) == b"TAG"
    except OSError:
        return False


def _kind(mf) -> str:
    if isinstance(mf, ID3_CLASSES):
        return "id3"
    if isinstance(mf, MP4):
        return "mp4"
    if isinstance(mf, FLAC):
        return "flac"
    if isinstance(mf, OggFileType):
        return "ogg"
    if isinstance(mf, APEv2File):
        return "ape"
    if isinstance(mf, ASF):
        return "asf"
    return "unknown"


class UnsupportedFile(Exception):
    pass


def _info_length(mf) -> float | None:
    return None if mf is None else float(getattr(mf.info, "length", 0) or 0)


@dataclass(eq=False)
class AudioFile:
    path: str
    tags: dict = field(default_factory=dict)       # name -> list[str]
    pictures: list = field(default_factory=list)   # list[Picture]
    length: float = 0.0
    bitrate: int = 0
    sample_rate: int = 0
    channels: int = 0
    tag_type: str = ""
    size: int = 0
    mtime: float = 0.0
    dev: int = 0
    ino: int = 0

    # ---- 표시용
    @property
    def filename(self) -> str:
        return nfc(os.path.basename(self.path))

    @property
    def stem(self) -> str:
        return os.path.splitext(self.filename)[0]

    @property
    def folder(self) -> str:
        return nfc(os.path.dirname(self.path))

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    def get(self, name: str) -> str:
        return SEP.join(self.tags.get(name, []))

    def primary_index(self) -> int:
        """패널에 보여 줄 대표 커버 (앞 표지 우선)."""
        for i, p in enumerate(self.pictures):
            if p.type == 3:
                return i
        return 0 if self.pictures else -1

    @property
    def cover(self) -> Picture | None:
        i = self.primary_index()
        return self.pictures[i] if i >= 0 else None

    # ---- 읽기
    @classmethod
    def load(cls, path: str, opts: Options) -> "AudioFile":
        try:
            mf = mutagen.File(path)
        except Exception as e:
            raise UnsupportedFile(str(e)) from e
        if mf is None:
            raise UnsupportedFile("지원하지 않는 형식")
        kind = _kind(mf)
        if kind == "unknown":
            raise UnsupportedFile(type(mf).__name__)

        af = cls(path=path)
        st = os.stat(path)
        af.size, af.mtime = st.st_size, st.st_mtime
        af.dev, af.ino = st.st_dev, st.st_ino
        info = mf.info
        af.length = float(getattr(info, "length", 0) or 0)
        af.bitrate = int(getattr(info, "bitrate", 0) or 0)
        af.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        af.channels = int(getattr(info, "channels", 0) or 0)
        if not af.bitrate and af.length:
            af.bitrate = int(af.size * 8 / af.length)

        tags = mf.tags
        if kind == "id3":
            if tags is not None:
                af.tags, af.pictures = _id3_read(tags, opts)
                major = tags.version[1] if tags.version[0] == 2 else 0
                parts = [f"ID3v2.{major}"] if major else []
                if _has_id3v1(path) or tags.version[0] == 1:
                    parts.append("ID3v1")
                af.tag_type = ", ".join(parts)
        elif kind == "mp4":
            if tags is not None:
                af.tags, af.pictures = _mp4_read(tags)
                af.tag_type = "MP4"
        elif kind == "flac":
            if tags is not None:
                af.tags = _vorbis_read(tags)
                af.tag_type = "FLAC"
            af.pictures = [_picture(p.data, p.mime or guess_mime(p.data), int(p.type), p.desc)
                           for p in mf.pictures]
        elif kind == "ogg":
            if tags is not None:
                af.tags = _vorbis_read(tags)
                af.pictures = _ogg_read_pics(tags)
                af.tag_type = "Vorbis Comment"
        elif kind == "ape":
            if tags is not None:
                af.tags, af.pictures = _ape_read(tags)
                af.tag_type = "APEv2"
        elif kind == "asf":
            if tags is not None:
                af.tags, af.pictures = _asf_read(tags)
                af.tag_type = "ASF"
        # 빈 값 정리
        af.tags = {k: v for k, v in af.tags.items() if any(s.strip() for s in v)}
        return af

    # ---- 쓰기
    def _snapshot_hook(self, snapshot):
        """snapshot 목록이 주어지면, 바꾸기 직전 상태를 같은 파일 읽기로 함께 담는 함수를 돌려준다."""
        if snapshot is None:
            return lambda mf, path: None
        orig = self.path

        def hook(mf, path):
            st = os.stat(orig)  # 시각은 작업 사본이 아니라 원본의 것 (아직 바뀌기 전)
            snapshot.append(_snapshot_of(mf, path, (st.st_atime, st.st_mtime)))
        return hook

    def save(self, changes: dict, pictures: list | None, opts: Options, snapshot: list | None = None) -> "AudioFile":
        """changes: {필드: [값...]} (빈 리스트는 삭제). pictures: None이면 커버 변경 없음.
        snapshot 에 목록을 주면 바꾸기 전 태그(실행 취소용)를 담아 준다."""
        hook = self._snapshot_hook(snapshot)

        def write(path):
            mf = mutagen.File(path)
            hook(mf, path)
            before = _info_length(mf)
            kind = _kind(mf)
            if mf.tags is None:
                mf.add_tags()
            tags = mf.tags
            if kind == "id3":
                _id3_write(tags, changes, pictures, opts)
                v2 = 3 if opts.id3_v23 else 4
                if v2 == 3:
                    tags.update_to_v23()
                if isinstance(mf, (MP3, TrueAudio)):
                    tags.save(path, v1=2 if opts.write_id3v1 else 1, v2_version=v2)
                else:
                    mf.save(v2_version=v2)
            elif kind == "mp4":
                _mp4_write(tags, changes, pictures)
                mf.save()
            elif kind == "flac":
                _vorbis_write(tags, changes)
                if pictures is not None:
                    mf.clear_pictures()
                    for p in pictures:
                        mf.add_picture(_flac_pic(p))
                mf.save()
            elif kind == "ogg":
                _vorbis_write(tags, changes)
                if pictures is not None:
                    _ogg_write_pics(tags, pictures)
                mf.save()
            elif kind == "ape":
                _ape_write(tags, changes, pictures)
                mf.save()
            elif kind == "asf":
                _asf_write(tags, changes, pictures)
                mf.save()
            else:
                raise UnsupportedFile("쓰기를 지원하지 않는 형식")
            return before

        return self._saved(safe_modify(self.path, write, opts.safe_save, opts.keep_mtime,
                                       inspect=lambda p: AudioFile.load(p, opts)), opts)

    def _saved(self, af, opts: Options) -> "AudioFile":
        return _adopt(af, self.path, opts)

    def remove_tags(self, opts: Options, snapshot: list | None = None) -> "AudioFile":
        hook = self._snapshot_hook(snapshot)

        def write(path):
            mf = mutagen.File(path)
            hook(mf, path)
            before = _info_length(mf)
            kind = _kind(mf)
            if kind == "id3" and isinstance(mf, (MP3, TrueAudio)):
                from mutagen.id3 import delete as id3_delete
                id3_delete(path, delete_v1=True, delete_v2=True)
            elif kind == "flac":
                mf.delete()
                mf.clear_pictures()
                mf.save()
            elif mf.tags is not None:
                try:
                    mf.delete()
                except Exception:
                    mf.tags.clear()
                    mf.save()
            return before

        return self._saved(safe_modify(self.path, write, opts.safe_save, opts.keep_mtime,
                                       inspect=lambda p: AudioFile.load(p, opts)), opts)

    def path_for_stem(self, new_stem: str) -> str:
        """같은 폴더에서 확장자는 그대로 두고 이름만 바꾼 경로."""
        new_stem = nfc(new_stem.strip()).replace("/", "_")
        if not new_stem:
            raise ValueError("파일명이 비어 있습니다.")
        return os.path.join(os.path.dirname(self.path), new_stem + os.path.splitext(self.path)[1])

    def move_to(self, new_path: str) -> str:
        """파일 이름 바꾸기/옮기기. 필요하면 폴더를 만든다."""
        move_file(self.path, new_path)
        self.path = new_path
        return new_path


def move_file(src: str, dst: str):
    if dst == src:
        return
    # 대소문자만 바뀌는 경우는 같은 파일이므로 허용
    if os.path.exists(dst) and not os.path.samefile(dst, src):
        raise FileExistsError(f"이미 같은 이름의 파일이 있습니다: {os.path.basename(dst)}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != 18:  # EXDEV: 다른 디스크로 옮기는 경우
            raise
        shutil.move(src, dst)


def scan(paths, recursive=True):
    """폴더/파일 경로 목록에서 지원하는 오디오 파일을 찾아 정렬된 목록으로 돌려준다."""
    found = []
    for p in paths:
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in AUDIO_EXTS:
                found.append(p)
        elif os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                    for f in sorted(files):
                        if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                            found.append(os.path.join(root, f))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if (not f.startswith(".") and os.path.isfile(fp)
                            and os.path.splitext(f)[1].lower() in AUDIO_EXTS):
                        found.append(fp)
    return found


# ---------------------------------------------------------------- 실행 취소용 스냅샷

@dataclass(eq=False)
class Snapshot:
    """파일의 태그 전체(지원하는 필드 외의 것까지)와 수정 시각을 그대로 보관한다."""
    kind: str
    data: object
    had_v1: bool
    atime: float
    mtime: float

    def size_estimate(self, seen: set | None = None) -> int:
        """대략의 메모리 크기. seen 을 주면 여러 스냅샷이 함께 쓰는 커버는 한 번만 센다."""
        try:
            if self.kind == "id3" and self.data is not None:
                frames = list(self.data.values())
                return 64 * len(frames) + _blob_size([getattr(f, "data", b"") for f in frames], seen)
            if self.kind == "flac" and self.data is not None:
                return 1024 + _blob_size([p.data for p in self.data[1]], seen)
        except Exception:
            pass
        return 4096


def _blob_size(blobs, seen):
    total = 0
    for b in blobs:
        if not isinstance(b, (bytes, bytearray)):
            continue
        if seen is not None:
            if id(b) in seen:
                continue
            seen.add(id(b))
        total += len(b)
    return total


def take_snapshot(path: str) -> Snapshot:
    st = os.stat(path)
    return _snapshot_of(mutagen.File(path), path, (st.st_atime, st.st_mtime))


def _snapshot_of(mf, path: str, times) -> Snapshot:
    """이미 읽어 둔 mutagen 객체로 스냅샷을 만든다. times: 원본의 (atime, mtime)."""
    kind = _kind(mf)
    tags = mf.tags
    had_v1 = False
    if kind == "id3":
        data = copy.deepcopy(tags)
        had_v1 = _has_id3v1(path)
        if data is not None:
            for frame in data.getall("APIC"):  # 같은 커버는 목록의 파일과 메모리를 함께 쓴다
                frame.data = _share(frame.data)
    elif kind == "flac":
        data = (None if tags is None else copy.deepcopy(list(tags)),
                copy.deepcopy(list(mf.pictures)))
        for p in data[1]:
            p.data = _share(p.data)
    elif kind in ("ogg", "asf"):
        data = None if tags is None else copy.deepcopy(list(tags))
    elif kind in ("mp4", "ape"):
        data = None if tags is None else {k: copy.deepcopy(v) for k, v in tags.items()}
    else:
        raise UnsupportedFile("스냅샷을 지원하지 않는 형식")
    return Snapshot(kind, data, had_v1, *times)


def _adopt(af, path: str, opts: Options) -> AudioFile:
    """저장 검증 때 작업 사본에서 읽은 정보를 원본의 경로·파일 정보로 고친다 (파일을 다시 읽지 않는다)."""
    if af is None:
        return AudioFile.load(path, opts)
    st = os.stat(path)
    af.path = path
    af.size, af.mtime, af.dev, af.ino = st.st_size, st.st_mtime, st.st_dev, st.st_ino
    return af


def restore_snapshot(path: str, snap: Snapshot, safe: bool = True, opts: Options | None = None):
    """스냅샷의 태그로 되돌린다. opts 를 주면 되돌린 파일을 읽은 AudioFile 을 돌려준다."""
    af = safe_modify(path, lambda p: _restore_into(p, snap), safe,
                     inspect=(lambda p: AudioFile.load(p, opts)) if opts else None)
    os.utime(path, (snap.atime, snap.mtime))
    return _adopt(af, path, opts) if opts else None


def _restore_into(path: str, snap: Snapshot):
    mf = mutagen.File(path)
    before = _info_length(mf)
    kind = snap.kind
    if kind == "id3":
        tags = copy.deepcopy(snap.data)
        is_mp3 = isinstance(mf, (MP3, TrueAudio))
        if tags is None:
            if is_mp3:
                from mutagen.id3 import delete as id3_delete
                id3_delete(path, delete_v1=True, delete_v2=True)
            elif mf.tags is not None:
                mf.delete()
        else:
            v2 = tags.version[1] if tags.version[0] == 2 and tags.version[1] in (3, 4) else 4
            if v2 == 3:
                tags.update_to_v23()
            if is_mp3:
                tags.save(path, v1=2 if snap.had_v1 else 0, v2_version=v2)
            else:
                mf.tags = tags
                mf.save(v2_version=v2)
    elif kind == "flac":
        items, pics = snap.data
        if mf.tags is None:
            mf.add_tags()
        del mf.tags[:]
        if items:
            mf.tags.extend(copy.deepcopy(items))
        mf.clear_pictures()
        for p in pics:
            mf.add_picture(copy.deepcopy(p))
        mf.save()
    elif kind in ("ogg", "asf"):
        if mf.tags is None:
            mf.add_tags()
        del mf.tags[:]
        if snap.data:
            mf.tags.extend(copy.deepcopy(snap.data))
        mf.save()
    elif kind in ("mp4", "ape"):
        if snap.data is None:
            if mf.tags is not None:
                mf.delete()
        else:
            if mf.tags is None:
                mf.add_tags()
            mf.tags.clear()
            for k, v in snap.data.items():
                mf.tags[k] = copy.deepcopy(v)
            mf.save()
    return before
