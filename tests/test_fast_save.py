"""속도 개선이 안전을 해치지 않는지 (1.1.15).

- 작업 사본(MemStage): 무작위로 읽고 쓰고 잘라도 실제 파일과 똑같이 동작 · mutagen 결과가 파일에 직접 쓴 것과 바이트 단위로 같음
- 큰 파일도 태그 부분만 읽음 · 쓴 양이 너무 커지면 디스크 사본으로 다시
- 묶음 저장(IoBatch): fsync 를 미뤘다가 한꺼번에 · 수정 시각 그대로 · 실패는 알림
- 태그만 보관하는 백업: 되돌리면 파일이 바이트 단위로 원래대로 · 오디오를 옮기는 저장은 원본 전체를 보관 · 뒤에 오디오가 밀려도 되돌림
- 남는 자리(패딩): 태그가 커지면 한 번만 오디오를 옮기고, 되돌릴 때는 옮기지 않음 · 커버를 줄이면 파일도 줄어듦
- 실행 취소: 파일이 그대로면 다시 읽지 않음
- ID3 프레임 읽기(복사 안 함), 형식 고르기, 표 모델, 글꼴, 미리 불러오기

실행: python tests/test_fast_save.py
"""
import hashlib
import io  # noqa: F401
import os
import random
import shutil
import struct
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, QApplication, check, finish, samples, window  # noqa: E402

import mutagen  # noqa: E402
from mutagen.id3 import APIC, CHAP, COMM, CTOC, ID3, TIT2, TPE1, CTOCFlags, ID3Tags  # noqa: E402
from PySide6.QtCore import QModelIndex, Qt  # noqa: E402

from localtag import backup, mainwindow, model, safewrite, tagio  # noqa: E402
from localtag.plan import Plan  # noqa: E402
from localtag.safewrite import MemStage, rewind  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
opts = tagio.Options()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def audio(path):
    with open(path, "rb") as f:
        data = f.read()
    return [data[s:s + n] for s, n in safewrite.payload_regions(path)]


def moov_first(path):
    """mdat 뒤의 moov 를 앞으로 옮긴다 (Apple Music·iTunes 파일의 배치). 오디오 위치 표(stco)도 고친다."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    atoms, pos = {}, 0
    while pos < len(data):
        n, name = struct.unpack(">I4s", data[pos:pos + 8])
        atoms[name] = (pos, n)
        pos += n
    if atoms[b"moov"][0] < atoms[b"mdat"][0]:
        return
    start, n = atoms[b"moov"]
    moov = bytearray(data[start:start + n])
    i = 0
    while True:  # moov 안의 stco 를 모두 고친다
        i = moov.find(b"stco", i)
        if i < 0:
            break
        count = struct.unpack(">I", moov[i + 8:i + 12])[0]
        for k in range(count):
            at = i + 12 + 4 * k
            moov[at:at + 4] = struct.pack(">I", struct.unpack(">I", moov[at:at + 4])[0] + n)
        i += 8
    mstart, mlen = atoms[b"mdat"]
    new = data[:mstart] + moov + data[mstart:mstart + mlen] + data[start + n:]
    with open(path, "wb") as f:
        f.write(new)


def fresh(name, src=None):
    """샘플을 새 이름으로 복사해 경로를 돌려준다."""
    src = src or next(os.path.join(root, x) for root, _, fs in os.walk(d) for x in fs if x.endswith(name))
    folder = os.path.join(RUN, "work")
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, f"{len(os.listdir(folder)):03d}{os.path.splitext(src)[1]}")
    shutil.copy2(src, p)
    return p


# ---- 1) 작업 사본이 실제 파일과 똑같이 동작한다
def fuzz(seed, base_size, stage_of, ops=1500):
    rnd = random.Random(seed)
    data = rnd.randbytes(base_size)
    mirror_path = os.path.join(RUN, f"mirror-{seed}.bin")
    with open(mirror_path, "wb") as f:
        f.write(data)
    stage = stage_of(data, seed)
    with open(mirror_path, "r+b") as m:
        for k in range(ops):
            op = rnd.random()
            if op < 0.35:
                pos = rnd.randrange(0, base_size + 3 * safewrite.PAGE)
                assert stage.seek(pos) == m.seek(pos)
            elif op < 0.42:
                off = rnd.randrange(-5000, 5000)
                whence = rnd.choice((1, 2))
                try:
                    want = m.seek(off, whence)
                except OSError:
                    want = None
                try:
                    got = stage.seek(off, whence)
                except OSError:
                    got = None
                if want != got:
                    return f"seek({off},{whence}) {got} != {want} (연산 {k})"
                if want is None:  # 실패했으면 위치는 그대로
                    continue
            elif op < 0.72:
                n = rnd.choice((0, 1, 10, 100, 4096, safewrite.PAGE - 1, safewrite.PAGE, safewrite.PAGE + 1, 300000, -1))
                a, b = stage.read(n), m.read(n)
                if a != b or type(a) is not bytes:
                    return f"read({n}) 다름 (연산 {k}, 위치 {stage.tell()}, {len(a)} != {len(b)})"
            elif op < 0.95:
                n = rnd.choice((1, 10, 100, 4096, safewrite.PAGE - 1, safewrite.PAGE, safewrite.PAGE + 1, 2 * safewrite.PAGE, 300000))
                b = rnd.randbytes(n)
                stage.write(b)
                m.write(b)
            else:
                size = rnd.randrange(0, base_size + 2 * safewrite.PAGE)
                stage.truncate(size)
                m.truncate(size)
            if stage.tell() != m.tell():
                return f"위치 다름 {stage.tell()} != {m.tell()} (연산 {k})"
            if stage.length() != os.fstat(m.fileno()).st_size and (m.flush() or stage.length() != os.fstat(m.fileno()).st_size):
                return f"크기 다름 {stage.length()} (연산 {k})"
        m.flush()
        m.seek(0)
        whole = m.read()
    stage.seek(0)
    got = stage.read()
    if hasattr(stage, "close"):
        stage.close()
    return None if got == whole else f"최종 내용 다름 ({len(got)} != {len(whole)})"


def over_file(data, seed):
    p = os.path.join(RUN, f"base-{seed}.bin")
    with open(p, "wb") as f:
        f.write(data)
    return MemStage.over(p)


for seed, size in ((1, 0), (2, 100), (3, safewrite.PAGE), (4, 700_000), (5, 3 * safewrite.PAGE + 17), (6, 2_000_000)):
    check(fuzz(seed, size, over_file) is None, f"무작위 조작 — 파일 위 사본 {size} 바이트: {fuzz(seed + 100, size, over_file)}")
    check(fuzz(seed + 200, size, lambda data, _s: MemStage(data, "x.bin")) is None, f"무작위 조작 — 내용 사본 {size} 바이트")

base = os.path.join(RUN, "untouched.bin")
with open(base, "wb") as f:
    f.write(b"A" * 500_000)
stage = MemStage.over(base)
stage.seek(1000)
stage.write(b"B" * 200_000)
stage.truncate(250_000)
check(stage.pread(3, 999) == b"ABB", "쓴 곳과 안 쓴 곳이 이어져 읽힘")
stage.close()
check(os.path.getsize(base) == 500_000 and sha(base) == hashlib.sha256(b"A" * 500_000).hexdigest(), "작업 사본에 써도 원본은 그대로")
os.rename(base, base + ".moved")  # 닫으면 이름을 바꿀 수 있다 (Windows)
os.unlink(base + ".moved")

# ---- 2) mutagen 결과가 파일에 직접 쓴 것과 바이트 단위로 같다
big_cover = b"\xff\xd8\xff\xe0" + bytes(random.Random(7).randrange(256) for _ in range(120_000))
EDITS = {"작은 변경": ({"comment": ["짧은 코멘트"]}, None),
         "긴 코멘트(태그 커짐)": ({"comment": ["아주 긴 코멘트 " * 20000]}, None),
         "커버(태그 커짐)": ({}, [tagio.Picture(big_cover, "image/jpeg", 3, "")]),
         "지우기": ({"title": [], "artist": []}, None)}
for suffix, rearrange in ((".mp3", False), (".flac", False), (".m4a", False), (".m4a", True), (".ogg", False), (".wma", False)):
    for name, (changes, pics) in EDITS.items():
        direct, staged = fresh(suffix), fresh(suffix)
        if rearrange:
            moov_first(direct)
            moov_first(staged)
            name += " (moov 앞)"
        tagio.AudioFile.load(direct, opts).save(changes, pics, tagio.Options(safe_save=False))
        tagio.AudioFile.load(staged, opts).save(changes, pics, opts)
        check(sha(direct) == sha(staged), f"{suffix} {name}: 메모리 사본을 거친 결과가 파일에 직접 쓴 것과 같음")
        check(not [x for x in os.listdir(os.path.dirname(staged)) if x.startswith(safewrite.STAGE_PREFIX)], f"{suffix} {name}: 작업 사본 안 남음")
        os.unlink(direct)
        os.unlink(staged)

# 샘플에 없는 형식 · 태그 제거 · ID3v1 도 같다
wav = os.path.join(RUN, "만든 곡.wav")
with wave.open(wav, "wb") as wf:
    wf.setnchannels(2)
    wf.setsampwidth(2)
    wf.setframerate(44100)
    wf.writeframes(random.Random(8).randbytes(44100 * 4))
pcm = random.Random(9).randbytes(44100 * 4)
aiff = os.path.join(RUN, "만든 곡.aiff")
with open(aiff, "wb") as f:  # COMM + SSND
    comm = struct.pack(">hIh", 2, 44100, 16) + bytes.fromhex("400EAC44000000000000")
    ssnd = struct.pack(">II", 0, 0) + pcm
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    f.write(b"FORM" + struct.pack(">I", len(body)) + body)
for made in (wav, aiff):
    for name, (changes, pics) in EDITS.items():
        direct, staged = fresh(os.path.splitext(made)[1], made), fresh(os.path.splitext(made)[1], made)
        tagio.AudioFile.load(direct, opts).save(changes, pics, tagio.Options(safe_save=False))
        tagio.AudioFile.load(staged, opts).save(changes, pics, opts)
        check(sha(direct) == sha(staged) and tagio.AudioFile.load(staged, opts).length > 0.5, f"{os.path.splitext(made)[1]} {name}: 파일에 직접 쓴 것과 같음")
        os.unlink(direct)
        os.unlink(staged)
for suffix in (".mp3", ".flac", ".m4a", ".ogg", ".wma"):
    direct, staged = fresh(suffix), fresh(suffix)
    tagio.AudioFile.load(direct, opts).remove_tags(tagio.Options(safe_save=False))
    tagio.AudioFile.load(staged, opts).remove_tags(opts)
    check(sha(direct) == sha(staged), f"{suffix} 태그 제거: 파일에 직접 쓴 것과 같음")
    os.unlink(direct)
    os.unlink(staged)
direct, staged = fresh(".mp3"), fresh(".mp3")
tagio.AudioFile.load(direct, opts).save({"title": ["v1"]}, None, tagio.Options(safe_save=False, write_id3v1=True))
tagio.AudioFile.load(staged, opts).save({"title": ["v1"]}, None, tagio.Options(write_id3v1=True))
check(sha(direct) == sha(staged) and "ID3v1" in tagio.AudioFile.load(staged, opts).tag_type, "ID3v1 함께 쓰기: 파일에 직접 쓴 것과 같음")
os.unlink(direct)
os.unlink(staged)

# ---- 3) 큰 파일도 태그 부분만 읽는다
src = fresh(".mp3")
regions = safewrite.payload_regions(src)
with open(src, "rb") as f:
    raw = f.read()
start, n = regions[0]
bigger = os.path.join(RUN, "big.mp3")
with open(bigger, "wb") as f:  # 곡 하나를 이어 붙여 40MB 쯤으로
    f.write(raw[:start])
    for _ in range(40_000_000 // n + 1):
        f.write(raw[start:start + n])
size_before, audio_before = os.path.getsize(bigger), hashlib.sha256(audio(bigger)[0]).hexdigest()
ino = os.stat(bigger).st_ino
if safewrite.MEM_STAGE:
    read = [0]
    orig_pread = safewrite._pread

    def counting(fd, k, off):
        got = orig_pread(fd, k, off)
        read[0] += len(got)
        return got

    safewrite._pread = counting
    tagio.AudioFile.load(bigger, opts).save({"comment": ["큰 파일"]}, None, opts)
    safewrite._pread = orig_pread
    check(read[0] < 2_000_000, f"{size_before // 1024 ** 2} MB 파일에서 {read[0] // 1024} KB 만 읽음")
else:
    tagio.AudioFile.load(bigger, opts).save({"comment": ["큰 파일"]}, None, opts)
check(hashlib.sha256(audio(bigger)[0]).hexdigest() == audio_before and os.stat(bigger).st_ino == ino, "큰 파일: 오디오·파일 번호 그대로")
check(tagio.AudioFile.load(bigger, opts).get("comment") == "큰 파일", "큰 파일: 저장됨")

if safewrite.MEM_STAGE:  # 쓴 양이 한도를 넘으면 디스크 사본으로 다시 한다 (태그가 커져 아주 큰 파일의 오디오를 옮길 때)
    limit = safewrite.MEM_STAGE_LIMIT
    copies = []
    orig_copy = safewrite.copy_preserving
    safewrite.copy_preserving = lambda s, t: (copies.append(t), orig_copy(s, t))[1]
    safewrite.MEM_STAGE_LIMIT = 1024 * 1024
    held = []
    af = tagio.AudioFile.load(bigger, opts).save({"comment": ["자리보다 긴 코멘트 " * 60000]}, None, opts, snapshot=held)
    safewrite.MEM_STAGE_LIMIT = limit
    safewrite.copy_preserving = orig_copy
    check(len(copies) == 1 and copies[0].endswith(".mp3"), f"한도를 넘으면 디스크 사본 {copies}")
    check(len(af.get("comment")) > 100_000 and hashlib.sha256(audio(bigger)[0]).hexdigest() == audio_before, "디스크 사본으로 저장 · 오디오 그대로")
    check(not [x for x in os.listdir(RUN) if x.startswith(safewrite.STAGE_PREFIX)], "디스크 사본도 안 남음")
    check(held and held[0].kind == "id3", "실행 취소 기록도 있음")
os.unlink(bigger)

# ---- 4) 묶음 저장: fsync 를 미뤘다가 한꺼번에
if safewrite.IS_WIN:
    syncs = []
    orig_fsync = safewrite.os.fsync
    paths = [fresh(".mp3") for _ in range(5)]
    files = [tagio.AudioFile.load(p, opts) for p in paths]
    safewrite.os.fsync = lambda fd: (syncs.append(fd), orig_fsync(fd))[1]
    with safewrite.IoBatch() as batch:
        batch.next(paths)
        saved = [f.save({"comment": ["묶음"]}, None, opts) for f in files]
        check(not syncs, "with 안에서는 fsync 를 미룸")
        stamps = {f.path: (f.size, os.stat(f.path).st_mtime_ns, f.ino) for f in saved}
        moved_to = paths[0] + ".renamed.mp3"
        os.rename(paths[0], moved_to)
        batch.moved(paths[0], moved_to)
        batch.next([])
        check(len(syncs) == 5, f"묶음이 끝나면 한꺼번에 확정 ({len(syncs)})")
        syncs.clear()
    check(not syncs, "남은 것이 없으면 더 하지 않음")
    check(not batch.errors, f"오류 없음 {batch.errors}")
    now = {p: (os.path.getsize(p), os.stat(p).st_mtime_ns, os.stat(p).st_ino) for p in paths[1:]}
    check(all(now[p] == stamps[p] for p in paths[1:]), "확정해도 수정 시각·크기·파일 번호 그대로")
    with safewrite.IoBatch():
        tagio.AudioFile.load(paths[1], opts).save({"comment": ["나갈 때"]}, None, opts)
        check(not syncs, "with 안")
    check(len(syncs) == 1, "with 를 나갈 때 나머지를 확정")
    safewrite.os.fsync = orig_fsync
    orig_f = safewrite._fsync
    safewrite._fsync = lambda p: (_ for _ in ()).throw(OSError(5, "시험 오류"))
    with safewrite.IoBatch() as batch:
        tagio.AudioFile.load(paths[2], opts).save({"comment": ["실패"]}, None, opts)
    check(len(batch.errors) == 1 and batch.errors[0][0] == paths[2], f"확정 실패는 알림 {batch.errors}")
    safewrite._fsync = lambda p: (_ for _ in ()).throw(PermissionError(13, "다른 프로그램"))
    with safewrite.IoBatch() as batch:
        tagio.AudioFile.load(paths[3], opts).save({"comment": ["공유"]}, None, opts)
    check(not batch.errors, "다른 프로그램이 잡고 있으면 넘어감 (쓴 내용은 이미 시스템에 있다)")
    safewrite._fsync = orig_f
    safewrite.prewarm(paths[1:] + [os.path.join(RUN, "없는 파일.mp3")])  # 없는 파일이 있어도 괜찮다
    os.rename(paths[1], paths[1] + ".x")  # 미리 열기가 끝나면 열려 있는 파일이 없다
    os.rename(paths[1] + ".x", paths[1])
    check(True, "미리 열기 뒤 이름 바꾸기 됨")

check(safewrite.error_text(PermissionError(13, "거부")) == (safewrite.IN_USE if safewrite.IS_WIN else "[Errno 13] 거부"), "파일을 잡고 있음 → 알기 쉬운 말")
check(safewrite.error_text(ValueError("다른 오류")) == "다른 오류", "다른 오류는 그대로")


# ---- 5) 백업: 되돌리면 바이트 단위로 원래대로 (태그만 보관 · 통째로 보관 모두)
def run_backup_checks(mode):
    w = window()
    work = os.path.join(RUN, f"backup-{mode}")
    os.makedirs(work)
    srcs = {x: fresh(x) for x in (".mp3", ".flac", ".m4a", ".ogg")}
    paths = {x: os.path.join(work, "곡" + x) for x in srcs}
    for x, p in paths.items():
        shutil.move(srcs[x], p)
    before = {p: sha(p) for p in paths.values()}
    inos = {p: os.stat(p).st_ino for p in paths.values()}
    w.load_paths([work], replace=True)
    app.processEvents()

    def latest():
        return w.backups.jobs()[0]

    w.apply_plans([Plan(f, changes={"comment": ["백업 시험"]}) for f in w.model.files], "짧은 변경")
    app.processEvents()
    job = latest()
    delta = [e for e in job["entries"] if e.get("delta")]
    if backup.TAGS_ONLY:
        check(len(delta) == 3 and all(e["backup"] == backup.PACK_NAME for e in delta), f"{mode}: MP3·FLAC·M4A 는 태그만 보관 ({len(delta)})")
        check(job["bytes"] < sum(os.path.getsize(p) for p in paths.values() if not p.endswith(".ogg")) + os.path.getsize(paths[".ogg"]), f"{mode}: 백업이 통째로 복사보다 작음")
        check(not [e for e in job["entries"] if e["before"].endswith(".ogg") and e.get("delta")], f"{mode}: 오디오 위치를 모르는 OGG 는 통째로")
    else:
        check(not delta, f"{mode}: 통째로 보관")
    check(not w.backups.check(job), f"{mode}: 그대로면 바뀐 파일 없음")
    done, errors = w.backups.restore(job)
    check(len(done) == 4 and not errors, f"{mode}: 모두 되돌림 {errors}")
    check({p: sha(p) for p in paths.values()} == before, f"{mode}: 바이트 단위로 원래대로")
    check({p: os.stat(p).st_ino for p in paths.values()} == inos, f"{mode}: 파일 번호 그대로")
    w.after_backup_restore(done)
    app.processEvents()

    # 태그가 커져 오디오를 옮기는 저장 → 원본 전체를 보관하고 되돌림
    w.apply_plans([Plan(f, pictures=[tagio.Picture(big_cover, "image/jpeg", 3, "")]) for f in w.model.files], "커버")
    app.processEvents()
    job = latest()
    moved_ok = [e for e in job["entries"] if not e.get("delta")]
    check(len(moved_ok) >= 3, f"{mode}: 오디오를 옮긴 곡은 원본 전체를 보관 ({len(moved_ok)})")
    check(all(os.path.exists(os.path.join(job["folder"], e["backup"])) for e in moved_ok), f"{mode}: 보관한 파일이 있음")
    for e in moved_ok:
        if e["before"].endswith((".mp3", ".flac")):
            kept = os.path.join(job["folder"], e["backup"])
            check(sha(kept) == before[e["before"]], f"{mode}: 보관한 {os.path.splitext(kept)[1]} 가 원본과 같음")
    done, errors = w.backups.restore(job)
    check(not errors and {p: sha(p) for p in paths.values()} == before, f"{mode}: 오디오를 옮긴 저장도 바이트 단위로 되돌림 {errors}")
    w.after_backup_restore(done)
    app.processEvents()

    # 이름도 바꾼 저장
    f = next(x for x in w.model.files if x.ext == ".mp3")
    new_name = os.path.join(work, "새 이름.mp3")
    w.apply_plans([Plan(f, changes={"title": ["이름도 바꿈"]}, new_path=new_name)], "이름")
    app.processEvents()
    job = latest()
    done, errors = w.backups.restore(job)
    check(not errors and os.path.exists(paths[".mp3"]) and not os.path.exists(new_name) and sha(paths[".mp3"]) == before[paths[".mp3"]],
          f"{mode}: 이름을 바꾼 저장도 되돌림 {errors}")
    w.after_backup_restore(done)
    app.processEvents()

    # 앞 작업(짧은 변경)을 나중에 되돌리기: 뒤 작업이 오디오를 밀었어도 원래대로
    w.apply_plans([Plan(f, changes={"comment": ["첫 작업"]}) for f in w.model.files if f.ext != ".ogg"], "첫 작업")
    app.processEvents()
    first = latest()
    w.apply_plans([Plan(f, changes={"comment": ["자리보다 긴 코멘트 " * 60000]}) for f in w.model.files if f.ext != ".ogg"], "둘째 작업")
    app.processEvents()
    check(w.backups.check(first), f"{mode}: 뒤에 바뀌었으면 알림")
    done, errors = w.backups.restore(first)
    check(errors and all("다시 바뀐" in e for e in errors), f"{mode}: 묻지 않으면 건너뜀 {errors[:1]}")
    first = next(j for j in w.backups.jobs() if j["id"] == first["id"])
    done, errors = w.backups.restore(first, force=True)
    now_same = {p: sha(p) == before[p] for p in paths.values() if not p.endswith(".ogg")}
    check(not errors and all(now_same.values()), f"{mode}: 덮어쓰고 되돌리면 오디오가 밀렸어도 원래대로 {errors} {now_same}")
    check(not [x for x in os.listdir(work) if x.startswith(".localtag")], f"{mode}: 임시 파일 안 남음")
    w.after_backup_restore(done)
    app.processEvents()

    # 오디오가 달라진 파일: 태그만 보관한 백업으로는 되돌릴 수 없고 파일은 그대로
    if backup.TAGS_ONLY:
        p = paths[".flac"]
        f = next(x for x in w.model.files if x.path == p)
        w.apply_plans([Plan(f, changes={"comment": ["오디오 시험"]})], "오디오 시험")
        app.processEvents()
        job = latest()
        with open(p, "ab") as fh:
            fh.write(b"\0" * 1000)  # 다른 프로그램이 오디오를 고쳤다
        cur = sha(p)
        done, errors = w.backups.restore(job, force=True)
        check(not done and errors and "태그 부분만" in errors[0], f"{mode}: 오디오가 달라졌으면 알림 {errors}")
        check(sha(p) == cur, f"{mode}: 그 파일은 그대로")
        with open(p, "r+b") as fh:
            fh.truncate(os.path.getsize(p) - 1000)
        w.backups.restore(job, force=True)
        w.load_paths([work], replace=True)
        app.processEvents()

        # 앱이 도중에 꺼진 작업: 기록과 태그 보관이 디스크에 있으면 되돌릴 수 있다
        target = tagio.AudioFile.load(paths[".m4a"], opts)
        hash0 = sha(paths[".m4a"])
        crashed = w.backups.begin("꺼진 작업")
        entry = crashed.before(paths[".m4a"], copy_file=True, sync=False)
        crashed.sync()
        target.save({"comment": ["꺼지기 직전"]}, None, opts)
        crashed._log.close()  # 끝맺지 못하고 꺼졌다
        crashed._pack.close()
        job = next(j for j in w.backups.jobs() if j["id"] == crashed.id)
        check(job["status"] == "running" and job["entries"][0].get("delta"), f"{mode}: 끝나지 않은 작업도 기록에서 읽음")
        done, errors = w.backups.restore(job)
        check(not errors and sha(paths[".m4a"]) == hash0, f"{mode}: 꺼진 작업도 되돌림 {errors}")

        # 실패한 곡이 있어도 다른 곡의 태그 보관은 지우지 않는다
        job2 = w.backups.begin("일부 실패")
        e1 = job2.before(paths[".mp3"], copy_file=True, sync=False)
        e2 = job2.before(paths[".flac"], copy_file=True, sync=False)
        job2.sync()
        hash1 = sha(paths[".flac"])
        tagio.AudioFile.load(paths[".flac"], opts).save({"comment": ["성공"]}, None, opts)
        job2.failed(e1, "시험")
        job2.after(e2, paths[".flac"])
        job2.finish()
        job = next(j for j in w.backups.jobs() if j["id"] == job2.id)
        check(os.path.exists(os.path.join(job["folder"], backup.PACK_NAME)) and len(job["entries"]) == 1, f"{mode}: 태그 보관은 남음")
        done, errors = w.backups.restore(job)
        check(not errors and sha(paths[".flac"]) == hash1, f"{mode}: 성공한 곡은 되돌림 {errors}")


run_backup_checks("태그만 보관 · 메모리 사본" if backup.TAGS_ONLY else "기본")
if safewrite.IS_WIN:  # 맥처럼: 통째로 보관 · 디스크 사본
    saved_flags = backup.TAGS_ONLY, safewrite.MEM_STAGE
    backup.TAGS_ONLY, safewrite.MEM_STAGE = False, False
    run_backup_checks("통째로 · 디스크 사본 (맥과 같은 길)")
    backup.TAGS_ONLY, safewrite.MEM_STAGE = saved_flags

# 안전 저장을 끄면 mutagen 이 원본에 바로 쓰므로 원본 전체를 보관한다
if backup.TAGS_ONLY:
    w = window()
    w.opts = tagio.Options(safe_save=False)
    p = fresh(".mp3")
    h0 = sha(p)
    w.load_paths([p], replace=True)
    app.processEvents()
    w.apply_plans([Plan(w.model.files[0], changes={"comment": ["안전 저장 끔"]})], "끔")
    app.processEvents()
    job = w.backups.jobs()[0]
    check(not job["entries"][0].get("delta"), "안전 저장을 끄면 원본 전체를 보관")
    w.backups.restore(job)
    check(sha(p) == h0, "되돌리면 원래대로")

# ---- 6) 남는 자리: 태그가 커져도 오디오를 한 번만 옮긴다
for suffix in (".mp3", ".flac", ".m4a"):
    p = fresh(suffix)
    if suffix == ".m4a":
        moov_first(p)
        check(safewrite.payload_regions(p)[0][0] > 1000 and tagio.AudioFile.load(p, opts).length > 1, "moov 를 앞으로 옮긴 파일을 읽을 수 있음")
    calls = []
    f = tagio.AudioFile.load(p, opts)
    audio0 = audio(p)
    start0 = safewrite.payload_regions(p)[0][0]
    held = []
    f = f.save({"comment": ["자리보다 긴 코멘트 " * 3000]}, None, opts, snapshot=held, before_move=lambda: calls.append("옮김"))
    moved_once = bool(calls)
    start1 = safewrite.payload_regions(p)[0][0]
    check(moved_once and start1 > start0 and audio(p) == audio0, f"{suffix}: 자리가 모자라면 오디오를 옮기고 내용은 그대로")
    calls.clear()
    size1 = os.path.getsize(p)
    f = f.save({"comment": ["자리보다 긴 코멘트 " * 3000 + "조금 더"]}, None, opts, before_move=lambda: calls.append("옮김"))
    check(not calls and safewrite.payload_regions(p)[0][0] == start1, f"{suffix}: 그 뒤에 조금 늘어도 옮기지 않음")
    f = f.save({"comment": ["짧게"]}, None, opts, before_move=lambda: calls.append("옮김"))
    check(not calls and safewrite.payload_regions(p)[0][0] == start1 and os.path.getsize(p) == size1, f"{suffix}: 태그가 줄어도 옮기지 않음 (크기 그대로)")
    tagio.restore_snapshot(p, held[0], opts=opts)
    check(safewrite.payload_regions(p)[0][0] == start1 and audio(p) == audio0, f"{suffix}: 되돌려도 옮기지 않음 · 오디오 그대로")
    check(tagio.AudioFile.load(p, opts).get("comment") != "짧게", f"{suffix}: 되돌린 태그")
    # 커버를 크게 줄이면 파일도 줄어든다
    huge = b"\xff\xd8\xff\xe0" + bytes(1_500_000)
    f = tagio.AudioFile.load(p, opts).save({}, [tagio.Picture(huge, "image/jpeg", 3, "")], opts)
    grown = os.path.getsize(p)
    f = f.save({}, [tagio.Picture(b"\xff\xd8\xff\xe0" + bytes(2000), "image/jpeg", 3, "")], opts)
    check(os.path.getsize(p) < grown - 1_000_000 and audio(p) == audio0, f"{suffix}: 커버를 줄이면 파일도 줄어듦 ({grown} → {os.path.getsize(p)})")
    f = f.save({}, [], opts)
    check(not tagio.AudioFile.load(p, opts).pictures and audio(p) == audio0, f"{suffix}: 커버 지우기")

# ---- 7) 실행 취소: 파일이 그대로면 다시 읽지 않는다
w = window()
work = os.path.join(RUN, "undo")
os.makedirs(work)
for i, x in enumerate((".mp3", ".flac", ".m4a", ".ogg", ".wma")):
    shutil.copy2(fresh(x), os.path.join(work, f"곡 {i}{x}"))
w.load_paths([work], replace=True)
app.processEvents()
titles = {f.path: f.get("title") for f in w.model.files}
mtimes = {f.path: os.stat(f.path).st_mtime for f in w.model.files}
w.apply_plans([Plan(f, changes={"title": ["실행 취소 시험"]}) for f in w.model.files], "시험")
app.processEvents()
loads = []
orig_load = tagio.AudioFile.load
tagio.AudioFile.load = classmethod(lambda cls, p, o: (loads.append(p), orig_load(p, o))[1])
check(w._edited_elsewhere(w.undo_stack[-1], "실행 취소") == set() and not loads, f"그대로면 태그를 읽지 않음 ({len(loads)})")
tagio.AudioFile.load = orig_load
w.undo()
app.processEvents()
check({f.path: f.get("title") for f in w.model.files} == titles, "실행 취소 → 원래 제목")
check({f.path: os.stat(f.path).st_mtime for f in w.model.files} == mtimes, "실행 취소 → 수정 시각도")
check(len(w.redo_stack[-1].items) == 5 and all(i.snapshot is not None for i in w.redo_stack[-1].items), "다시 실행 기록 (되돌리면서 함께 만듦)")
w.redo()
app.processEvents()
check(all(f.get("title") == "실행 취소 시험" for f in w.model.files), "다시 실행 → 바꾼 제목")
time.sleep(0.03)
victim = next(f for f in w.model.files if f.ext == ".flac")
mf = mutagen.File(victim.path)
mf["title"] = ["다른 곳에서 고침"]
mf.save()
asked = []
w._ask_elsewhere = lambda verb, text, can_skip: asked.append(text) or None
w.undo()
app.processEvents()
check(asked and "제목" in asked[0], "다른 곳에서 고쳤으면 그래도 알아챔")
check(len(w.undo_stack[-1].items) == 5, "묻고 취소하면 기록 그대로")

# ---- 8) ID3 프레임 읽기: 복사하지 않아도 결과가 같다
_fast, _slow = tagio._read_frames_fast, tagio._read_frames


def both(data):
    out = []
    for fn in (_slow, _fast):
        tagio._id3_tags.read_frames = fn
        tags = ID3(io.BytesIO(data))
        out.append((tags.pprint(), sorted(tags.keys()), [bytes(x.data) for x in tags.getall("APIC")], tags._padding, len(tags.unknown_frames)))
    tagio._id3_tags.read_frames = _fast
    return out


pic = bytes(range(256)) * 300
for version in (3, 4):
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["제목 タイトル"]))
    tags.add(TPE1(encoding=1, text=["아이유", "IU"]))
    tags.add(COMM(encoding=1, lang="kor", desc="", text=["코멘트"]))
    tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="", data=pic))
    sub = ID3Tags()
    sub.add(TIT2(encoding=3, text=["챕터 제목"]))
    tags.add(CHAP(element_id="c1", start_time=0, end_time=1000, start_offset=0, end_offset=0, sub_frames=sub))
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL, child_element_ids=["c1"], sub_frames=ID3Tags()))
    buf = io.BytesIO()
    tags.save(buf, v2_version=version, v1=0)
    a, b = both(buf.getvalue())
    check(a == b, f"ID3v2.{version}: 같은 결과")
    tags = ID3(io.BytesIO(buf.getvalue()))
    check(tags["TPE1"].text == (["아이유/IU"] if version == 3 else ["아이유", "IU"]), f"ID3v2.{version}: 값 {tags['TPE1'].text}")
    check(str(tags["CHAP:c1"].sub_frames["TIT2"]) == "챕터 제목" and tags["CTOC:toc"].child_element_ids == ["c1"], f"ID3v2.{version}: 챕터 안의 프레임")


def syncsafe(n):
    return bytes((n >> s) & 0x7F for s in (21, 14, 7, 0))


# 직접 만든 태그: v2.2, 프레임 크기를 일반 정수로 쓴 v2.4 (예전 iTunes), 비동기화(unsynch) 플래그, 모르는 프레임, 잘린 프레임
def v22():
    body = b"TT2" + struct.pack(">L", 6)[1:] + b"\x00title" + b"TP1" + struct.pack(">L", 7)[1:] + b"\x00artist" + b"\0" * 20
    return b"ID3\x02\x00\x00" + syncsafe(len(body)) + body


def v24_plain_ints():
    frame = b"TIT2" + struct.pack(">L", 300) + b"\0\0" + b"\x00" + b"x" * 299
    body = frame * 3 + b"\0" * 50
    return b"ID3\x04\x00\x00" + syncsafe(len(body)) + body


def v23_unsynch():
    frame = b"TIT2" + struct.pack(">L", 6) + b"\0\0" + b"\x00title"
    body = frame + b"\0" * 10
    return b"ID3\x03\x00\x80" + syncsafe(len(body)) + body


def unknown_and_cut():
    frame = b"TIT2" + struct.pack(">L", 6) + b"\0\0" + b"\x00title"
    other = b"XYZW" + struct.pack(">L", 4) + b"\0\0" + b"data"
    cut = b"TPE1" + struct.pack(">L", 400) + b"\0\0" + b"\x00short"
    body = frame + other + cut
    return b"ID3\x03\x00\x00" + syncsafe(len(body)) + body


for name, data in (("v2.2", v22()), ("크기를 일반 정수로 쓴 v2.4", v24_plain_ints()), ("비동기화", v23_unsynch()), ("모르는·잘린 프레임", unknown_and_cut())):
    a, b = both(data)
    check(a == b, f"{name}: 같은 결과 {a[0][:40]!r}")

for suffix, cls in ((".mp3", "MP3"), (".flac", "FLAC"), (".m4a", "MP4"), (".ogg", None), (".wma", None)):
    p = fresh(suffix)
    with open(p, "rb") as f:
        fast = tagio._mf(f)
    slow = mutagen.File(p)
    check(type(fast) is type(slow), f"{suffix}: 형식 고르기가 mutagen.File 과 같음 ({type(fast).__name__})")
    stage = MemStage.over(p)
    check(type(tagio._mf(rewind(stage))) is type(slow), f"{suffix}: 메모리 사본도 같음")
    stage.close()
    check(type(tagio._mf(p)) is type(slow), f"{suffix}: 경로로도 같음")
    # 확장자와 내용이 다른 파일
    for wrong in (".mp3", ".flac", ".m4a"):
        q = p + ".잘못" + wrong
        shutil.copy2(p, q)
        with open(q, "rb") as f:
            try:
                got = type(tagio._mf(f))
            except Exception as e:
                got = type(e).__name__
        try:
            want = type(mutagen.File(q))
        except Exception as e:
            want = type(e).__name__
        check(got == want, f"{suffix} 를 {wrong} 로 이름 바꿈: {got} / {want}")
        os.unlink(q)

# 곡 읽기: 파일 정보를 한 번 연 파일에서
p = fresh(".mp3")
af = tagio.AudioFile.load(p, opts)
st = os.stat(p)
check((af.size, af.mtime, af.ino, af.dev) == (st.st_size, st.st_mtime, st.st_ino, st.st_dev), "파일 정보가 stat 과 같음")
check(abs(af.btime - tagio._birthtime(st)) < 1, "생성 시각")
try:
    tagio.AudioFile.load(os.path.join(RUN, "없음.mp3"), opts)
    check(False, "없는 파일")
except tagio.UnsupportedFile:
    check(True, "없는 파일은 UnsupportedFile")
garbage = os.path.join(RUN, "쓰레기.mp3")
with open(garbage, "wb") as f:
    f.write(b"\x00" * 3000)
try:
    tagio.AudioFile.load(garbage, opts)
    check(False, "내용이 없는 파일")
except tagio.UnsupportedFile:
    check(True, "내용이 깨진 파일은 UnsupportedFile")

# ---- 9) 표 모델 지름길
w = window()
w.load_paths([album], replace=True)
app.processEvents()
m = w.model
keys = model.COLUMN_KEYS
idx = lambda key, row=0: m.index(row, keys.index(key))  # noqa: E731
f0 = m.files[0]
check(m.data(idx("title"), Qt.DisplayRole) == f0.get("title") == m.data(idx("title"), int(Qt.DisplayRole)), "표시 (역할을 enum 으로도 정수로도)")
check(m.data(idx("title"), Qt.ToolTipRole) == f0.get("title"), "말풍선")
check(m.data(idx("filename"), Qt.EditRole) == f0.stem, "고칠 때 파일명은 확장자 빼고")
check(m.data(idx("length"), Qt.TextAlignmentRole) == int(Qt.AlignRight | Qt.AlignVCenter) and m.data(idx("title"), Qt.TextAlignmentRole) is None, "오른쪽 맞춤")
check(m.data(idx("title"), model.SORT_ROLE) == f0.get("title").lower(), "정렬 값")
check(m.data(idx("title"), Qt.DecorationRole) is None and m.data(idx("title"), Qt.FontRole) is None, "쓰지 않는 역할")
check(m.data(QModelIndex(), Qt.DisplayRole) is None, "잘못된 칸")
check(m.headerData(0, Qt.Horizontal, Qt.DisplayRole) == model.COLUMNS[0][1] and m.headerData(0, Qt.Vertical, Qt.DisplayRole) is None
      and m.headerData(0, Qt.Horizontal, Qt.ToolTipRole) is None, "머리글")
check(bool(m.flags(idx("title")) & Qt.ItemIsEditable) and not bool(m.flags(idx("length")) & Qt.ItemIsEditable), "고칠 수 있는 칸만")
check(bool(m.flags(idx("length")) & Qt.ItemIsSelectable) and bool(m.flags(idx("length")) & Qt.ItemIsEnabled), "고를 수 있음")
check(not bool(m.flags(QModelIndex()) & Qt.ItemIsEditable), "잘못된 칸은 못 고침")
editor = m.on_edit
m.on_edit = None
check(not bool(m.flags(idx("title")) & Qt.ItemIsEditable), "고치는 쪽이 없으면 못 고침")
m.on_edit = editor

# ---- 10) 글꼴 · 미리 불러오기 · 최근 폴더
if safewrite.IS_WIN:
    light = mainwindow._app_font(mainwindow.FONT_FAMILIES[:1])
    app.setFont(light)
    check(app.font().families() == mainwindow.FONT_FAMILIES[:1], "처음에는 맑은 고딕만")
    w = window()
    mainwindow._finish_fonts(app, w)
    end = time.monotonic() + 20
    while app.font().families() != mainwindow.FONT_FAMILIES and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    check(app.font().families() == mainwindow.FONT_FAMILIES, f"뒤에서 읽어 둔 뒤 대체 글꼴까지 ({app.font().families()})")

for _ in mainwindow._preload_steps():
    pass
check(all(f"localtag.{n}" in sys.modules for n in mainwindow.PRELOAD_MODULES), "미리 불러오기 끝까지")
check(tagio._file_options is not None, "형식 목록도")

w = window()
gone = os.path.join(RUN, "사라진 폴더")
w.settings.setValue("recent_folders", [gone])
seen = []
orig_isdir = os.path.isdir
os.path.isdir = lambda x: (seen.append(x), orig_isdir(x))[1]
w._fill_recent(check=False)
fast_seen = list(seen)
w._fill_recent()
os.path.isdir = orig_isdir
check(gone not in fast_seen, "앱을 켤 때는 폴더를 확인하지 않음 (꺼진 네트워크 드라이브에서 멈추지 않게)")
check(gone in seen and any("찾을 수 없음" in a.text() for a in w.recent_menu.actions()), "메뉴를 열면 확인")
w.settings.setValue("recent_folders", [])
finish()
