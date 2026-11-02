"""Music 반영 속도 '몰아서' (Music 은 흉내만 — 실제 Music 을 쓰지 않는다).

- 곡마다 기다리지 않도록 ID_SCRIPT 에 대기 칸 0, 0 을 넘긴다 — 이때 스크립트는 refresh 만 보내고 정보 줄이 없다
- 끝난 뒤 한 번에 확인하고, 아직 옛 정보인 곡만 다시 반영한 뒤 그 곡만 다시 확인한다
- 다른 속도는 예전처럼 곡마다 기다린다
- 저장 전 찾기: 곡이 많으면 보관함 파일(iTunesLibrary)에서 한 번에, 못 찾은 곡만 Music 에 묻는다
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, check, finish  # noqa: E402

from localtag import librarycheck, music  # noqa: E402

music.ensure_access = lambda *a, **k: None
music.time.sleep = lambda s: None
A, B = (os.path.realpath(os.path.join(RUN, n)) for n in ("lt-a.mp3", "lt-b.mp3"))  # 이번 실행 폴더 안에
for p in (A, B):
    open(p, "wb").close()
calls = []
late = {"PB"}  # 첫 반영에서 Music 이 놓친 곡
path_of = {"PA": A, "PB": B}


def fake_script(script, args, progress, cancelled, idle_limit=240):
    calls.append(list(args))
    if args[0] == "refresh":
        if len(calls) > 1:
            late.clear()  # 다시 반영하면 따라온다
        if args[2] == "0":
            return []  # 몰아서: 새로 읽으라고만 한다
    ids = args[1:] if args[0] == "read" else args[3::3]
    return [f"LOCALTAG_INFO:{pid}\t{'옛 제목' if pid in late else '새 제목'}\t앨범\t{path_of[pid]}" for pid in ids]


music._run_script = fake_script
want = {A: ("새 제목", "앨범"), B: ("새 제목", "앨범")}
res = music.refresh([A, B], expected=want, pace="batch", known_pids={A: "PA", B: "PB"})
check("batch" in music.PACES and "batch" in music.PACE_LABELS, "반영 속도 메뉴에 '몰아서'")
check(calls[0][:3] == ["refresh", "0", "0"], f"곡마다 기다리지 않음 {calls[0][:3]}")
check(calls[0][3:] == ["PA", "새 제목", "앨범", "PB", "새 제목", "앨범"], f"모든 곡을 한 번에 {calls[0][3:]}")
check(calls[1] == ["read", "PA", "PB"], f"끝난 뒤 한 번에 확인 {calls[1]}")
check(calls[2][:4] == ["refresh", "0", "0", "PB"] and len(calls[2]) == 6, f"놓친 곡만 다시 {calls[2]}")
check(calls[3:] == [["read", "PB"]], f"다시 반영한 곡만 다시 확인 {calls[3:]}")
check(res.confirmed == 2 and not res.unconfirmed, f"둘 다 반영 확인 {res.confirmed} {res.unconfirmed}")
check('maxTicks is 0' in music.ID_SCRIPT and "properties of (first file track" in music.ID_SCRIPT,
      "스크립트: 몰아서는 refresh 만, 확인은 곡마다 한 번에")

calls.clear()
late.add("PB")
music.refresh([A], expected={A: want[A]}, pace="fast", known_pids={A: "PA"})
check(calls[0][:3] == ["refresh", "4", "40"], f"'빠르게'는 그대로 곡마다 기다림 {calls[0][:3]}")
check([c[0] for c in calls] == ["refresh", "read"], f"'빠르게'도 반영 뒤 한 번 확인 {[c[0] for c in calls]}")

# ---- 저장 전 찾기
paths = [os.path.realpath(os.path.join(RUN, f"song{i:02}.m4a")) for i in range(20)]
library = {p: f"ID{i:02}" for i, p in enumerate(paths[:18])}  # 둘은 막 추가해서 보관함 파일에 아직 없음
asked = []
music._music_running = lambda: True
librarycheck.library_ids = lambda: library
music._osascript = lambda script, *a, **k: "\n".join(v for v in library.values() if v != "ID05")  # ID05 는 Music 에서 지움


def fake_prelookup(script, args, progress, cancelled, idle_limit=240):
    asked.extend(args[0::3])
    return [f"LOCALTAG_RESULT:{p}\tNEW{i}" for i, p in enumerate(args[0::3])]


music._run_script = fake_prelookup
found = music.prelookup([(p, "제목", "앨범") for p in paths])
check(sorted(asked) == sorted([paths[5], paths[18], paths[19]]), f"보관함 파일에서 못 찾은 곡만 Music 에 묻기 {len(asked)}")
check(len(found) == 20 and found[paths[0]] == "ID00" and found[paths[5]] != "ID05", f"모두 찾음 {len(found)}")
asked.clear()
music.prelookup([(p, "제목", "앨범") for p in paths[:3]])
check(len(asked) == 3, "곡이 적으면 Music 에 바로 묻기")
for p in (A, B):
    os.remove(p)
finish()
