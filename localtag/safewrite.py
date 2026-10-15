"""안전 저장: 사본에 쓰고 검증한 뒤 원본에 반영한다 (앨범태그 core.py 방식).

- 사본은 같은 APFS 디스크면 복제본(clone)으로 만들어 즉시 끝난다
  (Windows 는 메모리에 둔다 — 디스크에 새로 만든 파일은 열 때마다 백신 검사로 느려진다)
- 오디오 데이터(MP3 프레임, FLAC 프레임, M4A mdat)가 바이트 단위로 그대로인지 확인
- 원본에는 달라진 부분만 다시 쓴다 (파일 번호 유지, 오디오 데이터는 다시 쓰지 않음)
- 작업 도중 다른 프로그램이 원본을 바꿨으면 교체하지 않음
- iCloud에서 아직 내려받지 않은 파일, 링크, 읽기 전용 파일은 건드리지 않음
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import os
import shutil
import stat
import struct
import subprocess
import sys
import uuid

import mutagen

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

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

# Windows 는 작업 사본을 메모리에 둔다: 디스크에 새로 만든 파일은 열 때마다 백신(Defender)이 검사해
# 곡마다 몇 ms 씩 든다 (300곡 저장 6.6초 중 약 3초). 이보다 큰 파일은 예전처럼 디스크 사본.
MEM_STAGE = IS_WIN
MEM_STAGE_LIMIT = 256 * 1024 * 1024


class MemStage(io.BytesIO):
    """메모리에 둔 작업 사본. name 은 원본 경로 (확장자로 형식을 가린다)."""

    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


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


CHUNK = 1024 * 1024

# copyfile(3): cp -p 처럼 확장 속성(Finder 태그 등)·ACL·시각까지 복사한다.
# COPYFILE_CLONE 이면 APFS에서는 데이터를 복사하지 않는 복제본(clone)을 만들어 즉시 끝나고 공간도 쓰지 않는다
# (어느 한쪽을 고치면 그 부분만 새로 저장되므로 원본과 사본은 서로 영향을 주지 않는다).
COPYFILE_ALL = 0xF
COPYFILE_CLONE = 1 << 24
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
    subprocess.run(["/bin/cp", "-p", src, dst], check=True, capture_output=True)


def _fsync(path: str):
    if IS_WIN and os.path.isdir(path):
        return  # Windows 는 폴더를 열어 fsync 할 수 없다 (NTFS 가 이름 바꾸기를 기록해 둔다)
    fd = os.open(path, os.O_RDONLY | O_BINARY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    if getattr(e, "winerror", 0) == 32:
        return IN_USE
    return str(e)


def safe_modify(path: str, fn, enabled: bool = True, keep_mtime: bool = False, inspect=None):
    """fn(작업할_경로)로 태그를 바꾼다. enabled면 사본에서 작업하고 검증 후 원본에 반영.

    작업 사본은 디스크 파일 경로이거나 (Windows) 메모리 사본(MemStage)이다 — fn·inspect 는
    mutagen 에 넘기기 전에 rewind() 를 거친다.
    fn 이 바꾸기 전 재생 시간(초)을 돌려주면 그것을 쓰고, 아니면 따로 읽는다.
    inspect(경로) 가 있으면 바뀐 파일을 읽은 결과(.length 가 있는 객체)를 돌려준다 —
    검증에 쓴 것을 그대로 돌려주므로 저장 뒤 파일을 다시 읽지 않아도 된다.
    """
    st = check_writable(path)
    if not enabled:
        fn(path)
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return inspect(path) if inspect else None
    folder, ext = os.path.dirname(path), os.path.splitext(path)[1]
    stage = os.path.join(folder, f"{STAGE_PREFIX}{uuid.uuid4().hex}{ext}")
    keep_stage = False
    result = None
    try:
        try:
            if MEM_STAGE and st.st_size <= MEM_STAGE_LIMIT:
                with open(path, "rb") as f:
                    stage = MemStage(f.read(), path)
            else:
                copy_preserving(path, stage)
        except PermissionError as e:
            if IS_WIN and getattr(e, "winerror", 0) == 32:  # 공유 위반: 다른 프로그램이 열어 둠
                raise SafeWriteError(IN_USE) from e
            raise
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
        protect = None
        if ra is not None:
            if rb is None or [n for _, n in ra] != [n for _, n in rb]:
                raise SafeWriteError(AUDIO_CHANGED)
            if ra == rb:
                protect = ra
            elif same_payload(path, stage) is not True:
                raise SafeWriteError(AUDIO_CHANGED)
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            raise SafeWriteError("작업하는 동안 다른 프로그램이 파일을 바꿨습니다. 원본은 그대로입니다.")
        try:
            write_back(stage, path, protect)
        except StageKeptError:
            keep_stage = True
            raise
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        if isinstance(stage, str) and not keep_stage and os.path.exists(stage):
            os.unlink(stage)
    return result


def _differing_chunks(read_src, d, size, protect):
    """같은 위치끼리 비교해 다른 조각 [(위치, 길이)]. protect(오디오 위치) 안이 다르면 SafeWriteError."""
    chunks = []
    off = 0
    while off < size:
        a = read_src(min(CHUNK, size - off), off)
        if not a:
            raise SafeWriteError("작업 사본을 읽지 못했습니다. 원본은 그대로입니다.")
        b = _pread(d, len(a), off)
        if a != b:
            end = off + len(a)
            for start, n in protect or ():
                lo, hi = max(off, start), min(end, start + n)
                if lo < hi and a[lo - off:hi - off] != b[lo - off:hi - off]:
                    raise SafeWriteError(AUDIO_CHANGED)
            chunks.append((off, len(a)))
        off += len(a)
    return chunks


def write_back(src, dst: str, protect=None):
    """검증한 사본(src: 경로 또는 메모리 사본)의 내용을 원본(dst) 파일 안에 다시 쓴다.

    파일을 통째로 바꿔치기(os.replace)하면 내용이 같아도 다른 파일(새 inode)이 되어,
    파일 번호로 곡 위치를 기억하는 Apple Music이 저장 직후 곡을 잠깐 찾지 못한다.
    같은 파일에 덮어쓰면 Music·Finder 별칭·하드 링크가 그대로 유지된다.
    먼저 비교해 달라진 부분(보통 앞쪽의 태그)만 쓰므로 오디오 데이터는 다시 쓰지 않는다.
    protect: 바뀌면 안 되는 위치 [(시작, 길이)] — 비교할 때 다르면 아무것도 쓰지 않고 SafeWriteError.
    쓰는 도중에 실패하면 사본으로 원본을 교체해 반쯤 쓰인 파일이 남지 않게 한다.
    """
    if isinstance(src, str):
        s = os.open(src, os.O_RDONLY | O_BINARY)
        size = os.fstat(s).st_size

        def read_src(n, off):
            return _pread(s, n, off)
    else:
        s = None
        data = src.getvalue()
        size = len(data)

        def read_src(n, off):
            return data[off:off + n]
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
                    chunks = _differing_chunks(read_src, d, size, protect)
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
                    os.fsync(d)
                    # 쓴 부분을 다시 읽어 확인 (나머지는 쓰기 전에 같다는 것을 확인했다)
                    for off, n in chunks:
                        if _pread(d, n, off) != read_src(n, off):
                            raise SafeWriteError("덮어쓴 내용 확인에 실패했습니다.")
                    if os.fstat(d).st_size != size:
                        raise SafeWriteError("덮어쓴 파일 크기가 다릅니다.")
                    return
                except Exception:
                    pass  # 아래에서 사본으로 교체
            finally:
                os.close(d)
    finally:
        if s is not None:
            os.close(s)
    # 원본을 열 수 없거나 반쯤 쓰였을 수 있으므로 검증된 사본으로 교체한다 (내용은 올바름)
    if not isinstance(src, str):  # 메모리 사본은 이때만 디스크에 내려놓는다
        path = os.path.join(os.path.dirname(dst), f"{STAGE_PREFIX}{uuid.uuid4().hex}{os.path.splitext(dst)[1]}")
        try:
            with open(path, "xb") as f:
                f.write(data)
        except OSError as e:
            if os.path.exists(path):
                os.unlink(path)
            msg = ("원본에 쓰다가 실패해 일부만 바뀌었을 수 있습니다. 백업에서 되돌려 주세요." if touched
                   else "원본을 다시 쓰지 못했습니다. 원본은 그대로입니다.")
            raise SafeWriteError(f"{msg}\n{e}") from e
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
