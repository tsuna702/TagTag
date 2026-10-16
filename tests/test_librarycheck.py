"""Music 보관함 점검: 커버 없는 곡 · 갈라진 앨범 · 파일 없는 곡 (가짜 보관함 — 실제 Music 은 읽지도 않음).

실행: python tests/test_librarycheck.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag import librarycheck as lc  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
p = {n: os.path.join(album, n) for n in os.listdir(album)}
T = lc.LibTrack
tracks = [
    # 꽃갈피: 한 곡만 앨범 아티스트가 달라 갈라짐 (없으면 아티스트로 묶여 같음), 한 곡은 커버 없음
    T("1", "곡 1", "아이유", "꽃갈피", "아이유", path=p["01 곡 1.mp3"]),
    T("2", "곡 2", "아이유", "꽃갈피", "아이유", path=p["02 곡 2.flac"], has_art=False),
    T("3", "곡 3", "아이유", "꽃갈피", "IU", path=p["03 곡 3.m4a"]),
    T("9", "새 제목", "아이유", "꽃갈피", "", path=p["새 파일.ogg"]),
    # 이름만 같은 다른 가수의 앨범 → 갈라짐으로 보지 않음
    T("4", "A", "가수1", "Greatest Hits", "가수1", path="/x/a/1.mp3"),
    T("5", "B", "가수2", "Greatest Hits", "가수2", path="/x/b/1.mp3"),
    # 컴필레이션은 앨범 아티스트가 없어도 하나로
    T("6", "C", "가수3", "OST", "", compilation=True, path="/x/c/1.mp3"),
    T("7", "D", "가수4", "OST", "", compilation=True, path="/x/c/2.mp3"),
    # 파일 없음
    T("8", "사라진 곡", "누군가", "옛 앨범", "누군가", path="/nowhere/8.mp3", exists=False),
]
r = lc.analyze(tracks)
check(r.total == 9, "곡 수")
check([(g.album, [t.pid for t in g.tracks]) for g in r.nocover] == [("꽃갈피", ["2"])], f"커버 없는 곡 {r.nocover}")
check([g.album for g in r.split] == ["꽃갈피"], f"갈라진 앨범만 (이름만 같은 앨범·컴필레이션 제외) {[g.album for g in r.split]}")
labels = [lab for lab, _ in r.split[0].parts]
check(len(labels) == 2 and "'아이유'" in labels[0] and "3곡" in labels[0] and "'IU' — 1곡" in labels[1],
      f"갈라진 묶음 설명 (앨범 아티스트 없는 곡은 아티스트로 묶임) {labels}")
check([(g.album, [t.pid for t in g.tracks]) for g in r.missing] == [("옛 앨범", ["8"])], "파일 없는 곡")

# 창 → 고른 곡 열기 (앨범을 고르면 그 앨범 곡 전부)
w = window()
lc.read_library = lambda: tracks


def run(self):
    check(self.tabs.count() == 3 and "(1)" in self.tabs.tabText(0) and "(1)" in self.tabs.tabText(2), "탭 이름 · 개수")
    self.tabs.setCurrentIndex(2)
    check(not self.open_btn.isEnabled(), "파일 없는 곡은 열 수 없음")
    self.tabs.setCurrentIndex(1)
    tree = self.trees["split"]
    tree.topLevelItem(0).setSelected(True)
    self.open_picked()
    return 1


lc.LibraryCheckDialog.exec = run
w.library_check()
for _ in range(200):
    app.processEvents()
    if w.model.files:
        break
    time.sleep(0.02)
check(sorted(f.filename for f in w.model.files) == ["01 곡 1.mp3", "02 곡 2.flac", "03 곡 3.m4a", "새 파일.ogg"],
      f"갈라진 앨범의 곡을 목록으로 {[f.filename for f in w.model.files]}")

# '열어서 앨범 점검으로 맞추기' → 열고 앨범 점검을 띄우고, 자동 반영이 꺼져 있어도 이번만 Music 에 반영
from localtag import albumcheck  # noqa: E402
from localtag.plan import Plan  # noqa: E402

w.apply_plans([Plan(next(f for f in w.model.files if f.filename == "03 곡 3.m4a"), changes={"albumartist": ["IU"]})],
              "갈라지게")
app.processEvents()
looked, refreshed = [], []
w._music_prelookup = lambda files: (looked.extend(f.path for f in files), {os.path.realpath(f.path): "PID" for f in files})[1]
w._music_refresh_paths = lambda paths, **kw: refreshed.append((list(paths), kw.get("known_pids")))
check(not w.settings.value("music_auto", False, bool), "자동 반영은 꺼져 있음")


def run_fix(self):
    self.tabs.setCurrentIndex(1)
    check(self.fix_btn.isVisible() or not self.isVisible(), "갈라진 앨범 탭에 맞추기 단추")
    self.fix_picked()
    return 1


seen = []


def run_check(self):
    kinds = {i.kind for r in self.reports for i in r.issues} if hasattr(self, "reports") else set()
    seen.append(kinds)
    for it, issue, cb in self.rows:
        it.setCheckState(0, Qt.Checked if issue.kind == "albumartist" else Qt.Unchecked)
    self._fix()
    return 1


from PySide6.QtCore import Qt  # noqa: E402

lc.LibraryCheckDialog.exec = run_fix
albumcheck.AlbumCheckDialog.exec = run_check
w.library_check()
for _ in range(200):
    app.processEvents()
    if refreshed:
        break
    time.sleep(0.02)
aa = {f.filename: f.get("albumartist") for f in w.model.files}
check(len(set(aa.values())) == 1, f"앨범 아티스트를 하나로 {aa}")
check(looked and refreshed, "저장 전에 곡 ID 를 찾고, 저장 뒤 Music 에 반영")
check(refreshed and all(v == "PID" for v in (refreshed[0][1] or {}).values()), "반영은 찾아 둔 ID 로 (경로로 찾지 않음)")
check(not getattr(w, "_music_once", False), "이번만 — 끝나면 다시 끔")
finish()
