"""안전 저장: 오디오 위치가 그대로면 원본의 오디오는 읽지도 쓰지도 않는다 (태그 부분만 비교해 다시 쓴다).

- 위치가 그대로: 오디오 부분은 비교하지 않고 원본 그대로 둔다 → 저장 뒤 오디오가 바이트 단위로 같다
- 태그가 커져 오디오가 밀림: 오디오가 같은지 확인한 뒤 처음 달라진 곳부터 다시 쓴다
- 쓰다가 실패해 사본으로 교체할 때: 건너뛴 오디오 부분이 사본에서도 같은지 먼저 확인한다
- m4a 실행 취소 기록의 커버는 목록의 커버와 메모리를 함께 쓴다 (곡마다 복사하지 않음)

실행: python tests/test_safewrite_skip.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, QApplication, check, finish, samples, window  # noqa: E402

import mutagen  # noqa: E402

from localtag import safewrite, tagio  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")


def audio(path):
    with open(path, "rb") as fp:
        data = fp.read()
    return [data[s:s + n] for s, n in safewrite.payload_regions(path)]


# 1) 위치가 그대로면 오디오 부분은 읽지 않는다 — 형식마다 저장해 보고 오디오가 그대로인지
reads = []
orig_differing = safewrite._differing_chunks


def counting(read_src, dd, size, protect, rewrite_rest=False):
    def rd(n, off):
        reads.append((off, n))
        return read_src(n, off)
    return orig_differing(rd, dd, size, protect, rewrite_rest)


safewrite._differing_chunks = counting
for name in ("01 곡 1.mp3", "02 곡 2.flac", "03 곡 3.m4a"):
    p = os.path.join(album, name)
    before, ino = audio(p), os.stat(p).st_ino
    f = tagio.AudioFile.load(p, tagio.Options())
    reads.clear()
    f.save({"comment": ["짧은 코멘트"]}, None, tagio.Options())
    regions = safewrite.payload_regions(p)
    inside = [(o, n) for o, n in reads if any(s <= o < s + k for s, k in regions)]
    check(not inside, f"{name}: 오디오 부분은 읽지 않음 (읽은 조각 {len(reads)}개, 오디오 안 {len(inside)}개)")
    check(audio(p) == before and os.stat(p).st_ino == ino, f"{name}: 오디오 그대로 · 파일 번호 그대로")
    check(tagio.AudioFile.load(p, tagio.Options()).get("comment") == "짧은 코멘트", f"{name}: 태그 저장")
safewrite._differing_chunks = orig_differing

# 2) 태그가 패딩보다 커져 오디오가 뒤로 밀리면: 오디오가 같은지 확인한 뒤 다시 쓴다
p = os.path.join(album, "01 곡 1.mp3")
before, start = audio(p), safewrite.payload_regions(p)[0][0]
f = tagio.AudioFile.load(p, tagio.Options())
f.save({"comment": ["긴 코멘트 " * 40000]}, None, tagio.Options())
check(safewrite.payload_regions(p)[0][0] > start, "태그가 커져 오디오가 뒤로 밀림")
check(audio(p) == before, "밀린 오디오도 바이트 단위로 그대로")
check(len(tagio.AudioFile.load(p, tagio.Options()).get("comment")) > 100000, "긴 코멘트 저장")
mf = mutagen.File(p)
check(mf is not None and mf.info.length > 0, f"다시 읽히는 파일 ({mf.info.length:.1f}초)")

# 3) write_back: protect 부분은 원본 그대로 두고 그 밖만 쓴다 (사본에서 오디오가 달라졌어도 원본 오디오가 남는다)
src, dst = os.path.join(RUN, "wb-src.bin"), os.path.join(RUN, "wb-dst.bin")
body = bytes(range(256)) * 12000  # 약 3 MB (조각 3개 넘게)
with open(dst, "wb") as fp:
    fp.write(b"HEAD" * 256 + body + b"TAIL")
with open(src, "wb") as fp:
    fp.write(b"NEWH" * 256 + body[:100] + b"X" + body[101:] + b"END!")
protect = [(1024, len(body))]
safewrite.write_back(src, dst, protect)
with open(dst, "rb") as fp:
    got = fp.read()
check(got == b"NEWH" * 256 + body + b"END!", "protect 밖(앞·뒤 태그)만 다시 쓰고 오디오는 원본 그대로")

# 4) rewrite_rest: 처음 달라진 곳부터 끝까지 모두 다시 쓴다 (내용이 밀린 경우)
with open(dst, "wb") as fp:
    fp.write(b"A" * 100 + body)
with open(src, "wb") as fp:
    fp.write(b"A" * 100 + b"B" * 5000 + body)
safewrite.write_back(src, dst, None, rewrite_rest=True)
with open(dst, "rb") as fp:
    check(fp.read() == b"A" * 100 + b"B" * 5000 + body, "밀린 내용 다시 쓰기")

# 5) 쓰다가 실패하면 사본으로 교체 — 그 전에 건너뛴 오디오 부분이 사본에서도 같은지 확인
orig_pwrite = safewrite._pwrite


def broken(fd, data, off):
    raise OSError(5, "쓰기 실패 (시험)")


with open(dst, "wb") as fp:
    fp.write(b"HEAD" * 256 + body)
with open(src, "wb") as fp:
    fp.write(b"NEWH" * 256 + body)
safewrite._pwrite = broken
try:
    safewrite.write_back(src, dst, protect)
    with open(dst, "rb") as fp:
        check(fp.read() == b"NEWH" * 256 + body, "쓰기 실패 → 오디오가 같은 사본으로 교체")
finally:
    safewrite._pwrite = orig_pwrite
with open(dst, "wb") as fp:
    fp.write(b"HEAD" * 256 + body)
shutil.copyfile(dst, src)
with open(src, "r+b") as fp:
    fp.seek(0)
    fp.write(b"NEWH")
    fp.seek(5000)
    fp.write(b"Z")  # 사본의 오디오 부분이 다르다
safewrite._pwrite = broken
try:
    safewrite.write_back(src, dst, protect)
    check(False, "오디오가 다른 사본으로 교체하면 안 됨")
except safewrite.SafeWriteError as e:
    check("백업" in str(e), f"오디오가 다르면 교체하지 않고 알림: {e}")
finally:
    safewrite._pwrite = orig_pwrite
with open(dst, "rb") as fp:
    check(fp.read(5000)[1024:] == body[:3976], "원본 오디오 부분은 그대로")

# 6) m4a 실행 취소 기록: 커버를 목록의 커버와 함께 쓰고, 메모리 크기에 센다 · 되돌리면 커버도 그대로
w = window()
w.backup_enabled = False
w.load_paths([album], replace=True)
app.processEvents()
m4a = next(f for f in w.model.files if f.ext == ".m4a")
jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(range(256)) * 40  # 1 KB 넘는 커버만 함께 쓴다 (샘플 커버는 765바이트)
w.apply_plans([Plan(m4a, pictures=[tagio.Picture(jpg, "image/jpeg", 3, "")])], "커버 넣기")
app.processEvents()
m4a = next(f for f in w.model.files if f.ext == ".m4a")
cover = m4a.pictures[0].data
check(cover == jpg, "커버 넣기")
for k in range(3):
    w.apply_plans([Plan(f, changes={"comment": [f"c{k}"]}) for f in w.model.files], f"c{k}")
    app.processEvents()
m4a = next(f for f in w.model.files if f.ext == ".m4a")
snaps = [i.snapshot for e in w.undo_stack[-3:] for i in e.items if i.snapshot and i.snapshot.kind == "mp4"]
covers = [c.data for s in snaps for c in s.data.get("covr", [])]
check(len(covers) == 3 and all(c is m4a.pictures[0].data for c in covers),
      f"실행 취소 기록의 커버가 목록의 커버와 같은 메모리 ({len(covers)}개)")
check(all(s.size_estimate() >= len(cover) for s in snaps), "m4a 기록의 크기에 커버를 셈")
check(sum(e.size_estimate(seen := set()) for e in w.undo_stack[-3:]) < len(cover) * 2 + 65536,
      "여러 기록이 함께 쓰는 커버는 한 번만 셈")
for _ in range(3):
    w.undo()
    app.processEvents()
back = tagio.AudioFile.load(m4a.path, w.opts)
check(back.pictures and back.pictures[0].data == cover, "되돌리면 커버도 그대로")
check(isinstance(mutagen.File(back.path).tags["covr"][0], mutagen.mp4.MP4Cover), "되돌린 커버의 형식 (MP4Cover)")
finish()
