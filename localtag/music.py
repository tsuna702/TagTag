"""Apple Music 연동 (앨범태그 music_refresh.py · music_albums.py · music_drop 이식).

- 바꾼 곡만 Music에서 새로 읽게 하고(refresh), 옮겨진 파일 경로를 persistent ID로 다시 찾는다
- Music에서 선택한 곡 / 끌어다 놓은 곡·앨범의 실제 파일 경로를 얻는다
AppleScript(osascript)를 쓰므로 처음에 macOS가 'Music 제어' 권한을 묻는다.
"""
from __future__ import annotations

import base64
import ctypes
import os
import plistlib
import re
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import unquote, urlsplit


class MusicError(Exception):
    pass


PERMISSION_HELP = ("Music 제어 권한이 필요합니다. 시스템 설정 → 개인정보 보호 및 보안 → 자동화에서 "
                   "LocalTag(또는 터미널) 아래의 'Music'을 켜 주세요.")

REFRESH_SCRIPT = '''
use framework "Foundation"
use scripting additions
on recordPath(pathMappings, oldPath, stableID, newPath, mappingFile)
    pathMappings's setObject:({stableID as text, newPath as text}) forKey:(oldPath as text)
    set saved to pathMappings's writeToFile:mappingFile atomically:true
    if not saved then error "경로 연결 정보를 저장하지 못했습니다."
end recordPath
on run arguments
    set mappingFile to item 1 of arguments
    set wantedPaths to rest of arguments
    set pathMappings to current application's NSMutableDictionary's dictionary()
    tell application "Music"
        if not running then error "Apple Music을 먼저 열어 주세요."
        set selectedCount to count of wantedPaths
        set refreshedCount to 0
        set skippedCount to 0
        set failedCount to 0
        set completedCount to 0
        repeat with wantedPath in wantedPaths
            log "LOCALTAG_PROGRESS:" & completedCount
            try
                set wantedFile to (POSIX file (contents of wantedPath)) as alias
                with timeout of 30 seconds
                    set matches to get (every file track of library playlist 1 whose location is wantedFile)
                end timeout
                if (count of matches) is 0 then
                    set skippedCount to skippedCount + 1
                else
                    with timeout of 15 seconds
                        set stableID to get persistent ID of item 1 of matches
                        refresh item 1 of matches
                    end timeout
                    set refreshedCount to refreshedCount + 1
                    try
                        with timeout of 15 seconds
                            set stableTrack to get first file track of library playlist 1 whose persistent ID is stableID
                            set newPath to POSIX path of (get location of stableTrack)
                        end timeout
                        my recordPath(pathMappings, contents of wantedPath, stableID, newPath, mappingFile)
                    on error pathError number pathErrorNumber
                        log "LOCALTAG_PATH_WARNING:" & pathErrorNumber & ": " & pathError
                    end try
                end if
            on error errorText number errorNumber
                if errorNumber is -1743 then error errorText number errorNumber
                set failedCount to failedCount + 1
            end try
            set completedCount to completedCount + 1
            log "LOCALTAG_PROGRESS:" & completedCount
        end repeat
        return (selectedCount as text) & "," & (refreshedCount as text) & "," & (skippedCount as text) & "," & (failedCount as text)
    end tell
end run
'''

PREFLIGHT = '''
tell application "Music"
    with timeout of 120 seconds
        get name of library playlist 1
        if (count of file tracks of library playlist 1) > 0 then
            get location of first file track of library playlist 1
        end if
    end timeout
end tell
return "READY"
'''

SELECTION_SCRIPT = '''
set out to {}
tell application "Music"
    if not running then error "Apple Music을 먼저 열어 주세요."
    set picked to selection
    repeat with t in picked
        try
            if class of t is file track then
                set loc to location of t
                if loc is not missing value then set end of out to POSIX path of loc
            end if
        end try
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return out as text
'''

ALBUM_SCRIPT = '''
use framework "Foundation"
use scripting additions
on run arguments
    set wantedAlbum to item 1 of arguments
    set outputFile to item 2 of arguments
    set albumRecords to current application's NSMutableArray's array()
    tell application "Music"
        with timeout of 45 seconds
            set matches to get (every track of library playlist 1 whose album is wantedAlbum)
            repeat with matchedTrack in matches
                set info to get properties of matchedTrack
                set recordData to current application's NSMutableDictionary's dictionary()
                recordData's setObject:(name of info as text) forKey:"Name"
                recordData's setObject:(album of info as text) forKey:"Album"
                recordData's setObject:(album artist of info as text) forKey:"Album Artist"
                recordData's setObject:(artist of info as text) forKey:"Artist"
                recordData's setObject:(disc number of info as integer) forKey:"Disc Number"
                recordData's setObject:(track number of info as integer) forKey:"Track Number"
                recordData's setObject:(compilation of info as boolean) forKey:"Compilation"
                try
                    set localFile to location of info
                    if localFile is not missing value then
                        recordData's setObject:(POSIX path of localFile) forKey:"Location"
                    end if
                end try
                albumRecords's addObject:recordData
            end repeat
        end timeout
    end tell
    if not (albumRecords's writeToFile:outputFile atomically:true) then error "앨범 곡 목록을 읽지 못했습니다."
    return "READY"
end run
'''


def _osascript(script, *args, timeout=60):
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script, "--", *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MusicError("Music이 응답하지 않습니다. 권한 창이 떠 있으면 허용한 뒤 다시 시도해 주세요.") from e
    if r.returncode:
        if "-1743" in r.stderr:
            raise MusicError(PERMISSION_HELP)
        if "-600" in r.stderr or "먼저 열어" in r.stderr:
            raise MusicError("Apple Music을 먼저 열어 주세요.")
        raise MusicError("Music 작업에 실패했습니다.\n" + r.stderr.strip())
    return r.stdout


def ensure_access(launch=False):
    if launch:
        subprocess.run(["/usr/bin/open", "-g", "-b", "com.apple.Music"], capture_output=True, timeout=15)
    out = _osascript(PREFLIGHT, timeout=150)
    if out.strip() != "READY":
        raise MusicError("Music 접근 확인에 실패했습니다.")


def open_privacy_settings():
    subprocess.run(["/usr/bin/open",
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"], check=False)


def _run_batch(paths, progress, cancelled, relocated, warnings):
    with tempfile.TemporaryFile() as out, tempfile.NamedTemporaryFile() as errs, \
            tempfile.NamedTemporaryFile(suffix=".plist") as mapping:
        proc = subprocess.Popen(["/usr/bin/osascript", "-e", REFRESH_SCRIPT, "--", mapping.name, *paths],
                                stdout=out, stderr=errs)
        reader = open(errs.name, "rb")
        offset, pending, diag = 0, b"", []
        last = time.monotonic()
        try:
            while True:
                reader.seek(offset)
                data = reader.read()
                offset += len(data)
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    text = line.decode("utf-8", "replace")
                    m = re.search(r"LOCALTAG_PROGRESS:(\d+)", text)
                    if m:
                        progress(int(m[1]))
                        last = time.monotonic()
                    elif "LOCALTAG_PATH_WARNING:" in text:
                        warnings.append(text.split("LOCALTAG_PATH_WARNING:", 1)[1])
                    else:
                        diag.append(text)
                if proc.poll() is not None:
                    reader.seek(offset)
                    if reader.read():
                        continue
                    break
                if cancelled():
                    raise MusicError("취소했습니다. 일부 곡은 이미 새로 읽었을 수 있습니다.")
                if time.monotonic() - last > 180:
                    raise MusicError("Music의 응답 시간이 초과되었습니다. 일부 곡은 이미 새로 읽었을 수 있습니다.")
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.close()
        if proc.returncode:
            detail = "\n".join(diag) + pending.decode("utf-8", "replace")
            if "-1743" in detail:
                raise MusicError(PERMISSION_HELP)
            raise MusicError("Music 새로고침을 끝내지 못했습니다.\n" + detail)
        if os.path.getsize(mapping.name):
            with open(mapping.name, "rb") as f:
                for old, value in plistlib.load(f).items():
                    if old in paths and isinstance(value, list) and len(value) == 2 and value[1].startswith("/"):
                        relocated[old] = value[1]
        out.seek(0)
        try:
            counts = tuple(int(x) for x in out.read().decode().strip().split(","))
            if len(counts) != 4:
                raise ValueError
        except ValueError as e:
            raise MusicError("Music의 응답을 확인하지 못했습니다.") from e
        return counts


def refresh(paths, progress=lambda *_: None, cancelled=lambda: False):
    """바꾼 파일을 Music에서 다시 읽게 한다.
    반환: ((선택, 새로 읽음, 보관함에 없음, 실패), {옛 경로: 새 경로}, 경고 목록)"""
    paths = list(dict.fromkeys(os.path.realpath(p) for p in paths))
    if not paths:
        raise MusicError("Music에 반영할 곡을 선택해 주세요.")
    total = len(paths)
    progress(0, total, "Music 권한 확인 중 · 권한 창이 뜨면 허용해 주세요")
    ensure_access()
    result = [total, 0, 0, 0]
    relocated, warnings = {}, []
    for start in range(0, total, 50):  # 명령줄 길이 제한
        batch = paths[start:start + 50]

        def report(cur, start=start, batch=batch):
            name = os.path.basename(batch[min(cur, len(batch) - 1)])
            progress(start + cur, total, "Music에서 새로 읽는 중 · " + name)
        report(0)
        counts = _run_batch(batch, report, cancelled, relocated, warnings)
        for i in range(1, 4):
            result[i] += counts[i]
    return tuple(result), relocated, warnings


def selected_paths():
    """Music에서 지금 선택한 곡들의 파일 경로."""
    out = _osascript(SELECTION_SCRIPT, timeout=60)
    paths = [p for p in out.splitlines() if p.startswith("/")]
    if not paths:
        raise MusicError("Music에서 선택한 곡 중 이 Mac에 파일이 있는 곡이 없습니다. "
                         "보관함에서 곡을 선택한 뒤 다시 시도해 주세요.")
    return paths


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def album_paths(names, cancelled=lambda: False):
    """앨범 이름으로 Music 보관함의 곡 파일 경로를 찾는다 (앨범 격자에서 끌어온 경우)."""
    paths, errors = [], []
    for name in dict.fromkeys(names):
        if cancelled():
            break
        with tempfile.TemporaryDirectory(prefix="localtag-album-") as tmp:
            out = os.path.join(tmp, "tracks.plist")
            _osascript(ALBUM_SCRIPT, name, out, timeout=60)
            try:
                with open(out, "rb") as f:
                    found = plistlib.load(f)
            except (OSError, plistlib.InvalidFileException) as e:
                raise MusicError("Music이 돌려준 앨범 정보를 읽지 못했습니다.") from e
        found = [r for r in found if isinstance(r, dict) and _nfc(str(r.get("Album", ""))) == _nfc(name)]
        if not found:
            errors.append(f"{name}: Music 보관함에서 이 앨범을 찾지 못했습니다.")
            continue
        groups = {_nfc(str(r.get("Album Artist") or ("(컴필레이션)" if r.get("Compilation") else r.get("Artist", ""))))
                  for r in found}
        if len(groups) > 1:
            errors.append(f"{name}: 같은 이름의 앨범이 여러 아티스트에게 있습니다. 앨범 안의 곡을 선택해 끌어와 주세요.")
            continue
        found.sort(key=lambda r: (int(r.get("Disc Number") or 0), int(r.get("Track Number") or 0)))
        for r in found:
            loc = r.get("Location")
            if loc:
                paths.append(loc)
            else:
                errors.append(f"{r.get('Name', '곡')}: 이 Mac에 파일이 없는 곡입니다 (클라우드 곡).")
    return paths, errors


# ------------------------------------------------------------------ Music에서 끌어다 놓기
# Qt는 Music 전용 끌어다 놓기 형식(com.apple.music.metadata 등)을 모르므로,
# 놓는 순간 macOS의 드래그 보드를 직접 읽는다. 시스템 클립보드는 건드리지 않는다.

METADATA_TYPES = ["com.apple.music.metadata", "com.apple.itunes.metadata", "com.apple.tv.metadata"]
ALBUM_TYPES = ["com.apple.Music.Oidl", "com.apple.iTunes.Oidl"]
PROMISE_TYPE = "com.apple.pasteboard.promised-file-content-type"
PROMISE_NAME = "com.apple.pasteboard.promised-suggested-file-name"


class _ObjC:
    def __init__(self):
        self.lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self._msg = ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value

    def cls(self, name):
        return self.lib.objc_getClass(name.encode())

    def send(self, obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(self._msg)
        return fn(obj, self.lib.sel_registerName(sel.encode()), *args)

    def nsstr(self, s):
        return self.send(self.cls("NSString"), "stringWithUTF8String:", s.encode(), argtypes=[ctypes.c_char_p])

    def pystr(self, ns):
        if not ns:
            return None
        b = self.send(ns, "UTF8String", restype=ctypes.c_char_p)
        return b.decode("utf-8", "replace") if b else None

    def array(self, ns):
        if not ns:
            return []
        n = self.send(ns, "count", restype=ctypes.c_ulong)
        return [self.send(ns, "objectAtIndex:", i, argtypes=[ctypes.c_ulong]) for i in range(n)]

    def data(self, ns):
        if not ns:
            return b""
        n = self.send(ns, "length", restype=ctypes.c_ulong)
        p = self.send(ns, "bytes")
        return ctypes.string_at(p, n) if p and n else b""


_objc = None


def _bridge():
    global _objc
    if _objc is None:
        _objc = _ObjC()
    return _objc


def register_drop_types(widget):
    """창이 Music에서 끌어온 항목도 받도록 드래그 형식을 추가로 등록한다."""
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        o = _bridge()
        view = int(widget.winId())
        current = o.send(view, "registeredDraggedTypes")
        extra = o.send(o.cls("NSMutableArray"), "array")
        for t in METADATA_TYPES + ALBUM_TYPES + ["public.file-url", "NSFilenamesPboardType"]:
            o.send(extra, "addObject:", o.nsstr(t), argtypes=[ctypes.c_void_p])
        merged = o.send(current, "arrayByAddingObjectsFromArray:", extra, argtypes=[ctypes.c_void_p]) \
            if current else extra
        o.send(view, "registerForDraggedTypes:", merged, argtypes=[ctypes.c_void_p])
        return True
    except Exception:
        return False


def _drag_board():
    o = _bridge()
    return o, o.send(o.cls("NSPasteboard"), "pasteboardWithName:", o.nsstr("Apple CFPasteboard drag"),
                     argtypes=[ctypes.c_void_p])


def drag_has_music() -> bool:
    try:
        o, board = _drag_board()
        types = {o.pystr(t) for t in o.array(o.send(board, "types"))}
        return bool(types & set(METADATA_TYPES + ALBUM_TYPES))
    except Exception:
        return False


def _local_path(value):
    if not isinstance(value, str) or "\x00" in value:
        return None
    if value.startswith("/"):
        return value
    u = urlsplit(value)
    if u.scheme.lower() != "file" or u.netloc.lower() not in ("", "localhost"):
        return None
    path = unquote(u.path)
    return path if path.startswith("/") else None


def read_music_drag():
    """지금 놓인 Music 드래그에서 (곡 파일 경로, 앨범 이름) 을 읽는다."""
    o, board = _drag_board()
    paths, albums = [], []
    for item in o.array(o.send(board, "pasteboardItems")):
        types = {o.pystr(t) for t in o.array(o.send(item, "types"))}
        if types & set(ALBUM_TYPES):
            kind = o.pystr(o.send(item, "stringForType:", o.nsstr(PROMISE_TYPE), argtypes=[ctypes.c_void_p]))
            name = o.pystr(o.send(item, "stringForType:", o.nsstr(PROMISE_NAME), argtypes=[ctypes.c_void_p]))
            if kind == "public.folder" and name:
                albums.append(name)
        for t in METADATA_TYPES:
            if t in types:
                raw = o.data(o.send(item, "dataForType:", o.nsstr(t), argtypes=[ctypes.c_void_p]))
                paths.extend(_paths_from_metadata(raw))
    return list(dict.fromkeys(p for p in paths if p)), list(dict.fromkeys(albums))


def _paths_from_metadata(raw: bytes):
    try:
        doc = plistlib.loads(raw)
    except Exception:
        try:
            doc = plistlib.loads(base64.b64decode(raw))
        except Exception:
            return []
    tracks = doc.get("Tracks", {}) if isinstance(doc, dict) else {}
    tracks = list(tracks.values()) if isinstance(tracks, dict) else list(tracks)
    tracks = [t for t in tracks if isinstance(t, dict)]

    def num(t, k):
        try:
            return int(t.get(k, 0))
        except (TypeError, ValueError):
            return 0
    # 앨범 순서는 끌어온 순서대로, 앨범 안에서는 디스크·트랙 번호 순
    groups = {}
    for t in tracks:
        groups.setdefault((str(t.get("Album Artist", t.get("Artist", ""))), str(t.get("Album", ""))), []).append(t)
    out = []
    for g in groups.values():
        out += [_local_path(t.get("Location")) for t in sorted(g, key=lambda t: (num(t, "Disc Number"), num(t, "Track Number")))]
    return out
