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

from .safewrite import CHUNK, IS_WIN, rewind, safe_modify
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

import mutagen._util as _mutagen_util
import mutagen.id3._specs as _id3_specs
import mutagen.id3._tags as _id3_tags
import mutagen.mp4 as _mutagen_mp4

_mp4_sort_key = _mutagen_mp4._item_sort_key


def _item_sort_key(key, value):
    # mutagen 은 m4a 를 저장할 때 항목 순서를 정하려고 값마다 repr() 을 만든다. 커버는 수백 KB 라 곡마다 수 ms 가
    # 든다. 'covr' 는 순서 칸이 혼자뿐이라 값을 비교할 일이 없으므로 값 없이 같은 순서를 돌려준다.
    if key == "covr":
        return _mp4_sort_key(key, ())
    return _mp4_sort_key(key, value)


_mutagen_mp4._item_sort_key = _item_sort_key


class _Tail:
    """bytes 의 뒷부분을 복사하지 않고 가리킨다 (read_frames 가 쓰는 만큼만: 앞뒤를 정한 조각은 bytes, 뒤가 열린 조각은 또 _Tail)."""
    __slots__ = ("data", "at")

    def __init__(self, data, at=0):
        self.data, self.at = data, at

    def __len__(self):
        return max(0, len(self.data) - self.at)  # 프레임 크기가 태그를 넘어가면 (잘린 파일) 뒤가 없다

    def __getitem__(self, s):
        if s.stop is None:
            return _Tail(self.data, self.at + (s.start or 0))
        return self.data[self.at + (s.start or 0):self.at + s.stop]


_read_frames = _id3_tags.read_frames


def _read_frames_fast(id3, data, frames):
    # mutagen 은 ID3 프레임을 하나 읽을 때마다 남은 태그 전체를 복사한다 (data = data[10 + size:]). 커버가 뒤에 있으면
    # 곡마다 커버 크기 × 프레임 수만큼 복사하는 셈이다 (MP3 를 읽는 시간의 3분의 1). 복사하지 않는 _Tail 을 넘긴다.
    if id3.f_unsynch:
        return _read_frames(id3, data, frames)
    result, unknown, rest = _read_frames(id3, _Tail(data), frames)
    return result, unknown, rest.data[rest.at:]  # 남은 부분은 bytes 로 (챕터 프레임 안의 프레임을 읽을 때 쓰인다)


_id3_tags.read_frames = _read_frames_fast

_decode_terminated = _id3_specs.decode_terminated
_UTF16_BOM = {b"\xff\xfe": "utf-16-le", b"\xfe\xff": "utf-16-be"}


def _decode_terminated_fast(data, encoding, strict=True):
    # mutagen 은 UTF-16 글을 한 바이트씩 파이썬으로 풀어 첫 NUL 을 찾는다 (MP3 태그를 읽는 시간의 5분의 1, 가사가 길면 곡당 수십 ms).
    # 짝수 위치의 NUL 을 찾아 한 번에 푼다. 결과와 오류는 같다 (test_fast_save 가 무작위 입력으로 비교한다).
    if encoding == "utf16":
        codec, start = _UTF16_BOM.get(data[:2]), 2
        if codec is None:  # BOM 이 없는 경우 등은 mutagen 그대로
            return _decode_terminated(data, encoding, strict)
    elif encoding == "utf_16_be":
        codec, start = "utf-16-be", 0
    else:
        return _decode_terminated(data, encoding, strict)
    end = data.find(b"\0\0", start)
    while end != -1 and (end - start) % 2:
        end = data.find(b"\0\0", end + 1)
    if end == -1:
        text = data[start:].decode(codec)  # 풀 수 없으면 NUL 이 없다는 오류보다 먼저 알린다 (mutagen 과 같다)
        if strict:
            raise ValueError("not null terminated")
        return text, b""
    return data[start:end].decode(codec), data[end + 2:]


_id3_specs.decode_terminated = _decode_terminated_fast

if IS_WIN:  # 오디오를 옮길 때도 CHUNK 크기로 (safewrite.CHUNK 참고)
    for _fn in (_mutagen_util.resize_file, _mutagen_util.move_bytes, _mutagen_util.insert_bytes, _mutagen_util.delete_bytes):
        if _fn.__defaults__ == (_mutagen_util._DEFAULT_BUFFER_SIZE,):
            _fn.__defaults__ = (CHUNK,)

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
# 정렬용 이름 — Music 이 이 값으로 정렬한다 (예: 米津玄師 → Yonezu Kenshi)
SORT_FIELDS = ["TITLESORT", "ARTISTSORT", "ALBUMSORT", "ALBUMARTISTSORT"]
FIELD_LABELS.update({"TITLESORT": "정렬용 제목", "ARTISTSORT": "정렬용 아티스트", "ALBUMSORT": "정렬용 앨범",
                     "ALBUMARTISTSORT": "정렬용 앨범 아티스트", "COMPOSERSORT": "정렬용 작곡가"})

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


_file_options = None
_BY_EXT = {".mp3": MP3, ".flac": FLAC, ".m4a": MP4, ".m4b": MP4, ".m4r": MP4, ".mp4": MP4}


def _mf(target):
    """mutagen.File 과 같다. 형식 목록을 한 번만 만들어 넘긴다 — 넘기지 않으면 부를 때마다 import 문 20여 개를 실행하는데,
    PySide6 가 import 를 가로채므로 곡마다 0.1 ms 가까이 든다. 확장자와 내용이 맞는 흔한 형식은 형식마다 점수 매기는 일도 건너뛴다
    (mutagen.File 도 같은 형식을 고른다)."""
    if not isinstance(target, str):
        kind = _BY_EXT.get(os.path.splitext(target.name)[1].lower())
        if kind is not None:
            head = target.read(8)
            target.seek(0)
            if (head[:4] == b"fLaC" if kind is FLAC
                    else head[4:8] == b"ftyp" if kind is MP4
                    else head[:3] == b"ID3" or head[:2] in (b"\xff\xf2", b"\xff\xf3", b"\xff\xfa", b"\xff\xfb")):
                return kind(target)
    return mutagen.File(target, options=_file_formats())


def _file_formats() -> list:
    global _file_options
    if _file_options is None:
        from mutagen.aac import AAC
        from mutagen.ac3 import AC3
        from mutagen.dsdiff import DSDIFF
        from mutagen.id3 import ID3FileType
        from mutagen.monkeysaudio import MonkeysAudio
        from mutagen.musepack import Musepack
        from mutagen.oggflac import OggFLAC
        from mutagen.oggopus import OggOpus
        from mutagen.oggspeex import OggSpeex
        from mutagen.oggtheora import OggTheora
        from mutagen.oggvorbis import OggVorbis
        from mutagen.optimfrog import OptimFROG
        from mutagen.smf import SMF
        from mutagen.tak import TAK
        from mutagen.wavpack import WavPack
        _file_options = [MP3, TrueAudio, OggTheora, OggSpeex, OggVorbis, OggFLAC, FLAC, AIFF, APEv2File, MP4,
                         ID3FileType, WavPack, Musepack, MonkeysAudio, OptimFROG, ASF, OggOpus, AAC, AC3, SMF, TAK,
                         DSF, DSDIFF, WAVE]
    return _file_options


# 태그가 남는 자리(패딩)보다 커지면 오디오를 통째로 옮겨 곡 전체를 다시 써야 한다 (곡당 수십 ms). 그래서 그때 자리를 넉넉히 만들어
# 다음 저장부터는 옮기지 않게 하고, 남는 자리는 줄이지 않는다 (줄이면 그때도 오디오를 옮긴다).
def _slack(size: int) -> int:
    return max(32 * 1024, min(256 * 1024, size // 100))


def _pad_keep(info) -> int:
    """태그만 바꿀 때·되돌릴 때."""
    return info.padding if info.padding >= 0 else _slack(info.size)


def _pad_cover(info) -> int:
    """커버를 바꿀 때: 커버를 줄이면 파일도 줄어들게, 많이 남으면 줄인다."""
    slack = _slack(info.size)
    return slack if info.padding < 0 or info.padding > 2 * slack else info.padding


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
                data = other.data
                break
        pic = Picture(data, mime, ptype, desc)
        # 함께 쓰는 것도 모두 적어 둔다 — 처음 것만 적으면 그것이 사라질 때 같은 그림을 다시 못 찾는다
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
ID3_V24_ONLY = {"TMOO", "TDRL", "TSST", "TPRO", "TDEN", "TDTG"}
# 정렬용 프레임: 표준으로는 v2.4 것이지만 iTunes·Music 이 v2.3 에도 쓰고 읽는다 → v2.3 에서도 그대로 둔다
ID3_ITUNES_SORT = ("TSOA", "TSOP", "TSOT", "TSO2", "TSOC")


def _to_v23(tags):
    """v2.3 으로 바꾸되 정렬용 프레임은 남긴다 (mutagen 은 지운다)."""
    keep = [tags.pop(k) for k in ID3_ITUNES_SORT if k in tags]
    tags.update_to_v23()
    for fr in keep:
        tags.add(fr)
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
    "MOVEMENT": "©mvi", "MOVEMENTTOTAL": "©mvc", "SHOWMOVEMENT": "shwm",
}
MP4_INTS = {"tmpo", "©mvi", "©mvc", "shwm"}  # 숫자로 저장하는 칸
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
            if key in MP4_INTS:
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

def _has_id3v1(path) -> bool:
    """path: 경로 또는 메모리 작업 사본."""
    try:
        if not isinstance(path, str):
            path.seek(-128, os.SEEK_END)
            return path.read(3) == b"TAG"
        with open(path, "rb") as f:
            f.seek(-128, os.SEEK_END)
            return f.read(3) == b"TAG"
    except (OSError, ValueError):
        return False


LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "APE", "WAVPACK", "TTA", "DSD", "WMA LOSSLESS"}


def _codec_name(mf, kind) -> str:
    """mutagen 객체 → 사람이 읽는 코덱 이름 (확장자가 아니라 실제 내용)."""
    info = mf.info
    if kind == "mp4":
        c = (getattr(info, "codec", "") or "").lower()
        if c.startswith("alac"):
            return "ALAC"
        if c.startswith("mp4a") or c.startswith("aac"):
            return "AAC"
        return c.upper() or "MP4"
    if isinstance(mf, MP3):
        return "MP3"
    if isinstance(mf, WAVE):
        return "WAV"
    if isinstance(mf, AIFF):
        return "AIFF"
    if isinstance(mf, DSF):
        return "DSD"
    if isinstance(mf, TrueAudio):
        return "TTA"
    if kind == "flac":
        return "FLAC"
    name = type(mf).__name__.lower()
    for key, label in (("opus", "Opus"), ("vorbis", "Vorbis"), ("oggflac", "FLAC"), ("speex", "Speex"),
                       ("monkey", "APE"), ("wavpack", "WAVPACK"), ("musepack", "MPC"), ("optimfrog", "OFR")):
        if key in name:
            return label
    if kind == "asf":
        codec = (getattr(info, "codec_name", "") or "")
        return "WMA LOSSLESS" if "lossless" in codec.lower() else "WMA"
    return os.path.splitext(mf.filename or "")[1].lstrip(".").upper()


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
    codec: str = ""        # 실제 코덱 (AAC, ALAC, FLAC, MP3 …)
    bits: int = 0          # 비트 깊이 (무손실 형식만)
    tag_type: str = ""
    size: int = 0
    mtime: float = 0.0
    btime: float = 0.0     # 파일이 생성된 시각
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

    @property
    def lossless(self) -> bool:
        return (self.codec or self.ext.lstrip(".").upper()) in LOSSLESS

    def quality_key(self):
        """음질 비교용 (클수록 좋음): 무손실 → 비트레이트 → 샘플레이트 → 비트 깊이."""
        return (self.lossless, self.bitrate if not self.lossless else 0, self.sample_rate, self.bits, self.bitrate)

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
        if not isinstance(path, str):  # 메모리 작업 사본 (저장 검증) — 경로·파일 정보는 _adopt 가 원본 것으로 채운다
            af = cls(path=path.name)
            af.size = path.length()
            return af._read(rewind(path), opts)
        try:
            f = open(path, "rb")
        except OSError as e:
            raise UnsupportedFile(str(e)) from e
        with f:  # 곡마다 한 번만 연다 (파일 정보도 ID3v1 확인도 이 파일로)
            st = os.fstat(f.fileno())
            af = cls(path=path)
            af.size, af.mtime, af.btime = st.st_size, st.st_mtime, _birthtime(st)
            af.dev, af.ino = st.st_dev, st.st_ino
            return af._read(f, opts)

    def _read(self, target, opts: Options) -> "AudioFile":
        af = self
        try:
            mf = _mf(target)
        except Exception as e:
            raise UnsupportedFile(str(e)) from e
        if mf is None:
            raise UnsupportedFile("지원하지 않는 형식")
        kind = _kind(mf)
        if kind == "unknown":
            raise UnsupportedFile(type(mf).__name__)
        mf.filename = mf.filename or af.path
        info = mf.info
        af.length = float(getattr(info, "length", 0) or 0)
        af.bitrate = int(getattr(info, "bitrate", 0) or 0)
        af.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
        af.channels = int(getattr(info, "channels", 0) or 0)
        if not af.bitrate and af.length:
            af.bitrate = int(af.size * 8 / af.length)
        af.codec = _codec_name(mf, kind)
        if af.codec in LOSSLESS:
            af.bits = int(getattr(info, "bits_per_sample", 0) or 0)

        tags = mf.tags
        if kind == "id3":
            if tags is not None:
                af.tags, af.pictures = _id3_read(tags, opts)
                major = tags.version[1] if tags.version[0] == 2 else 0
                parts = [f"ID3v2.{major}"] if major else []
                if _has_id3v1(target) or tags.version[0] == 1:
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

    def save(self, changes: dict, pictures: list | None, opts: Options, snapshot: list | None = None,
             before_move=None) -> "AudioFile":
        """changes: {필드: [값...]} (빈 리스트는 삭제). pictures: None이면 커버 변경 없음.
        snapshot 에 목록을 주면 바꾸기 전 태그(실행 취소용)를 담아 준다. before_move: safe_modify 참고."""
        hook = self._snapshot_hook(snapshot)
        pad = _pad_keep if pictures is None else _pad_cover

        def write(path):
            mf = _mf(rewind(path))
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
                    _to_v23(tags)
                if isinstance(mf, (MP3, TrueAudio)):
                    tags.save(rewind(path), v1=2 if opts.write_id3v1 else 1, v2_version=v2, padding=pad)
                else:
                    mf.save(rewind(path), v2_version=v2)
            elif kind == "mp4":
                _mp4_write(tags, changes, pictures)
                mf.save(rewind(path), padding=pad)
            elif kind == "flac":
                _vorbis_write(tags, changes)
                if pictures is not None:
                    mf.clear_pictures()
                    for p in pictures:
                        mf.add_picture(_flac_pic(p))
                mf.save(rewind(path), padding=pad)
            elif kind == "ogg":
                _vorbis_write(tags, changes)
                if pictures is not None:
                    _ogg_write_pics(tags, pictures)
                mf.save(rewind(path))
            elif kind == "ape":
                _ape_write(tags, changes, pictures)
                mf.save(rewind(path))
            elif kind == "asf":
                _asf_write(tags, changes, pictures)
                mf.save(rewind(path))
            else:
                raise UnsupportedFile("쓰기를 지원하지 않는 형식")
            return before

        return self._saved(safe_modify(self.path, write, opts.safe_save, opts.keep_mtime,
                                       inspect=lambda p: AudioFile.load(p, opts), before_move=before_move), opts)

    def _saved(self, af, opts: Options) -> "AudioFile":
        return _adopt(af, self.path, opts)

    def remove_tags(self, opts: Options, snapshot: list | None = None, before_move=None) -> "AudioFile":
        hook = self._snapshot_hook(snapshot)

        def write(path):
            mf = _mf(rewind(path))
            hook(mf, path)
            before = _info_length(mf)
            kind = _kind(mf)
            if kind == "id3" and isinstance(mf, (MP3, TrueAudio)):
                from mutagen.id3 import delete as id3_delete
                id3_delete(rewind(path), delete_v1=True, delete_v2=True)
            elif kind == "flac":
                mf.delete(rewind(path))
                mf.clear_pictures()
                mf.save(rewind(path))
            elif mf.tags is not None:
                try:
                    mf.delete(rewind(path))
                except Exception:
                    mf.tags.clear()
                    mf.save(rewind(path))
            return before

        return self._saved(safe_modify(self.path, write, opts.safe_save, opts.keep_mtime,
                                       inspect=lambda p: AudioFile.load(p, opts), before_move=before_move), opts)

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
            if self.kind == "mp4" and self.data is not None:
                return 64 * len(self.data) + _blob_size([c.data for c in self.data.get("covr", ())], seen)
            if self.kind == "ape" and self.data is not None:
                return 64 * len(self.data) + _blob_size([getattr(v, "value", b"") for v in self.data.values()], seen)
            if self.kind in ("ogg", "asf") and self.data is not None:  # 커버가 태그 값 안에 (base64·바이트)
                return 64 * len(self.data) + sum(_value_size(v) for _, v in self.data)
        except Exception:
            pass
        return 4096


@dataclass(frozen=True)
class _SnapCover:
    """스냅샷 속 MP4 커버. MP4Cover 는 bytes 라 다른 bytes 와 메모리를 함께 쓸 수 없어 데이터와 형식만 둔다."""
    data: bytes
    imageformat: int


def _value_size(v) -> int:
    v = getattr(v, "value", v)
    return len(v) if isinstance(v, (str, bytes, bytearray)) else 16


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
    return _snapshot_of(_mf(rewind(path)), path, (st.st_atime, st.st_mtime))


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
    elif kind == "mp4":  # 커버는 목록의 파일과 메모리를 함께 쓴다 (곡마다 따로 복사하면 수백 MB)
        data = None if tags is None else {
            k: [_SnapCover(_share(bytes(c)), c.imageformat) for c in v] if k == "covr" else copy.deepcopy(v)
            for k, v in tags.items()}
    elif kind == "ape":
        data = None if tags is None else {k: copy.deepcopy(v) for k, v in tags.items()}
    else:
        raise UnsupportedFile("스냅샷을 지원하지 않는 형식")
    return Snapshot(kind, data, had_v1, *times)


def _birthtime(st) -> float:
    """파일이 생성된 시각 (Windows 의 예전 Python 은 st_birthtime 이 없고 st_ctime 이 생성 시각)."""
    return getattr(st, "st_birthtime", st.st_ctime)


def _adopt(af, path: str, opts: Options) -> AudioFile:
    """저장 검증 때 작업 사본에서 읽은 정보를 원본의 경로·파일 정보로 고친다 (파일을 다시 읽지 않는다)."""
    if af is None:
        return AudioFile.load(path, opts)
    st = os.stat(path)
    af.path = path
    af.size, af.mtime, af.btime, af.dev, af.ino = st.st_size, st.st_mtime, _birthtime(st), st.st_dev, st.st_ino
    return af


def restore_snapshot(path: str, snap: Snapshot, safe: bool = True, opts: Options | None = None,
                     capture: list | None = None):
    """스냅샷의 태그로 되돌린다. opts 를 주면 되돌린 파일을 읽은 AudioFile 을 돌려준다.
    capture 에 목록을 주면 되돌리기 직전 상태의 스냅샷을 (같은 파일 읽기로) 담아 준다 — 다시 실행용."""
    def write(p):
        return _restore_into(p, snap, path, capture)

    af = safe_modify(path, write, safe, inspect=(lambda p: AudioFile.load(p, opts)) if opts else None)
    os.utime(path, (snap.atime, snap.mtime))
    return _adopt(af, path, opts) if opts else None


def _restore_into(path: str, snap: Snapshot, orig: str, capture: list | None = None):
    mf = _mf(rewind(path))
    if capture is not None:
        st = os.stat(orig)  # 시각은 작업 사본이 아니라 원본의 것 (아직 바뀌기 전)
        capture.append(_snapshot_of(mf, path, (st.st_atime, st.st_mtime)))
    before = _info_length(mf)
    kind = snap.kind
    if kind == "id3":
        tags = copy.deepcopy(snap.data)
        is_mp3 = isinstance(mf, (MP3, TrueAudio))
        if tags is None:
            if is_mp3:
                from mutagen.id3 import delete as id3_delete
                id3_delete(rewind(path), delete_v1=True, delete_v2=True)
            elif mf.tags is not None:
                mf.delete(rewind(path))
        else:
            v2 = tags.version[1] if tags.version[0] == 2 and tags.version[1] in (3, 4) else 4
            if v2 == 3:
                _to_v23(tags)
            if is_mp3:
                tags.save(rewind(path), v1=2 if snap.had_v1 else 0, v2_version=v2, padding=_pad_keep)
            else:
                mf.tags = tags
                mf.save(rewind(path), v2_version=v2)
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
        mf.save(rewind(path), padding=_pad_keep)
    elif kind in ("ogg", "asf"):
        if mf.tags is None:
            mf.add_tags()
        del mf.tags[:]
        if snap.data:
            mf.tags.extend(copy.deepcopy(snap.data))
        mf.save(rewind(path))
    elif kind in ("mp4", "ape"):
        if snap.data is None:
            if mf.tags is not None:
                mf.delete(rewind(path))
        else:
            if mf.tags is None:
                mf.add_tags()
            mf.tags.clear()
            for k, v in snap.data.items():
                mf.tags[k] = ([MP4Cover(c.data, c.imageformat) for c in v] if kind == "mp4" and k == "covr"
                              else copy.deepcopy(v))
            if kind == "mp4":
                mf.save(rewind(path), padding=_pad_keep)
            else:
                mf.save(rewind(path))
    return before
