"""Music 보관함 점검: 커버 없는 곡 · 갈라진 앨범 · 파일 없는 곡 (가짜 보관함 — 실제 Music 은 읽지도 않음).

실행: python tests/test_librarycheck.py
"""
import os
import sys

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
    import time
    time.sleep(0.02)
check(sorted(f.filename for f in w.model.files) == ["01 곡 1.mp3", "02 곡 2.flac", "03 곡 3.m4a", "새 파일.ogg"],
      f"갈라진 앨범의 곡을 목록으로 {[f.filename for f in w.model.files]}")
finish()
