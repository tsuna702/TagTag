"""안전 저장: 사본에 쓰고 검증한 뒤 원본과 바꾼다 (앨범태그 core.py 방식).

- 오디오 데이터(MP3 프레임, FLAC 프레임, M4A mdat)가 바이트 단위로 그대로인지 확인
- 작업 도중 다른 프로그램이 원본을 바꿨으면 교체하지 않음
- iCloud에서 아직 내려받지 않은 파일, 링크, 읽기 전용 파일은 건드리지 않음
"""
from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import uuid

import mutagen

SF_DATALESS = 0x40000000  # iCloud 등에 있고 아직 내려받지 않은 파일
STAGE_PREFIX = ".localtag-"


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
    if not os.access(path, os.W_OK):
        raise SafeWriteError("읽기 전용 파일입니다.")
    return st


def is_dataless(path: str) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def copy_preserving(src: str, dst: str):
    # macOS의 cp -p 는 확장 속성(Finder 태그 등)과 ACL까지 보존한다
    subprocess.run(["/bin/cp", "-p", src, dst], check=True, capture_output=True)


def _fsync(path: str):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def payload_hash(path: str) -> str | None:
    """태그를 뺀 압축 오디오 데이터의 해시. 확인할 수 없는 형식이면 None."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".m4r"):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
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
        for start, n in regions:
            h.update(struct.pack(">Q", n))
            f.seek(start)
            while n:
                b = f.read(min(1024 * 1024, n))
                if not b:
                    return None
                h.update(b)
                n -= len(b)
    return h.hexdigest()


def _length(path):
    try:
        mf = mutagen.File(path)
    except Exception:
        return None
    return None if mf is None else round(float(getattr(mf.info, "length", 0) or 0), 2)


def safe_modify(path: str, fn, enabled: bool = True, keep_mtime: bool = False):
    """fn(작업할_경로)로 태그를 바꾼다. enabled면 사본에서 작업하고 검증 후 교체."""
    st = check_writable(path)
    if not enabled:
        fn(path)
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        return
    folder, ext = os.path.dirname(path), os.path.splitext(path)[1]
    stage = os.path.join(folder, f"{STAGE_PREFIX}{uuid.uuid4().hex}{ext}")
    keep_stage = False
    try:
        copy_preserving(path, stage)
        before_audio = payload_hash(stage)
        before_len = _length(stage)
        fn(stage)
        if _length(stage) is None:
            raise SafeWriteError("저장한 파일을 다시 읽지 못했습니다. 원본은 그대로입니다.")
        if before_audio is not None and payload_hash(stage) != before_audio:
            raise SafeWriteError("오디오 데이터 검증에 실패했습니다. 원본은 그대로입니다.")
        if before_len is not None and abs((_length(stage) or 0) - before_len) > 0.05:
            raise SafeWriteError("재생 시간이 달라져 저장을 취소했습니다. 원본은 그대로입니다.")
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns, now.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            raise SafeWriteError("작업하는 동안 다른 프로그램이 파일을 바꿨습니다. 원본은 그대로입니다.")
        _fsync(stage)
        try:
            write_back(stage, path)
        except StageKeptError:
            keep_stage = True
            raise
        if keep_mtime:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        if not keep_stage and os.path.exists(stage):
            os.unlink(stage)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_back(src: str, dst: str):
    """검증한 사본(src)의 내용을 원본(dst) 파일 안에 다시 쓴다.

    파일을 통째로 바꿔치기(os.replace)하면 내용이 같아도 다른 파일(새 inode)이 되어,
    파일 번호로 곡 위치를 기억하는 Apple Music이 저장 직후 곡을 잠깐 찾지 못한다.
    같은 파일에 덮어쓰면 Music·Finder 별칭·하드 링크가 그대로 유지된다.
    중간에 실패하면 사본으로 원본을 교체해 반쯤 쓰인 파일이 남지 않게 한다.
    """
    h = hashlib.sha256()
    try:
        with open(src, "rb") as s, open(dst, "r+b") as d:
            for block in iter(lambda: s.read(1024 * 1024), b""):
                d.write(block)
                h.update(block)
            d.truncate()
            d.flush()
            os.fsync(d.fileno())
        if _digest(dst) != h.hexdigest():
            raise SafeWriteError("덮어쓴 내용 확인에 실패했습니다.")
    except Exception:
        # 원본이 반쯤 쓰였을 수 있으므로 검증된 사본으로 교체한다 (내용은 올바름)
        try:
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
