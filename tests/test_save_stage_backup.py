"""저장 속도 개선이 안전 저장·백업을 해치지 않는지.

- Windows: 작업 사본을 디스크가 아니라 메모리에 둔다 (곡마다 새 파일을 만들지 않음). 형식마다 저장·되돌리기·오디오 검증
- 백업 기록을 여러 곡씩 모아 확정해도: 묶음보다 많은 곡을 저장한 뒤 백업 관리자에서 모두 되돌릴 수 있다
- 백업 사본을 만들지 못한 곡은 바꾸지 않는다
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, QApplication, check, finish, samples, window  # noqa: E402

import mutagen  # noqa: E402

from localtag import backup, mainwindow, safewrite  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
d = samples()
w = window()
w.load_paths([d], replace=True)
app.processEvents()

# 1) 형식마다 저장 → 다시 읽기 → 되돌리기, Windows 에서는 디스크 작업 사본을 만들지 않는다
copies = []
orig_copy = safewrite.copy_preserving
safewrite.copy_preserving = lambda s, t: (copies.append(t), orig_copy(s, t))[1]
before = {f.path: f.get("title") for f in w.model.files}
raw = {}
for f in w.model.files:
    regions = safewrite.payload_regions(f.path)
    if regions:
        with open(f.path, "rb") as fp:
            data = fp.read()
        raw[f.path] = [data[s:s + n] for s, n in regions]
w.apply_plans([Plan(f, changes={"title": ["메모리 저장"]}) for f in w.model.files], "테스트")
app.processEvents()
stage_copies = [c for c in copies if os.path.basename(c).startswith(safewrite.STAGE_PREFIX)]
if safewrite.MEM_STAGE:
    check(not stage_copies, f"Windows: 디스크 작업 사본을 만들지 않음 ({len(stage_copies)}개)")
safewrite.copy_preserving = orig_copy
for f in w.model.files:
    check(f.get("title") == "메모리 저장", f"{f.ext} 저장 → 목록 값")
    mf = mutagen.File(f.path, easy=True)
    got = (mf.get("title") or [None])[0] if mf is not None and hasattr(mf, "get") else None
    if f.ext != ".wma":  # easy 모드가 없는 형식은 목록 값으로만 본다
        check(got == "메모리 저장", f"{f.ext} 파일에 실제로 저장 ({got!r})")
for path, chunks in raw.items():
    regions = safewrite.payload_regions(path)
    with open(path, "rb") as fp:
        data = fp.read()
    check([data[s:s + n] for s, n in regions] == chunks, f"{os.path.splitext(path)[1]} 오디오 데이터 그대로")
leftover = [n for root, _, fs in os.walk(d) for n in fs if n.startswith(safewrite.STAGE_PREFIX)]
check(not leftover, f"작업 사본 남지 않음 {leftover}")
w.undo()
app.processEvents()
check({f.path: f.get("title") for f in w.model.files} == before, "되돌리기")

# 2) 메모리 사본에서 원본으로 달라진 부분만 다시 쓰기
p = next(f.path for f in w.model.files if f.ext == ".mp3")
with open(p, "rb") as fp:
    data = fp.read()
stage = safewrite.MemStage(b"ID3" + data[3:], p)
ino = os.stat(p).st_ino
safewrite.write_back(stage, p)
with open(p, "rb") as fp:
    check(fp.read() == b"ID3" + data[3:], "메모리 사본 → 원본 다시 쓰기")
check(os.stat(p).st_ino == ino, "파일 번호 그대로 (바꿔치기 아님)")

# 3) 백업 묶음보다 많은 곡을 저장 → 백업 관리자에서 모두 되돌리기
many = os.path.join(RUN, "many")
os.makedirs(many)
mp3 = next(f.path for f in w.model.files if f.ext == ".mp3")
n = mainwindow.BACKUP_BATCH * 2 + 3
for i in range(n):
    shutil.copy2(mp3, os.path.join(many, f"{i:03d}.mp3"))
w.load_paths([many], replace=True)
app.processEvents()
syncs = []
orig_sync = backup.BackupJob.sync
backup.BackupJob.sync = lambda self: (syncs.append(1), orig_sync(self))[1]
w.apply_plans([Plan(f, changes={"title": ["백업 묶음"]}) for f in w.model.files], "백업 묶음")
app.processEvents()
backup.BackupJob.sync = orig_sync
check(len(syncs) == -(-n // mainwindow.BACKUP_BATCH), f"백업 기록 확정 {len(syncs)}번 ({n}곡)")
job = w.backups.jobs()[0]
check(len([e for e in job["entries"] if e["state"] == "done"]) == n, f"백업 항목 {len(job['entries'])}개 모두 완료")
done, errors = w.backups.restore(job)
check(len(done) == n and not errors, f"백업에서 모두 되돌림 ({len(done)}개, 오류 {errors[:2]})")
titles = {str(mutagen.File(os.path.join(many, x), easy=True)["title"][0]) for x in os.listdir(many)}
check(titles == {before[mp3]}, f"되돌린 제목 {titles}")

# 4) 백업 사본을 만들지 못한 곡은 바꾸지 않는다
w.load_paths([many], replace=True)
app.processEvents()
files = list(w.model.files)[:3]
bad = files[1].path
orig_before = backup.BackupJob.before


def failing_before(self, path, copy_file, sync=True):
    if path == bad:
        raise safewrite.SafeWriteError("백업할 디스크 공간이 부족합니다.")
    return orig_before(self, path, copy_file, sync)


backup.BackupJob.before = failing_before
w.apply_plans([Plan(f, changes={"title": ["실패 시험"]}) for f in files], "실패 시험")
app.processEvents()
backup.BackupJob.before = orig_before
got = [str(mutagen.File(f.path, easy=True)["title"][0]) for f in files]
check(got == ["실패 시험", before[mp3], "실패 시험"], f"백업 못 한 곡만 그대로 {got}")
finish()
