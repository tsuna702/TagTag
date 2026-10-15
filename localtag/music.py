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
import sys
import tempfile
import time
import unicodedata
from urllib.parse import unquote, urlsplit


class MusicError(Exception):
    pass


PERMISSION_HELP = ("Music 제어 권한이 필요합니다. 시스템 설정 → 개인정보 보호 및 보안 → 자동화에서 "
                   "LocalTag(또는 터미널) 아래의 'Music'을 켜 주세요.")

# 1단계: 경로로 곡을 찾아 persistent ID 만 얻는다 (아직 새로 읽기는 하지 않으므로 파일이 움직이지 않는다).
#   LOCALTAG_RESULT:<경로>\t<found|notfound|error 번호>\t<persistent ID>
LOOKUP_SCRIPT = '''
on run wantedPaths
    tell application "Music"
        if not running then error "Apple Music을 먼저 열어 주세요."
    end tell
    set i to 0
    repeat with wantedPath in wantedPaths
        set p to contents of wantedPath
        set resultLine to p & tab & "error" & tab & ""
        try
            set f to (POSIX file p) as alias
            tell application "Music"
                with timeout of 90 seconds
                    set matches to (every file track of library playlist 1 whose location is f)
                end timeout
                if (count of matches) is 0 then
                    set resultLine to p & tab & "notfound" & tab & ""
                else
                    set resultLine to p & tab & "found" & tab & (persistent ID of item 1 of matches)
                end if
            end tell
        on error errText number errNum
            if errNum is -1743 then error errText number errNum
            set resultLine to p & tab & "error " & errNum & tab & ""
        end try
        set i to i + 1
        log "LOCALTAG_RESULT:" & resultLine
        log "LOCALTAG_PROGRESS:" & i
    end repeat
    return "DONE"
end run
'''

# persistent ID 로 곡을 찾아 현재 Music에 보이는 정보를 읽거나(read), 다시 새로 읽게 한다(refresh).
#   read:    인자 = read, <pid>, <pid>, …
#   refresh: 인자 = refresh, <최소 대기 칸>, <최대 대기 칸>, <pid>, <제목>, <앨범>, <pid>, <제목>, <앨범>, …
#            (한 칸 = 0.2초)
#   LOCALTAG_INFO:<pid>\t<제목>\t<앨범>\t<경로>
# 새로 읽기는 한 곡씩, Music이 그 곡을 다 처리할 때까지 기다린 뒤 다음 곡으로 넘어간다:
#   - 최소 대기 시간이 지났고
#   - Music에 새 제목·앨범이 보이고
#   - Music이 알려 주는 파일 위치가 더 이상 바뀌지 않으며(앨범명이 바뀌면 Music이 파일을 새 앨범 폴더로 옮긴다)
#     그 자리에 실제로 파일이 있을 때.
# 너무 빨리 몰아 보내면 Music 앨범 화면이 따라오지 못해 Music을 다시 켤 때까지 곡이 다른 앨범으로 갈라져 보인다.
ID_SCRIPT = '''
on fileExists(p)
    try
        (POSIX file p) as alias
        return true
    on error
        return false
    end try
end fileExists

on run arguments
    set mode to item 1 of arguments
    set args to rest of arguments
    set stepCount to 1
    set minTicks to 0
    set maxTicks to 0
    if mode is "refresh" then
        set stepCount to 3
        set minTicks to (item 1 of args) as integer
        set maxTicks to (item 2 of args) as integer
        set args to rest of (rest of args)
    end if
    set i to 0
    tell application "Music"
        repeat with k from 1 to (count of args) by stepCount
            set pid to item k of args
            try
                with timeout of 60 seconds
                    set t to first file track of library playlist 1 whose persistent ID is pid
                    if mode is "refresh" then
                        set wantName to item (k + 1) of args
                        set wantAlbum to item (k + 2) of args
                        refresh t
                        set lastLoc to ""
                        set steady to 0
                        repeat with tick from 1 to maxTicks
                            delay 0.2
                            set curLoc to ""
                            try
                                set curLoc to POSIX path of (get location of t)
                            end try
                            if curLoc is not "" and curLoc is lastLoc and (my fileExists(curLoc)) then
                                set steady to steady + 1
                            else
                                set steady to 0
                            end if
                            set lastLoc to curLoc
                            if tick >= minTicks and steady >= 2 then
                                if wantName is "" and wantAlbum is "" then exit repeat
                                if (name of t as text) is wantName and (album of t as text) is wantAlbum then exit repeat
                            end if
                        end repeat
                    end if
                    set loc to ""
                    try
                        set loc to POSIX path of (get location of t)
                    end try
                    set infoLine to pid & tab & (name of t as text) & tab & (album of t as text) & tab & loc
                end timeout
                log "LOCALTAG_INFO:" & infoLine
            on error errText number errNum
                if errNum is -1743 then error errText number errNum
                log "LOCALTAG_INFO:" & pid & tab & "" & tab & "" & tab & "" & tab & "error " & errNum
            end try
            set i to i + 1
            log "LOCALTAG_PROGRESS:" & i
        end repeat
    end tell
    return "DONE"
end run
'''

# 반영 속도: 곡 하나를 새로 읽게 한 뒤 다음 곡까지 (최소, 최대) 기다리는 시간(초)
PACES = {"fast": (0.8, 4.0), "normal": (1.5, 6.0), "slow": (3.0, 10.0)}
PACE_LABELS = {"fast": "빠르게 (곡마다 0.8초 이상)", "normal": "보통 (곡마다 1.5초 이상)",
               "slow": "천천히 (곡마다 3초 이상)"}
DEFAULT_PACE = "slow"

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


def _osascript_js(script, timeout=120):
    """JavaScript for Automation 으로 실행 (곡 정보를 한꺼번에 JSON 으로 받을 때)."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MusicError("Music이 응답하지 않습니다. 보관함이 크면 시간이 걸릴 수 있습니다. 다시 시도해 주세요.") from e
    if r.returncode:
        if "-1743" in r.stderr:
            raise MusicError(PERMISSION_HELP)
        if "-600" in r.stderr:
            raise MusicError("Apple Music을 먼저 열어 주세요.")
        raise MusicError("Music 작업에 실패했습니다.\n" + r.stderr.strip())
    return r.stdout


def ensure_access(launch=False):
    """Music을 쓸 수 있는지 확인한다. 아직 묻지 않은 'Apple Music 보관함 접근' 권한은 먼저 받는다."""
    if media_library_status() == "ask":
        request_media_library()
    if launch:
        subprocess.run(["/usr/bin/open", "-g", "-b", "com.apple.Music"], capture_output=True, timeout=15)
    out = _osascript(PREFLIGHT, timeout=150)
    if out.strip() != "READY":
        raise MusicError("Music 접근 확인에 실패했습니다.")


def automation_status() -> str:
    """권한 창을 띄우지 않고 'Music 제어' 권한 상태를 확인한다.
    'granted' / 'denied' / 'ask' (아직 묻지 않음) / 'unknown' (Music이 꺼져 있는 등)"""
    try:
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")

        class AEDesc(ctypes.Structure):
            _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]

        def fourcc(s):
            return int.from_bytes(s.encode("ascii"), "big")
        bundle = b"com.apple.Music"
        target = AEDesc()
        cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(AEDesc)]
        if cs.AECreateDesc(fourcc("bund"), bundle, len(bundle), ctypes.byref(target)):
            return "unknown"
        fn = cs.AEDeterminePermissionToAutomateTarget
        fn.argtypes = [ctypes.POINTER(AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_ubyte]
        fn.restype = ctypes.c_int32
        status = fn(ctypes.byref(target), fourcc("****"), fourcc("****"), 0)
        cs.AEDisposeDesc.argtypes = [ctypes.POINTER(AEDesc)]
        cs.AEDisposeDesc(ctypes.byref(target))
    except Exception:
        return "unknown"
    return {0: "granted", -1743: "denied", -1744: "ask"}.get(status, "unknown")


# ---- 'Apple Music 보관함 접근'(시스템 설정의 '미디어 및 Apple Music') 권한
# Music 제어(자동화)와는 따로 관리되는 권한이다. Music에 반영할 때 Music이 이 권한을 요구해 처음 반영할 때 권한 창이
# 뜨므로, 앱을 켤 때 미리 상태를 보고 받아 둔다. 공개 API로는 상태를 알 수 없어 상태 확인(창을 띄우지 않음)에만
# macOS의 TCC 프레임워크를 쓰고, 쓸 수 없으면 'unknown' 으로 두어 예전처럼 Music이 필요할 때 묻게 둔다.
# 권한 요청은 Apple의 공개 프레임워크(iTunesLibrary)로 보관함을 한 번 여는 방식으로 한다 — macOS가 같은 권한 창을 띄운다.
# (TCC 의 요청 함수는 결과를 받을 블록을 요구해 ctypes 로 부르면 응답 순간 앱이 죽는다. 0.6.4~0.6.5 크래시)
_MEDIA_SERVICE = b"kTCCServiceMediaLibrary"


def _tcc_call(name):
    """TCCAccessPreflight 처럼 (서비스, 옵션) 만 받고 바로 결과를 돌려주는 함수만 부를 것."""
    tcc = ctypes.CDLL("/System/Library/PrivateFrameworks/TCC.framework/TCC")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    fn = getattr(tcc, name)
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    svc = cf.CFStringCreateWithCString(None, _MEDIA_SERVICE, 0x08000100)  # UTF-8
    try:
        return fn(svc, None)
    finally:
        cf.CFRelease(svc)


def media_library_status() -> str:
    """권한 창을 띄우지 않고 'Apple Music 보관함 접근' 상태를 본다. 'granted' / 'denied' / 'ask' / 'unknown'"""
    try:
        return {0: "granted", 1: "denied", 2: "ask"}.get(_tcc_call("TCCAccessPreflight"), "unknown")
    except Exception:
        return "unknown"


_media_requested = False


def request_media_library(cancelled=lambda: False) -> bool:
    """macOS 권한 창을 띄워 'Apple Music 보관함 접근'을 받는다 (작업 스레드에서 부를 것). 앱을 켠 동안 한 번만 한다.
    보관함을 열었으면 True.

    권한 상태(TCCAccessPreflight)는 앱 안에서 기억되어, 허용한 뒤에도 앱을 다시 켤 때까지 '아직 묻지 않음'으로
    보일 수 있다. 그래서 상태가 바뀌기를 기다리지 않는다 (0.6.6 에서 허용 후에도 진행 창이 닫히지 않던 문제).
    """
    global _media_requested
    if _media_requested:
        return True
    _media_requested = True
    try:
        o = _bridge()
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/iTunesLibrary.framework/iTunesLibrary")
        o.lib.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        o.lib.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        pool = o.lib.objc_autoreleasePoolPush()
        try:
            err = ctypes.c_void_p()
            lib = o.send(o.cls("ITLibrary"), "libraryWithAPIVersion:error:", o.nsstr("1.0"), ctypes.byref(err),
                         argtypes=[ctypes.c_void_p, ctypes.c_void_p])
            return bool(lib)
        finally:
            o.lib.objc_autoreleasePoolPop(pool)
    except Exception:
        return False


def open_privacy_settings(kind: str = "automation"):
    pane = "Privacy_Media" if kind == "media" else "Privacy_Automation"
    subprocess.run(["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?" + pane], check=False)


def _run_script(script, args, progress, cancelled, idle_limit=240):
    """osascript 를 돌리면서 log 줄을 실시간으로 읽는다. (log 줄 목록) 을 돌려준다."""
    with tempfile.TemporaryFile() as out, tempfile.NamedTemporaryFile() as errs:
        proc = subprocess.Popen(["/usr/bin/osascript", "-e", script, "--", *args], stdout=out, stderr=errs)
        reader = open(errs.name, "rb")
        offset, pending, lines = 0, b"", []
        last = time.monotonic()
        try:
            while True:
                reader.seek(offset)
                data = reader.read()
                offset += len(data)
                pending += data
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace")
                    m = re.search(r"LOCALTAG_PROGRESS:(\d+)", text)
                    if m:
                        progress(int(m[1]))
                        last = time.monotonic()
                    else:
                        lines.append(text)
                if proc.poll() is not None:
                    reader.seek(offset)
                    if reader.read():
                        continue
                    break
                if cancelled():
                    raise MusicError("취소했습니다. 일부 곡은 이미 반영됐을 수 있습니다.")
                if time.monotonic() - last > idle_limit:
                    raise MusicError("Music이 오래 응답하지 않습니다. 일부 곡은 이미 반영됐을 수 있습니다.")
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.close()
        if pending:
            lines.append(pending.decode("utf-8", "replace"))
        if proc.returncode:
            detail = "\n".join(l for l in lines if "LOCALTAG_" not in l)
            if "-1743" in detail:
                raise MusicError(PERMISSION_HELP)
            if "-600" in detail or "먼저 열어" in detail:
                raise MusicError("Apple Music을 먼저 열어 주세요.")
            raise MusicError("Music 작업을 끝내지 못했습니다.\n" + detail.strip())
        return lines


def _tagged(lines, marker):
    return [l.split(marker, 1)[1].split("\t") for l in lines if marker in l]


def _norm(s):
    return unicodedata.normalize("NFC", (s or "").strip()).casefold()


class RefreshResult:
    def __init__(self, total):
        self.total = total
        self.confirmed = 0      # Music에 새 정보가 보이는 것까지 확인
        self.unconfirmed = []   # 명령은 보냈지만 Music에 아직 옛 정보가 보임 (처음 받은 경로)
        self.notfound = []      # Music 보관함에 없는 파일
        self.failed = []        # (처음 받은 경로, 이유)
        self.relocated = {}     # 처음 받은 경로 → 지금 경로 (옮겨진 경우만)
        self.changed = []       # 반영하는 사이 Music 등에서 파일이 다시 바뀐 곡 (처음 받은 경로) — 그 값을 따른다
        self.retried = 0


def _changed_elsewhere(file_values, path, want) -> bool:
    """파일에 든 제목·앨범이 LocalTag 가 저장한 값과 다르면 True (그사이 다른 곳에서 고침)."""
    try:
        title, album = file_values(path)
    except Exception:
        return False
    w_title, w_album = want
    return bool((w_title and _norm(title) != _norm(w_title)) or (w_album and _norm(album) != _norm(w_album)))


def _lookup(cur, keys, progress, cancelled):
    """keys 의 곡을 지금 경로(cur[key])로 찾는다. → ({key: pid}, {key: 'notfound'|'error ..'})"""
    by_path = {}
    for k in keys:
        by_path.setdefault(cur[k], []).append(k)
    paths = list(by_path)
    pids, bad = {}, {}
    for start in range(0, len(paths), 40):  # 명령줄 길이 제한
        batch = paths[start:start + 40]

        def report(n, start=start, batch=batch, total=len(paths)):
            name = os.path.basename(batch[min(n, len(batch) - 1)])
            progress(start + n, total, f"Music에서 곡 찾는 중 ({start + n}/{total}) · {name}")
        lines = _run_script(LOOKUP_SCRIPT, batch, report, cancelled)
        seen = set()
        for parts in _tagged(lines, "LOCALTAG_RESULT:"):
            if len(parts) < 3 or parts[0] not in by_path:
                continue
            path, status, pid = parts[0], parts[1], parts[2]
            seen.add(path)
            for k in by_path[path]:
                if pid:
                    pids[k] = pid
                else:
                    bad[k] = status
        for path in batch:
            if path not in seen:
                for k in by_path[path]:
                    bad[k] = "응답 없음"
    return pids, bad


def refresh(paths, progress=lambda *_: None, cancelled=lambda: False, expected=None, rounds=3,
            relocate=None, pace=DEFAULT_PACE, file_values=None):
    """바꾼 파일을 Music에서 다시 읽게 하고, 실제로 반영됐는지 확인한다.

    1) 경로로 곡을 모두 찾아 persistent ID 를 얻는다 (아직 아무것도 반영하지 않음)
    2) persistent ID 로 새로 읽게 한다 — Music이 반영하면서 파일·폴더를 옮겨도 놓치지 않는다
    3) Music에 보이는 제목·앨범을 expected 와 비교하고, 다르면 잠시 뒤 다시 반영한다

    expected: {경로: (제목, 앨범)}
    relocate: 옮겨진 파일의 새 경로를 찾는 함수 ([경로] → {옛 경로: 새 경로}). 찾기 재시도 전에 부른다.
    pace: 반영 속도 (PACES 의 키)
    file_values: 경로 → 파일에 지금 들어 있는 (제목, 앨범). Music 에 보이는 값이 기대와 다를 때, 파일도 이미
        다른 값으로 바뀌었으면(반영하는 사이 Music 에서 직접 고친 경우) 다시 반영하지 않고 그 값을 따른다.
    """
    min_wait, max_wait = PACES.get(pace, PACES[DEFAULT_PACE])
    ticks = [str(max(1, round(min_wait / 0.2))), str(max(2, round(max_wait / 0.2)))]
    keys = list(dict.fromkeys(os.path.realpath(p) for p in paths))
    if not keys:
        raise MusicError("Music에 반영할 곡을 선택해 주세요.")
    expected = {os.path.realpath(k): v for k, v in (expected or {}).items()}
    cur = {k: k for k in keys}
    res = RefreshResult(len(keys))
    progress(0, len(keys), "Music 권한 확인 중 · 권한 창이 뜨면 허용해 주세요")
    ensure_access()

    # ---- 1) 찾기 (실패하면 새 위치를 확인하고 잠시 뒤 다시)
    pid_of, bad = {}, {}
    todo = list(keys)
    for attempt in range(3):
        if not todo or cancelled():
            break
        if attempt:
            if relocate:
                moved = relocate([cur[k] for k in todo]) or {}
                for k in todo:
                    if cur[k] in moved:
                        cur[k] = moved[cur[k]]
            wait = 2.0 * attempt
            progress(0, len(todo), f"Music이 곡을 찾지 못해 {wait:.0f}초 뒤 다시 찾습니다 ({len(todo)}곡)")
            time.sleep(wait)
        pids, errs = _lookup(cur, todo, progress, cancelled)
        pid_of.update(pids)
        for k in pids:
            bad.pop(k, None)
        retry = []
        for k, status in errs.items():
            if status == "notfound" and os.path.exists(cur[k]):
                res.notfound.append(k)
            else:
                bad[k] = status
                retry.append(k)
        todo = retry
    for k, status in bad.items():
        if not os.path.exists(cur[k]):
            why = "파일이 옮겨졌거나 지워졌습니다. 목록을 새로 고침(F5)해 보세요"
        else:
            why = f"Music에서 곡을 찾지 못했습니다 ({status})"
        res.failed.append((k, why))

    # ---- 2) 새로 읽기 · 3) 확인과 재시도 (모두 persistent ID 로)
    pending = dict(pid_of)
    for rnd in range(rounds + 2):
        if not pending or cancelled():
            break
        if rnd == 0:
            mode, msg = "refresh", "Music에서 새로 읽는 중"
        else:
            time.sleep(max(1.5, min_wait) if rnd == 1 else 2.5 + min_wait)  # Music이 마저 정리할 시간
            mode = "read" if rnd == 1 else "refresh"
            msg = "Music에 반영됐는지 확인하는 중" if rnd == 1 else f"아직 옛 정보인 곡 다시 반영 · {rnd - 1}번째"
        ids = list(dict.fromkeys(pending.values()))
        if mode == "refresh":
            want = {}
            for k, pid in pending.items():
                want.setdefault(pid, expected.get(k) or ("", ""))
            args = ticks + [x for pid in ids for x in (pid, *want[pid])]
        else:
            args = ids
        lines = _run_script(ID_SCRIPT, [mode, *args],
                            lambda n, total=len(ids), msg=msg: progress(n, total, f"{msg} ({n}/{total})"), cancelled,
                            idle_limit=240 + max_wait)
        if rnd == 0:
            continue  # 새로 읽기 직후 정보는 아직 옛것일 수 있으므로 확인은 다음 단계에서
        if rnd >= 2:
            res.retried += len(ids)
        info = {p[0]: p for p in _tagged(lines, "LOCALTAG_INFO:") if p}
        still = {}
        for k, pid in pending.items():
            row = info.get(pid)
            if row is None or len(row) < 4 or (len(row) > 4 and row[4].startswith("error")):
                still[k] = pid
                continue
            _, name, album, loc = row[:4]
            if loc:
                cur[k] = loc
            want = expected.get(k)
            ok = True
            if want:
                title, want_album = want
                if title and _norm(name) != _norm(title):
                    ok = False
                if want_album and _norm(album) != _norm(want_album):
                    ok = False
            if ok:
                res.confirmed += 1
            elif file_values and _changed_elsewhere(file_values, cur[k], want):
                res.changed.append(k)
            else:
                still[k] = pid
        pending = still
    res.unconfirmed = list(pending)
    res.relocated = {k: v for k, v in cur.items() if os.path.realpath(v) != k}
    return res


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
    if sys.platform != "darwin":
        return False
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
