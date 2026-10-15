"""반영한 뒤 Music 다시 켜기 (Music 은 흉내만 — 실제 Music 을 끄거나 켜지 않는다)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag import music  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()
log = []
state = {"running": True, "player": "stopped\t\t0"}
music.time.sleep = lambda s: None


def fake_osascript(script, *args, timeout=60):
    if "player state" in script:
        return state["player"]
    if "to quit" in script:
        log.append("quit")
        state["running"] = False
        return ""
    if "to get name" in script:
        return "Music"
    if "play t" in script:
        log.append(("resume",) + args)
        return ""
    return ""


class R:
    def __init__(self, rc):
        self.returncode = rc


real_run = music.subprocess.run


def fake_run(cmd, *a, **k):
    if cmd[0] == "/usr/bin/pgrep":
        return R(0 if state["running"] else 1)
    if cmd[0] == "/usr/bin/open":
        log.append("open")
        state["running"] = True
        return R(0)
    return real_run(cmd, *a, **k)


music._osascript = fake_osascript
music.subprocess.run = fake_run

check(music.restart("never") == "never" and not log, "하지 않기")
check(music.restart("idle") == "restarted" and log == ["quit", "open"], f"멈춰 있으면 다시 켬 {log}")
log.clear()
state["player"] = "playing\tPID1\t83.5"
check(music.restart("idle") == "playing" and not log, "재생 중이면 건너뜀")
check(music.restart("always") == "restarted" and log == ["quit", "open", ("resume", "PID1", "83.5", "playing")],
      f"항상: 듣던 곡·위치 이어서 {log}")
log.clear()
state["running"] = False
check(music.restart("idle") == "notrunning" and not log, "Music 이 꺼져 있으면 아무것도 안 함")
state["running"], state["player"] = True, "stopped\t\t0"

# 저장 → 반영: 앨범을 바꿨을 때만 다시 켜기
d = samples()
w = window()
w.load_paths([os.path.join(d, "아이유 - 꽃갈피")], replace=True)
app.processEvents()
calls = []
w._music_refresh_paths = lambda paths, modal=True, album_change=False, known_pids=None: calls.append(album_change)
w._music_prelookup = lambda files: {}  # 실제 Music 에 묻지 않는다
w.settings.setValue("music_auto", True)
music.IS_MAC = True
f = w.model.files[0]
w.apply_plans([Plan(f, changes={"title": ["제목만"]})], "제목")
w.apply_plans([Plan(w.model.files[0], changes={"album": ["새 앨범"]})], "앨범")
w.undo()
w.settings.setValue("music_auto", False)
if sys.platform == "darwin":
    check(calls == [False, True, True], f"제목만 → 안 켬, 앨범 → 켬, 앨범 되돌리기 → 켬 {calls}")
finish()
