"""백그라운드 작업: 빨리 끝나는 작업을 잔뜩 돌려도 아직 도는 스레드를 지우지 않는다 (1.0.5 에서 앱이 죽음).

실행: python tests/test_tasks.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, window  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402

from localtag.tasks import run_task  # noqa: E402

app = QApplication.instance()
w = window()
done = []
N = 300
for i in range(N):
    run_task(w, "", lambda p, c, i=i: i, done.append, done.append, modal=False)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)  # 지우기를 바로 처리 — 예전엔 여기서 죽었다
end = time.time() + 10
while (len(done) < N or w._tasks) and time.time() < end:
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
check(sorted(done) == list(range(N)), f"작업 {N}개 모두 끝남 ({len(done)})")
check(not w._tasks, f"끝난 작업은 모두 정리됨 ({len(w._tasks)})")
finish()
