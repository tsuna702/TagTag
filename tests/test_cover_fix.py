"""커버를 바꾼 곡만 Music 의 커버를 확인해 다르면 다시 지정 · 맞춤 여백 색(검정 기본 / 흰색 / 투명 PNG).

Music 은 흉내만 낸다 (실제 Music 에 묻거나 쓰지 않는다).
실행: python tests/test_cover_fix.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from localtag import artwork as art, music  # noqa: E402
from localtag.osutil import IS_MAC  # noqa: E402
from localtag.plan import Plan  # noqa: E402

app = QApplication.instance()


def jpeg(w, h, color):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPEG", 95)
    return bytes(ba)


# ---- 맞춤 여백: 16:9 그림을 정사각형으로
wide = jpeg(320, 180, "#3366cc")
for fit, want in (("contain", (0, 0, 0)), ("contain_white", (255, 255, 255))):
    pic = art.process(wide, 500, fit)
    img = QImage.fromData(pic.data)
    corner = QColor(img.pixel(5, 5)).getRgb()[:3]
    check(pic.mime == "image/jpeg" and all(abs(a - b) < 8 for a, b in zip(corner, want)), f"{fit}: 여백 {corner}")
clear = art.process(wide, 500, "contain_clear")
img = QImage.fromData(clear.data)
check(clear.mime == "image/png" and img.hasAlphaChannel() and QColor(img.pixelColor(5, 5)).alpha() == 0
      and QColor(img.pixelColor(250, 250)).alpha() == 255, "투명 여백: PNG, 여백만 투명")
check(art.FITS["contain"].endswith("검은 여백"), "예전 '비율 유지 · 여백' 설정은 이제 검은 여백")

# ---- music.fix_artwork: 다른 곡만 다시 지정
sent = []
music._osascript = lambda script, *args, timeout=60: (sent.append(args), "1")[1]
same, diff = b"\xff\xd8same", b"\xff\xd8new"
fixed = music.fix_artwork([("AAAA", "/a.m4a", same), ("BBBB", "/b.m4a", diff), ("", "/c.m4a", diff)],
                          cached={"AAAA": same, "BBBB": b"\x89PNGold"})
check(fixed == ["/b.m4a"] and len(sent) == 1 and sent[0][0] == "BBBB", f"Music 커버가 다른 곡만 {fixed} {sent}")
check(not os.path.exists(sent[0][1]), "보낸 그림 파일은 지움")
sent.clear()
check(music.fix_artwork([("AAAA", "/a.m4a", same)], cached={"AAAA": same}) == [] and not sent, "같으면 아무것도 안 보냄")

# ---- 저장 흐름: 커버를 바꾼 곡만 cover_pids 로 넘김
d = samples()
w = window()
w.load_paths([os.path.join(d, "아이유 - 꽃갈피")], replace=True)
app.processEvents()
got = []
w._music_refresh_paths = lambda paths, **kw: got.append((sorted(os.path.basename(p) for p in paths),
                                                          sorted(os.path.basename(p) for p in (kw.get("cover_pids") or {}))))
w._music_prelookup = lambda files: {os.path.realpath(f.path): f"PID{i}" for i, f in enumerate(files)}
w._music_once = True  # 자동 반영 설정은 건드리지 않고 이번만
files = sorted(w.model.files, key=lambda f: f.filename)
pic = art.process(jpeg(300, 300, "#cc3333"), 0)
w.apply_plans([Plan(files[0], pictures=[pic]), Plan(files[1], changes={"title": ["제목만"]})], "커버")
w._music_once = False
if IS_MAC:  # 저장 뒤 Music 반영은 macOS 에서만
    check(got and got[0][1] == ["01 곡 1.mp3"] and len(got[0][0]) == 2, f"커버를 바꾼 곡만 커버 확인 {got}")
else:
    check(not got, "Windows: 저장 뒤 Music 반영 없음")

# ---- 반영 흐름 전체: 새로 읽은 뒤 커버를 바꾼 곡만 fix_artwork, 다시 지정하면 상태 줄에 알림
from localtag.mainwindow import MainWindow  # noqa: E402

asked = []


def fake_refresh(paths, progress=None, cancelled=lambda: False, expected=None, **kw):
    r = music.RefreshResult(len(paths))
    r.confirmed = len(paths)
    return r


music.refresh = fake_refresh
music.fix_artwork = lambda items, cached=None: (asked.extend(items), [items[0][1]])[1]
w2 = window()
w2.load_paths([os.path.join(d, "아이유 - 꽃갈피")], replace=True)
app.processEvents()
f0 = sorted(w2.model.files, key=lambda f: f.filename)[0]
MainWindow._music_refresh_paths(w2, [f0.path], modal=False, cover_pids={f0.path: "PID0"})
import time  # noqa: E402
for _ in range(200):
    app.processEvents()
    if not getattr(w2, "_music_busy", False):
        break
    time.sleep(0.02)
check(len(asked) == 1 and asked[0][0] == "PID0" and asked[0][2] == f0.pictures[0].data, "파일 속 커버로 확인")
check("커버를 Music 에 다시 지정 1곡" in w2.statusBar().currentMessage(), f"알림 {w2.statusBar().currentMessage()}")
finish()
