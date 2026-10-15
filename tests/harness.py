"""격리된 테스트 환경: 실제 설정·앱 데이터·Music 을 건드리지 않는 MainWindow.

- 설정: mainwindow.QSettings 를 임시 INI 파일로 바꿔 끼운다
  (macOS 실제 설정 ~/Library/Preferences/LocalTag.LocalTag.plist, Windows 는 레지스트리 HKCU\\Software\\LocalTag).
  실제 설정에 clear() 같은 것을 절대 하지 않는다.
- 앱 데이터(백업 등): QStandardPaths.writableLocation 을 이번 실행 폴더 안으로 돌린다
  (setTestModeEnabled 만 쓰면 ~/.qttest 등에 쌓인다 — 예전에 5GB까지 커졌던 적이 있다).
- 실행 폴더: tests/runs/<날짜-시각>/ — 실행할 때마다 새로 만들고, 이전 실행 폴더는 지운다.
"""
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALTAG_NO_PERMISSION_PROMPT"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RUNS = os.path.join(ROOT, "tests", "runs")
os.makedirs(RUNS, exist_ok=True)
RUN = os.path.join(RUNS, time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")  # 같은 초에 여러 테스트를 돌려도 겹치지 않게
for old in os.listdir(RUNS):
    p = os.path.join(RUNS, old)
    if p != RUN and os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
os.makedirs(RUN, exist_ok=True)

from PySide6.QtCore import QSettings, QStandardPaths  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

QStandardPaths.setTestModeEnabled(True)
_data = os.path.join(RUN, "appdata")
os.makedirs(_data, exist_ok=True)
QStandardPaths.writableLocation = staticmethod(lambda kind: _data)

from localtag import mainwindow as mw  # noqa: E402
from localtag import music  # noqa: E402

music.drag_has_music = lambda: False  # 끌기 보드에 남은 예전 Music 끌기 기록을 보지 않게
_ini = os.path.join(tempfile.mkdtemp(dir=RUN), "settings.ini")


class TestSettings(QSettings):
    def __init__(self, *a):
        super().__init__(_ini, QSettings.IniFormat)


mw.QSettings = TestSettings
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARN:", a[1:3]))
QMessageBox.information = staticmethod(lambda *a, **k: print("INFO:", a[1:3]))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
app = QApplication.instance() or QApplication([])


def window():
    w = mw.MainWindow()
    assert not w.settings.value("music_auto", False, bool), "테스트에서는 Music 자동 반영을 쓰지 않는다"
    return w


def samples(name="samples"):
    """저장소의 samples/ 를 이번 실행 폴더로 복사해 그 경로를 돌려준다 (원본은 건드리지 않는다)."""
    dst = os.path.join(RUN, name)
    shutil.copytree(os.path.join(ROOT, "samples"), dst)
    return dst


_ok = True


def check(cond, msg):
    global _ok
    print(("ok  " if cond else "FAIL"), msg)
    if not cond:
        _ok = False


def finish():
    print("RESULT", "PASS" if _ok else "FAIL", "—", RUN)
    sys.exit(0 if _ok else 1)
