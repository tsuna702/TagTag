"""안전 저장: 사본에 쓰고 검증한 뒤 원본에 반영한다 (앨범태그 core.py 방식).

- 사본은 메모리에 둔다 — 쓴 부분만 메모리에 두고 나머지는 원본에서 읽는다. 곡 크기와 상관없이 태그 부분만 읽고,
  곡 폴더에 임시 파일을 만들지 않는다 (복제본이 안 되는 외장 디스크(exFAT 등)에서 곡을 통째로 복사하지 않는다)
- 원본에는 달라진 부분만 다시 쓴다 (파일 번호 유지). 오디오 데이터(MP3 프레임, FLAC 프레임, M4A mdat)의
  위치가 그대로면 원본의 오디오는 읽지도 쓰지도 않고 태그 부분만 비교해 다시 쓴다 (곡당 수 MB → 수백 KB).
  태그가 커져 오디오가 뒤로 밀렸으면 오디오 데이터가 바이트 단위로 같은지 확인한 뒤 다시 쓴다
- 작업 도중 다른 프로그램이 원본을 바꿨으면 교체하지 않음
- iCloud에서 아직 내려받지 않은 파일, 링크, 읽기 전용 파일은 건드리지 않음
"""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import struct
import sys
import threading
import uuid

import mutagen

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
if IS_MAC:  # 맥에서만 쓴다 (Windows 는 앱이 켜지는 시간을 아낀다)
    import ctypes

SF_DATALESS = 0x40000000  # iCloud 등에 있고 아직 내려받지 않은 파일
RECALL_ON_DATA_ACCESS = 0x400000  # Windows: OneDrive "온라인 전용" 파일
STAGE_PREFIX = ".localtag-"

# Windows 는 os.open 이 기본으로 텍스트 모드라 줄 끝을 바꾼다 → 늘 바이너리로
O_BINARY = getattr(os, "O_BINARY", 0)

if hasattr(os, "pread"):
    _pread, _pwrite = os.pread, os.pwrite
else:  # Windows 에는 pread/pwrite 가 없다 — 한 fd 를 한 스레드에서만 쓰므로 lseek 로 충분
    def _pread(fd, n, off):
        os.lseek(fd, off, os.SEEK_SET)
        return os.read(fd, n)

    def _pwrite(fd, data, off):
        os.lseek(fd, off, os.SEEK_SET)
        return os.write(fd, data)

# 작업 사본은 메모리에 둔다. 디스크 사본은 맥의 내장 디스크(복제본)에서도 곡당 1 ms 넘게 들고, 복제본이 없는 디스크(exFAT·NTFS)에서는
# 곡을 통째로 복사한다 (exFAT 디스크 이미지: 곡당 146 ms → 3 ms). Windows 는 새로 만든 파일을 열 때마다 백신(Defender)도 검사한다.
# 메모리 사본에 쓴 양이 이보다 커지면 디스크 사본으로 다시 한다 (태그가 커져 아주 큰 파일의 오디오를 옮겨야 할 때뿐이다).
MEM_STAGE = True
MEM_STAGE_LIMIT = 256 * 1024 * 1024
PAGE = 64 * 1024
READ_AHEAD = 128 * 1024  # 작은 읽기가 이어지므로 원본에서 이만큼 앞서 읽어 둔다


def _pread_full(fd, n, off) -> bytes:
    data = _pread(fd, n, off)
    if len(data) == n or not data:
        return data
    parts, got = [data], len(data)
    while got < n:
        more = _pread(fd, n - got, off + got)
        if not more:
            break
        parts.append(more)
        got += len(more)
    return b"".join(parts)


class StageTooBig(Exception):
    """메모리 사본에 쓴 양이 MEM_STAGE_LIMIT 를 넘었다."""


class MemStage:
    """메모리에 둔 작업 사본 (copy-on-write): 원본은 읽기만 하고 쓴 부분만 메모리에 둔다. 태그만 바꾸면 곡이 아무리 커도
    태그 부분만 읽는다. mutagen 이 쓰는 파일 객체처럼 동작하고 원본은 고치지 않는다.
    base: 내용(bytes) 또는 읽기로 연 원본의 fd (over 로 연다). name 은 원본 경로 (확장자로 형식을 가린다)."""

    def __init__(self, base, name: str, size: int | None = None):
        self.name = name
        self._data, self._fd = (base, None) if isinstance(base, (bytes, bytearray)) else (None, base)
        self._base_size = len(base) if self._fd is None else size  # 잘라 내면 줄어든다 (그 뒤쪽은 0 으로 읽힌다)
        self._size = self._base_size
        self._pos = 0
        self._pages: dict[int, bytearray] = {}  # 쓴 부분 (쪽 번호 → PAGE 바이트)
        self._win_off, self._win = 0, b""

    @classmethod
    def over(cls, path: str) -> "MemStage":
        fd = os.open(path, os.O_RDONLY | O_BINARY)
        try:
            return cls(fd, path, os.fstat(fd).st_size)
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        """원본을 닫는다. Windows 는 열려 있는 파일의 이름을 바꾸거나 교체할 수 없다."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def length(self) -> int:
        return self._size

    def _base(self, off, n) -> bytes:
        """원본의 off 부터 최대 n 바이트."""
        n = min(n, self._base_size - off)
        if n <= 0:
            return b""
        if self._fd is None:
            return bytes(self._data[off:off + n])
        i = off - self._win_off
        if i >= 0 and i + n <= len(self._win):
            return self._win[i:i + n]
        if n >= READ_AHEAD:
            return _pread_full(self._fd, n, off)
        self._win, self._win_off = _pread_full(self._fd, READ_AHEAD, off), off
        return self._win[:n]

    def pread(self, n, off) -> bytes:
        """off 부터 최대 n 바이트 (현재 위치는 그대로)."""
        n = min(n, self._size - off)
        if n <= 0:
            return b""
        pages = self._pages
        if not pages:
            data = self._base(off, n)
            return data if len(data) == n else data + bytes(n - len(data))
        parts, end = [], off + n
        while off < end:
            i = off // PAGE
            page = pages.get(i)
            if page is not None:
                stop = min(end, (i + 1) * PAGE)
                parts.append(page[off - i * PAGE:stop - i * PAGE])
            else:  # 쓰지 않은 쪽이 이어지면 원본에서 한 번에
                j = i + 1
                while j * PAGE < end and j not in pages:
                    j += 1
                stop = min(end, j * PAGE)
                data = self._base(off, stop - off)
                parts.append(data)
                if len(data) != stop - off:
                    parts.append(bytes(stop - off - len(data)))
            off = stop
        return b"".join(parts)

    def read(self, n=-1) -> bytes:
        data = self.pread(self._size if n is None or n < 0 else n, self._pos)
        self._pos += len(data)
        return data

    def write(self, b) -> int:
        n = len(b)
        if not n:
            return 0
        off, end, data = self._pos, self._pos + n, memoryview(b)
        pages, src = self._pages, 0
        while off < end:
            i, at = off // PAGE, off % PAGE
            k = min(PAGE - at, end - off)
            page = pages.get(i)
            if page is None:
                if len(pages) * PAGE >= MEM_STAGE_LIMIT:
                    raise StageTooBig
                if k == PAGE:  # 한 쪽 전체를 덮어쓰면 원본을 읽을 필요가 없다
                    pages[i] = bytearray(data[src:src + k])
                    src, off = src + k, off + k
                    continue
                page = pages[i] = bytearray(self._base(i * PAGE, PAGE)).ljust(PAGE, b"\0")
            page[at:at + k] = data[src:src + k]
            src, off = src + k, off + k
        self._pos = end
        self._size = max(self._size, end)
        return n

    def seek(self, off, whence=0) -> int:
        pos = off + (self._pos if whence == 1 else self._size if whence == 2 else 0)
        if pos < 0:
            raise OSError(22, "Invalid argument")  # 실제 파일처럼
        self._pos = pos
        return pos

    def tell(self) -> int:
        return self._pos

    def truncate(self, size=None) -> int:
        size = self._pos if size is None else size
        if size < self._size:
            self._base_size = min(self._base_size, size)
            i = size // PAGE
            page = self._pages.get(i)
            if page is not None:  # 다시 늘어나면 잘린 부분은 0 으로 읽혀야 한다
                page[size - i * PAGE:] = bytes(PAGE - (size - i * PAGE))
            for j in [j for j in self._pages if j > i]:
                del self._pages[j]
        self._size = size
        return size

    def flush(self):
        pass

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True


def rewind(target):
    """mutagen 에 넘기기 전에: 경로는 그대로, 메모리 사본은 처음으로 되감는다 (mutagen 은 지금 위치부터 읽는다)."""
    if not isinstance(target, str):
        target.seek(0)
    return target


def _ext(target) -> str:
    return os.path.splitext(target if isinstance(target, str) else target.name)[1].lower()


class SafeWriteError(Exception):
    pass


class StageKeptError(SafeWriteError):
    """원본을 고치지 못해 검증된 작업 사본을 일부러 남겨 둔 경우."""


def check_writable(path: str):
    try:
        st = os.lstat(path)
    except FileNotFoundError as e:
        raise SafeWriteError("파일이 없습니다. 옮겨졌거나 지워졌을 수 있습니다.") from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SafeWriteError("일반 파일만 편집할 수 있습니다. 링크 파일은 제외됩니다.")
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        raise SafeWriteError("아직 내려받지 않은 iCloud 파일입니다. Finder에서 먼저 내려받아 주세요.")
    if getattr(st, "st_file_attributes", 0) & RECALL_ON_DATA_ACCESS:
        raise SafeWriteError("아직 내려받지 않은 OneDrive 파일입니다. 탐색기에서 '항상 이 장치에 유지'로 먼저 내려받아 주세요.")
    if not os.access(path, os.W_OK):
        raise SafeWriteError("읽기 전용 파일입니다.")
    return st


def is_dataless(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS
                or getattr(st, "st_file_attributes", 0) & RECALL_ON_DATA_ACCESS)


# Windows 는 1MB 넘게 메모리를 잡을 때마다 새 쪽을 받느라(VirtualAlloc) 느리다 — 256KB 로 나누면 읽어 비교하는 속도가 2배가 넘는다
CHUNK = 256 * 1024 if IS_WIN else 1024 * 1024

# copyfile(3): cp -p 처럼 확장 속성(Finder 태그 등)·ACL·시각까지 복사한다.
# COPYFILE_CLONE 이면 APFS에서는 데이터를 복사하지 않는 복제본(clone)을 만들어 즉시 끝나고 공간도 쓰지 않는다
# (어느 한쪽을 고치면 그 부분만 새로 저장되므로 원본과 사본은 서로 영향을 주지 않는다).
COPYFILE_METADATA = 0x7  # ACL·권한·확장 속성
COPYFILE_ALL = 0xF
COPYFILE_CLONE = 1 << 24
COPYFILE_CLONE_FORCE = 1 << 25  # 복제본을 만들 수 없으면 복사하지 않고 실패
_copyfile = None
if IS_MAC:
    try:
        _copyfile = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).copyfile
        _copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
        _copyfile.restype = ctypes.c_int
    except (OSError, AttributeError):
        _copyfile = None


def copy_preserving(src: str, dst: str):
    """메타데이터까지 그대로 복사한다. 같은 APFS 디스크면 복제본(clone)으로 즉시 만든다."""
    if os.path.lexists(dst):
        raise FileExistsError(17, "이미 같은 이름의 파일이 있습니다", dst)
    if not IS_MAC:
        shutil.copy2(src, dst)  # 내용과 시각
        return
    if _copyfile is not None:
        if _copyfile(os.fsencode(src), os.fsencode(dst), None, COPYFILE_ALL | COPYFILE_CLONE) == 0:
            return
        err = ctypes.get_errno()
        if os.path.lexists(dst):
            os.unlink(dst)
        if err not in (0, 45, 78):  # ENOTSUP, ENOSYS 이외의 오류는 그대로 알린다
            raise OSError(err, os.strerror(err), src)
    import subprocess
    subprocess.run(["/bin/cp", "-p", src, dst], check=True, capture_output=True)


def clone_preserving(src: str, dst: str) -> bool:
    """복제본(clone)으로 만들 수 있을 때만 만든다 (맥의 같은 APFS 디스크). 못 만들면 False — 복사하지 않는다."""
    if _copyfile is None or os.path.lexists(dst):
        return False
    if _copyfile(os.fsencode(src), os.fsencode(dst), None, COPYFILE_ALL | COPYFILE_CLONE_FORCE) == 0:
        return True
    if os.path.lexists(dst):
        os.unlink(dst)
    return False


def _fsync(path: str):
    if IS_WIN and os.path.isdir(path):
        return  # Windows 는 폴더를 열어 fsync 할 수 없다 (NTFS 가 이름 바꾸기를 기록해 둔다)
    # Windows 의 fsync(_commit)는 쓰기로 연 파일에만 된다 — 읽기 전용으로 열면 EBADF
    fd = os.open(path, (os.O_RDWR if IS_WIN else os.O_RDONLY) | O_BINARY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Windows 에서 파일을 많이 저장할 때: 바뀐 지 얼마 안 된 파일을 처음 열면 백신이 검사하느라 곡당 5~10 ms, fsync 도 곡당 2~7 ms 가
# 걸리는데 (거의 기다리는 시간) 여러 개를 동시에 하면 곡당 0.5~1 ms 로 줄어든다. 그래서 묶음으로 처리한다.
IO_WORKERS = 16
_local = threading.local()
_pool = None


def _parallel(fn, items):
    """I/O 만 하는 fn 을 여러 스레드로 (기다리는 동안 파이썬을 붙잡지 않으므로 함께 진행된다). 끝날 때까지 기다린다.
    스레드는 한 번 만들어 계속 쓴다 (묶음마다 만들면 곡당 0.1 ms 쯤 든다)."""
    global _pool
    if len(items) < 2:
        for x in items:
            fn(x)
        return
    if _pool is None:
        from concurrent.futures import ThreadPoolExecutor  # 앱이 켜지는 데 시간이 들어 필요할 때 부른다
        _pool = ThreadPoolExecutor(IO_WORKERS, thread_name_prefix="localtag-io")
    list(_pool.map(fn, items))


def prewarm(paths):
    """Windows: 파일들을 미리 한 번씩 동시에 열어 백신 검사를 끝내 둔다. 끝난 뒤에는 어느 파일도 열려 있지 않다
    (열려 있으면 이름을 바꿀 수 없다). 다른 운영체제에서는 아무것도 하지 않는다."""
    if not IS_WIN or len(paths) < 2:
        return

    def one(p):
        try:
            os.close(os.open(p, os.O_RDONLY | O_BINARY))
        except OSError:
            pass  # 실제로 쓸 때 알린다

    _parallel(one, paths)


class IoBatch:
    """파일을 묶음으로 저장한다: with 안에서 이 스레드의 write_back 은 fsync 를 미루고, next(다음 묶음 경로) 를 부르면
    앞 묶음을 한꺼번에 디스크에 확정한 뒤 다음 묶음을 미리 열어 둔다 (prewarm). with 를 나갈 때 나머지를 확정한다.
    맥은 곡마다 fsync 한다 (내장 디스크 0.1 ms, exFAT 이미지 1 ms — 스레드로 나눌 만큼 들지 않는다). errors: 확정하지 못한 [(경로, 이유)]."""

    def __init__(self):
        self.errors = []
        self._pending = []

    def __enter__(self):
        if IS_WIN:
            _local.pending = self._pending
        return self

    def __exit__(self, *_):
        _local.pending = None
        self.flush()

    def next(self, paths):
        self.flush()
        prewarm(paths)

    def moved(self, old, new):
        """미뤄 둔 파일의 이름을 바꿨다."""
        self._pending[:] = [new if p == old else p for p in self._pending]

    def flush(self):
        paths = list(dict.fromkeys(self._pending))
        self._pending.clear()

        def one(p):
            try:
                _fsync(p)
            except (FileNotFoundError, PermissionError):
                pass  # 그사이 옮겨졌거나 다른 프로그램이 열었다 — 쓴 내용은 이미 시스템에 넘어갔다
            except OSError as e:
                self.errors.append((p, f"디스크에 확정하지 못했습니다: {e}"))

        _parallel(one, paths)


def _reader(target):
    """경로면 열고, 메모리 사본이면 그대로 (닫지 않는다)."""
    if isinstance(target, str):
        return open(target, "rb")
    target.seek(0)
    return contextlib.nullcontext(target)


def payload_regions(path) -> list[tuple[int, int]] | None:
    """태그를 뺀 압축 오디오 데이터의 위치 [(시작, 길이)]. 확인할 수 없는 형식이면 None. path 는 경로 또는 메모리 사본."""
    ext = _ext(path)
    if ext not in (".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".m4r"):
        return None
    with _reader(path) as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        regions = []
        if ext == ".mp3":
            head = f.read(10)
            start = 0
            if head[:3] == b"ID3" and len(head) == 10:
                if any(x & 0x80 for x in head[6:10]):
                    return None
                start = 10 + sum(x << (7 * (3 - i)) for i, x in enumerate(head[6:10]))
                if head[5] & 0x10:  # footer
                    start += 10
            end = size
            if size >= 128:
                f.seek(-128, 2)
                if f.read(3) == b"TAG":
                    end -= 128
            regions = [(start, end - start)]
        elif ext == ".flac":
            if f.read(4) != b"fLaC":
                return None
            while True:
                head = f.read(4)
                if len(head) != 4:
                    return None
                n = int.from_bytes(head[1:], "big")
                if f.tell() + n > size:
                    return None
                f.seek(n, 1)
                if head[0] & 0x80:
                    break
            regions = [(f.tell(), size - f.tell())]
        else:
            pos = 0
            while pos < size:
                f.seek(pos)
                head = f.read(8)
                if len(head) != 8:
                    return None
                n, atom = struct.unpack(">I4s", head)
                header = 8
                if n == 1:
                    ext8 = f.read(8)
                    if len(ext8) != 8:
                        return None
                    n = struct.unpack(">Q", ext8)[0]
                    header = 16
                elif n == 0:
                    n = size - pos
                if n < header or pos + n > size:
                    return None
                if atom == b"mdat":
                    regions.append((pos + header, n - header))
                pos += n
    if not regions or any(n <= 0 for _, n in regions):
        return None
    return regions


def _same_bytes(fa, fb, ra, rb) -> bool:
    for (sa, n), (sb, _) in zip(ra, rb):
        off = 0
        while off < n:
            k = min(CHUNK, n - off)
            fa.seek(sa + off)
            fb.seek(sb + off)
            a = fa.read(k)
            if len(a) != k or a != fb.read(k):
                return False
            off += k
    return True


def same_payload(original: str, modified) -> bool | None:
    """두 파일의 오디오 데이터가 바이트 단위로 같은지. 원본 형식을 확인할 수 없으면 None."""
    ra = payload_regions(original)
    if ra is None:
        return None
    rb = payload_regions(modified)
    if rb is None or [n for _, n in ra] != [n for _, n in rb]:
        return False
    with _reader(original) as fa, _reader(modified) as fb:
        return _same_bytes(fa, fb, ra, rb)


def _length(path):
    try:
        mf = mutagen.File(rewind(path))
    except Exception:
        return None
    return None if mf is None else round(float(getattr(mf.info, "length", 0) or 0), 2)


AUDIO_CHANGED = "오디오 데이터 검증에 실패했습니다. 원본은 그대로입니다."
IN_USE = "다른 프로그램(플레이어 등)이 파일을 사용 중이라 저장하지 못했습니다. 원본은 그대로입니다."


def error_text(e: Exception) -> str:
    """사용자에게 보일 오류 문구. Windows 공유 위반(백업·이름 바꾸기 등 어디서 나든)은 알기 쉬운 말로."""
    # open()·os.open() 은 공유 위반을 errno 13 으로만 알린다 (winerror 없음) — 읽기 전용 파일은 check_writable 이 따로 알린다
    if IS_WIN and isinstance(e, PermissionError) and getattr(e, "winerror", None) in (None, 32):
        return IN_USE
    return str(e)


def safe_modify(path: str, fn, enabled: bool = True, keep_mtime: bool = False, inspect=None, before_move=None):
    """fn(작업할_경로)로 태그를 바꾼다. enabled면 사본에서 작업하고 검증 후 원본에 반영.

    작업 사본은 메모리 사본(MemStage)이거나 (아주 큰 파일의 오디오를 옮길 때) 디스크 파일 경로다 — fn·inspect 는
    mutagen 에 넘기기 전에 rewind() 를 거친다.
    fn 이 바꾸기 전 재생 시간(초)을 돌려주면 그것을 쓰고, 아니면 따로 읽는다.
    inspect(경로) 가 있으면 바뀐 파일을 읽은 결과(.length 가 있는 객체)를 돌려준다 —
    검증에 쓴 것을 그대로 돌려주므로 저장 뒤 파일을 다시 읽지 않아도 된다.
    before_move() 는 원본의 오디오까지 다시 쓰게 될 때(태그가 커져 오디오가 밀림) 쓰기 직전에 부른다.
    """
    st = check_writable(path)
    if not enabled:
        if before_move:
            before_move()
        fn(path)
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return inspect(path) if inspect else None
    folder, ext = os.path.dirname(path), os.path.splitext(path)[1]
    stage = None
    keep_stage = False
    result = None

    def disk_stage():
        nonlocal stage
        stage = os.path.join(folder, f"{STAGE_PREFIX}{uuid.uuid4().hex}{ext}")
        copy_preserving(path, stage)

    try:
        try:
            if MEM_STAGE:
                stage = MemStage.over(path)
            else:
                disk_stage()
        except PermissionError as e:
            if IS_WIN:  # 공유 위반: 다른 프로그램이 열어 둠 (읽기 전용은 check_writable 이 이미 걸렀다)
                raise SafeWriteError(IN_USE) from e
            raise
        try:
            before_len = fn(stage)
        except StageTooBig:  # 태그가 커져 아주 큰 파일의 오디오를 옮겨야 한다 → 디스크 사본으로 다시
            stage.close()
            disk_stage()
            before_len = fn(stage)
        before_len = round(float(before_len), 2) if isinstance(before_len, (int, float)) else _length(path)
        if inspect:
            try:
                result = inspect(stage)
                after_len = round(float(result.length), 2)
            except Exception:
                after_len = None
        else:
            after_len = _length(stage)
        if after_len is None:
            raise SafeWriteError("저장한 파일을 다시 읽지 못했습니다. 원본은 그대로입니다.")
        if before_len is not None and abs(after_len - before_len) > 0.05:
            raise SafeWriteError("재생 시간이 달라져 저장을 취소했습니다. 원본은 그대로입니다.")
        # 오디오 데이터 확인: 위치가 그대로면(보통) 덮어쓰기 전 비교에서 함께 확인하고, 옮겨졌으면 따로 비교한다
        ra, rb = payload_regions(path), payload_regions(stage)
        protect, moved = None, False
        if ra is not None:
            if rb is None or [n for _, n in ra] != [n for _, n in rb]:
                raise SafeWriteError(AUDIO_CHANGED)
            if ra == rb:
                protect = ra  # 오디오 위치가 그대로 (보통) — 원본의 오디오는 다시 쓰지 않으므로 그대로 남는다
            elif same_payload(path, stage) is not True:
                raise SafeWriteError(AUDIO_CHANGED)
            else:
                moved = True  # 태그가 커져 오디오가 밀렸다 (같은 내용임은 확인했다)
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            raise SafeWriteError("작업하는 동안 다른 프로그램이 파일을 바꿨습니다. 원본은 그대로입니다.")
        if before_move and protect is None:
            before_move()
        try:
            write_back(stage, path, protect, rewrite_rest=moved)
        except StageKeptError:
            keep_stage = True
            raise
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        if isinstance(stage, str):
            if not keep_stage and os.path.exists(stage):
                os.unlink(stage)
        elif stage is not None:
            stage.close()
    return result


def _outside(size, protect):
    """[0, size) 에서 protect 구간을 뺀 나머지 [(시작, 끝)]."""
    spans, pos = [], 0
    for start, n in sorted(protect or ()):
        if start > pos:
            spans.append((pos, min(start, size)))
        pos = max(pos, start + n)
    if pos < size:
        spans.append((pos, size))
    return [(lo, hi) for lo, hi in spans if lo < hi]


def _differing_chunks(read_src, d, size, protect, rewrite_rest=False):
    """같은 위치끼리 비교해 다른 조각 [(위치, 길이)]. protect(오디오 위치)는 원본에 쓰지 않으므로 읽지도 않는다.
    rewrite_rest: 처음 달라진 곳부터 끝까지는 비교하지 않고 모두 다시 쓴다 (내용이 밀린 경우)."""
    chunks = []
    for lo, hi in _outside(size, protect):
        off = lo
        while off < hi:
            n = min(CHUNK, hi - off)
            if rewrite_rest and chunks:
                chunks.append((off, n))
                off += n
                continue
            a = read_src(n, off)
            if not a:
                raise SafeWriteError("작업 사본을 읽지 못했습니다. 원본은 그대로입니다.")
            if a != _pread(d, len(a), off):
                chunks.append((off, len(a)))
            off += len(a)
    return chunks


def _same_regions(src, dst: str, regions) -> bool:
    """src(경로 또는 메모리 사본)와 dst 의 regions 위치 내용이 같은지."""
    try:
        with _reader(src) as fa, open(dst, "rb") as fb:
            return _same_bytes(fa, fb, regions, regions)
    except OSError:
        return False


def write_back(src, dst: str, protect=None, rewrite_rest=False):
    """검증한 사본(src: 경로 또는 메모리 사본)의 내용을 원본(dst) 파일 안에 다시 쓴다.

    파일을 통째로 바꿔치기(os.replace)하면 내용이 같아도 다른 파일(새 inode)이 되어,
    파일 번호로 곡 위치를 기억하는 Apple Music이 저장 직후 곡을 잠깐 찾지 못한다.
    같은 파일에 덮어쓰면 Music·Finder 별칭·하드 링크가 그대로 유지된다.
    먼저 비교해 달라진 부분(보통 앞쪽의 태그)만 쓰므로 오디오 데이터는 다시 쓰지 않는다.
    protect: 오디오 데이터 위치 [(시작, 길이)] — 원본과 사본에서 같은 위치라 이 부분은 원본 그대로 두고 비교도 하지 않는다.
    rewrite_rest: 내용이 밀린 경우 — 처음 달라진 곳부터는 원본을 읽지 않고 모두 다시 쓴다.
    쓰는 도중에 실패하면 사본으로 원본을 교체해 반쯤 쓰인 파일이 남지 않게 한다 (protect 부분이 같은지 확인한 뒤).
    """
    if isinstance(src, str):
        s = os.open(src, os.O_RDONLY | O_BINARY)
        size = os.fstat(s).st_size

        def read_src(n, off):
            return _pread(s, n, off)
    else:
        s = None
        size = src.length()
        read_src = src.pread
    pending = getattr(_local, "pending", None)  # IoBatch 안이면 fsync 는 묶음이 끝날 때 한꺼번에
    touched = False  # 원본에 쓰기 시작했는지
    try:
        try:
            d = os.open(dst, os.O_RDWR | O_BINARY)
        except PermissionError as e:
            if IS_WIN:  # 다른 프로그램이 열어 둔 파일은 교체도 안 된다
                raise SafeWriteError(IN_USE) from e
            d = None
        except OSError:
            d = None
        if d is not None:
            try:
                try:
                    chunks = _differing_chunks(read_src, d, size, protect, rewrite_rest)
                except OSError as e:
                    raise SafeWriteError(f"원본과 비교하지 못했습니다. 원본은 그대로입니다.\n{e}") from e
                touched = True
                try:
                    for off, n in chunks:
                        a = read_src(n, off)
                        done = 0
                        while done < n:
                            done += _pwrite(d, a[done:], off + done)
                    if os.fstat(d).st_size != size:
                        os.ftruncate(d, size)
                    if pending is None:
                        os.fsync(d)
                    # 쓴 부분을 다시 읽어 확인 (나머지는 쓰기 전에 같다는 것을 확인했다)
                    for off, n in chunks:
                        if _pread(d, n, off) != read_src(n, off):
                            raise SafeWriteError("덮어쓴 내용 확인에 실패했습니다.")
                    if os.fstat(d).st_size != size:
                        raise SafeWriteError("덮어쓴 파일 크기가 다릅니다.")
                    if pending is not None:
                        pending.append(dst)
                    return
                except Exception:
                    pass  # 아래에서 사본으로 교체
            finally:
                os.close(d)
    finally:
        if s is not None:
            os.close(s)
    # 원본을 열 수 없거나 반쯤 쓰였을 수 있으므로 검증된 사본으로 교체한다 — 비교하지 않고 둔 오디오 부분이
    # 사본에서도 같은지 먼저 확인한다 (원본의 그 부분은 쓰지 않았으므로 그대로다)
    if protect and not _same_regions(src, dst, protect):
        msg = ("원본에 쓰다가 실패해 일부만 바뀌었을 수 있습니다. 백업에서 되돌려 주세요." if touched
               else "원본을 다시 쓰지 못했습니다. 원본은 그대로입니다.")
        raise SafeWriteError(msg)
    if not isinstance(src, str):  # 메모리 사본은 이때만 디스크에 내려놓는다
        path = os.path.join(os.path.dirname(dst), f"{STAGE_PREFIX}{uuid.uuid4().hex}{os.path.splitext(dst)[1]}")
        try:
            with open(path, "xb") as f:
                for off in range(0, size, CHUNK):
                    f.write(src.pread(CHUNK, off))
        except OSError as e:
            if os.path.exists(path):
                os.unlink(path)
            msg = ("원본에 쓰다가 실패해 일부만 바뀌었을 수 있습니다. 백업에서 되돌려 주세요." if touched
                   else "원본을 다시 쓰지 못했습니다. 원본은 그대로입니다.")
            raise SafeWriteError(f"{msg}\n{e}") from e
        if _copyfile is not None and _copyfile(os.fsencode(dst), os.fsencode(path), None, COPYFILE_METADATA) == 0:
            os.utime(path)  # 권한·Finder 태그 등은 원본대로, 수정 시각은 지금 (디스크 사본에 쓸 때와 같게)
        src.close()  # 원본을 열어 둔 채로는 교체할 수 없다
        src = path
    try:
        _fsync(src)
        os.replace(src, dst)
        _fsync(os.path.dirname(dst))
    except OSError as e:
        raise StageKeptError(f"원본을 다시 쓰지 못했습니다. 작업 사본이 남아 있습니다: {src}\n{e}") from e


def clean_stale_stages(folder: str):
    """비정상 종료로 남은 작업 사본 정리."""
    try:
        for name in os.listdir(folder):
            if name.startswith(STAGE_PREFIX):
                try:
                    os.unlink(os.path.join(folder, name))
                except OSError:
                    pass
    except OSError:
        pass
