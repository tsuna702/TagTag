"""Music 이 앨범 폴더를 옮긴 뒤의 실행 취소 · Music 반영 중 실행 취소는 끝난 뒤에."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
d = samples()
src = os.path.join(d, "아이유 - 꽃갈피")
w = window()
w.load_paths([src], replace=True)
app.processEvents()
files = [f for f in w.model.files if f.ext in (".mp3", ".flac")]
w.apply_plans([Plan(f, changes={"album": ["꽃갈피 2"]}) for f in files], "앨범 이름")
app.processEvents()
# Music 이 새 앨범 폴더로 옮긴 것처럼
dst = os.path.join(d, "아이유 - 꽃갈피 2")
os.makedirs(dst)
relocated = {}
for f in [x for x in w.model.files if x.ext in (".mp3", ".flac")]:
    new = os.path.join(dst, f.filename)
    shutil.move(f.path, new)
    relocated[os.path.realpath(f.path)] = new
check(w._relink(relocated) == 2, "목록 경로를 새 위치로")
item_paths = [(i.old_path, i.new_path) for i in w.undo_stack[-1].items]
check(all(o == n and o.startswith(dst) for o, n in item_paths), "실행 취소 기록 경로도 새 위치로")
# Music 반영 중에는 기다렸다가 되돌린다
w._music_busy = True
w.undo()
check(w._undo_waiting and w.undo_stack, "반영 중엔 되돌리기를 미룸")
w._music_busy = False
w._run_waiting_undo()
for _ in range(5):
    app.processEvents()
check(not w.undo_stack, "반영이 끝나면 되돌림")
albums = {f.get("album") for f in w.model.files if f.ext in (".mp3", ".flac")}
check(albums == {"꽃갈피"} and all(os.path.exists(f.path) for f in w.model.files), f"새 위치에서 태그만 되돌림 {albums}")
finish()
