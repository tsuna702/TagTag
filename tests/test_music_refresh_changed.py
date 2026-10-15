"""Music 반영 확인 중에 사용자가 Music 에서 곡을 직접 고친 경우 (Music 은 흉내만 — 실제 Music 을 쓰지 않는다).

- 파일도 다른 값으로 바뀌었으면: 다시 반영하지 않고 '그 값을 따름' (res.changed)
- 파일은 LocalTag 가 저장한 값 그대로인데 Music 만 늦으면: 지금처럼 다시 반영
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish  # noqa: E402

from localtag import music  # noqa: E402
from localtag.tasks import run_task  # noqa: E402

music.ensure_access = lambda *a, **k: None
music.time.sleep = lambda s: None
A, B = os.path.realpath("/tmp/lt-a.mp3"), os.path.realpath("/tmp/lt-b.mp3")
for p in (A, B):
    open(p, "wb").close()
music._lookup = lambda cur, keys, progress, cancelled: ({k: ("PA" if k == A else "PB") for k in keys}, {})
calls = []


def fake_script(script, args, progress, cancelled, idle_limit=240):
    calls.append(args[0])
    # Music 에는 계속: A 는 사용자가 Music 에서 고친 제목, B 는 옛 제목
    return [f"LOCALTAG_INFO:PA\t사용자가 고친 제목\t앨범\t{A}", f"LOCALTAG_INFO:PB\t옛 제목\t앨범\t{B}"]


music._run_script = fake_script
files = {A: ("사용자가 고친 제목", "앨범"), B: ("새 제목", "앨범")}  # 파일에 지금 든 값
res = music.refresh([A, B], expected={A: ("새 제목", "앨범"), B: ("새 제목", "앨범")},
                    file_values=lambda p: files[p])
check(res.changed == [A], f"Music 에서 고친 곡은 그 값을 따름 {res.changed}")
check(res.unconfirmed == [B], f"Music 이 늦은 곡은 그대로 다시 반영 대상 {res.unconfirmed}")
check(calls.count("refresh") == 1 + 3, f"다시 반영은 늦은 곡만 3번 ({calls})")
# file_values 가 없으면 예전처럼 둘 다 다시 반영
calls.clear()
res = music.refresh([A, B], expected={A: ("새 제목", "앨범"), B: ("새 제목", "앨범")})
check(not res.changed and sorted(res.unconfirmed) == sorted([A, B]), "file_values 없으면 예전 동작")
# 저장 전에 찾아 둔 ID(known_pids)가 있으면 위치로 다시 찾지 않는다 (위치로 찾으면 Music 이 몰아서 다시 읽는다)
looked = []
music._lookup = lambda cur, keys, progress, cancelled: (looked.extend(keys), ({k: "PB" for k in keys}, {}))[1]
calls.clear()
res = music.refresh([A, B], expected={A: ("사용자가 고친 제목", "앨범"), B: ("옛 제목", "앨범")}, known_pids={A: "PA"})
check(looked == [B], f"ID 를 아는 곡은 위치로 찾지 않음 {looked}")
check(res.confirmed == 2, f"둘 다 반영 확인 {res.confirmed}")

# 진행 표시에 개수가 두 번 붙지 않음 (실제 run_task 의 상태 표시줄 경로)
import time  # noqa: E402

from harness import app  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

shown, finished = [], []
host = QWidget()
run_task(host, "t", lambda prog, cancelled: (prog(4, 15, "다시 반영 · 1번째 (4/15)"), prog(5, 15, "확인 중"))[-1],
         lambda v: finished.append(1), lambda m: finished.append(m), modal=False, status=shown.append)
t = time.time()
while not finished and time.time() - t < 10:
    app.processEvents()
check(shown == ["다시 반영 · 1번째 (4/15)", "확인 중 (5/15)"], f"개수는 한 번만 {shown}")
for p in (A, B):
    os.remove(p)
finish()
