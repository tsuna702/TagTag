"""작업(Music 반영 등)이 도는 중에 앱을 닫아도 죽지 않는다 (1.1.10: QThread 가 창과 함께 지워져 abort).

- 닫을 때 작업에 취소를 알리고 끝나기를 기다린다
- 취소해도 끝나지 않는 작업이면 창을 지우지 않고 끝낸다 (abort 대신 종료 코드 0)
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import RUN, ROOT, app, check, finish, window  # noqa: E402, F401

from localtag.tasks import run_task, stop_tasks  # noqa: E402


def polite(prog, cancelled):
    for _ in range(300):  # 최대 30초, 취소하면 곧바로
        if cancelled():
            return "취소"
        time.sleep(0.1)
    return "끝"


w = window()
got = []
task = run_task(w, "", polite, got.append, got.append, modal=False)
time.sleep(0.2)
t0 = time.monotonic()
w.close()
check(not task.isRunning() and time.monotonic() - t0 < 2, f"닫으면 취소하고 기다림 ({time.monotonic() - t0:.1f}초)")
check(not getattr(w, "tasks_left", False), "다 끝났으니 바로 끝낼 필요 없음")

stubborn = run_task(w, "", lambda prog, cancelled: time.sleep(1), got.append, got.append, modal=False)
time.sleep(0.1)
check(not stop_tasks(w, timeout=0.1), "취소해도 안 끝나면 False")
stubborn.wait()

# 실제로 앱을 띄워 끝내 본다 (설정·앱 데이터는 실행 폴더 안에)
CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QSettings, QStandardPaths, QTimer
QStandardPaths.writableLocation = staticmethod(lambda kind: sys.argv[2])
from localtag import mainwindow as mw, tasks
class S(QSettings):
    def __init__(self, *a):
        super().__init__(os.path.join(sys.argv[2], "settings.ini"), QSettings.IniFormat)
mw.QSettings = S
mw.stop_tasks = lambda owner: tasks.stop_tasks(owner, timeout=0.3)
stubborn = sys.argv[3] == "stubborn"
def job(prog, cancelled):
    for _ in range(50):
        if cancelled() and not stubborn:
            return
        time.sleep(0.1)
def go(w):  # main() 이 창을 띄운 뒤 부르는 자리
    tasks.run_task(w, "", job, lambda v: None, lambda m: None, modal=False)
    QTimer.singleShot(200, w.close)
mw.MainWindow.reopen_last = go
sys.exit(mw.main(["LocalTag"]))
'''
for kind in ("polite", "stubborn"):
    data = os.path.join(RUN, f"child-{kind}")
    os.makedirs(data, exist_ok=True)
    r = subprocess.run([sys.executable, "-c", CHILD, ROOT, data, kind], capture_output=True, text=True, timeout=60)
    check(r.returncode == 0, f"작업 중에 닫기 ({kind}) → 종료 코드 {r.returncode} {r.stderr[-300:]}")
finish()
