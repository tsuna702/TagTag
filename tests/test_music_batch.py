"""Music 반영 속도 '몰아서' (Music 은 흉내만 — 실제 Music 을 쓰지 않는다).

- 곡마다 기다리지 않도록 ID_SCRIPT 에 대기 칸 0, 0 을 넘긴다
- 끝난 뒤 한 번에 확인하고, 아직 옛 정보인 곡만 다시 반영한다
- 다른 속도는 예전처럼 곡마다 기다린다
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, check, finish  # noqa: E402

from localtag import music  # noqa: E402

music.ensure_access = lambda *a, **k: None
music.time.sleep = lambda s: None
A, B = (os.path.realpath(os.path.join(RUN, n)) for n in ("lt-a.mp3", "lt-b.mp3"))  # 이번 실행 폴더 안에
for p in (A, B):
    open(p, "wb").close()
calls = []
late = {"PB"}  # 첫 반영에서 Music 이 놓친 곡


def fake_script(script, args, progress, cancelled, idle_limit=240):
    calls.append(list(args))
    if args[0] == "refresh" and len(calls) > 1:
        late.clear()  # 다시 반영하면 따라온다
    return [f"LOCALTAG_INFO:PA\t새 제목\t앨범\t{A}",
            f"LOCALTAG_INFO:PB\t{'옛 제목' if 'PB' in late else '새 제목'}\t앨범\t{B}"]


music._run_script = fake_script
want = {A: ("새 제목", "앨범"), B: ("새 제목", "앨범")}
res = music.refresh([A, B], expected=want, pace="batch", known_pids={A: "PA", B: "PB"})
check("batch" in music.PACES and "batch" in music.PACE_LABELS, "반영 속도 메뉴에 '몰아서'")
check(calls[0][:3] == ["refresh", "0", "0"], f"곡마다 기다리지 않음 {calls[0][:3]}")
check(calls[0][3:] == ["PA", "새 제목", "앨범", "PB", "새 제목", "앨범"], f"모든 곡을 한 번에 {calls[0][3:]}")
check(calls[1][0] == "read", "끝난 뒤 한 번에 확인")
check(calls[2][:4] == ["refresh", "0", "0", "PB"] and len(calls[2]) == 6, f"놓친 곡만 다시 {calls[2]}")
check(res.confirmed == 2 and not res.unconfirmed, f"둘 다 반영 확인 {res.confirmed} {res.unconfirmed}")

calls.clear()
music.refresh([A], expected={A: want[A]}, pace="fast", known_pids={A: "PA"})
check(calls[0][:3] == ["refresh", "4", "40"], f"'빠르게'는 그대로 곡마다 기다림 {calls[0][:3]}")
for p in (A, B):
    os.remove(p)
finish()
