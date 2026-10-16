"""형식 변환 (macOS afconvert · Windows ffmpeg): FLAC → ALAC 는 같은 소리(디코딩 결과 같음)·태그·커버 그대로, 손실 → ALAC 는 건너뜀,
원본 휴지통 옮기기는 시험하지 않고 '그대로 두기'만 (실제 휴지통을 건드리지 않게).

실행: python tests/test_formatconv.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag import formatconv as fc, music  # noqa: E402
from localtag.osutil import IS_MAC  # noqa: E402

if not fc.available():
    print("변환 프로그램 없음 (macOS afconvert · Windows ffmpeg) — 건너뜀")
    finish()
    sys.exit(0)

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
w = window()
w.load_paths([album], replace=True)
app.processEvents()
by = {f.filename: f for f in w.model.files}
flac = by["02 곡 2.flac"]
flac_tags = {k: v for k, v in flac.tags.items() if v}
check(flac.pictures, "원본 FLAC 에 커버 있음")

jobs = fc.plan(list(w.model.files), "alac")
why = {j.f.filename: (j.skip, os.path.basename(j.dst)) for j in jobs}
check(why["02 곡 2.flac"] == ("", "02 곡 2.m4a") or why["02 곡 2.flac"] == ("", "02 곡 2 (ALAC).m4a"),
      f"FLAC → .m4a {why['02 곡 2.flac']}")
check("음질이 좋아지지 않음" in why["01 곡 1.mp3"][0], f"MP3 → ALAC 는 건너뜀 {why['01 곡 1.mp3']}")
aac_jobs = {j.f.filename: j for j in fc.plan(list(w.model.files), "aac256")}
check(not aac_jobs["01 곡 1.mp3"].skip, "MP3 → AAC 는 됨")


added = []
music.add_files = lambda paths: (added.extend(paths), len(paths))[1]  # 실제 Music 에는 넣지 않는다


def run(self):
    check("2곡" in self.ok_btn.text() or "1곡" in self.ok_btn.text(), f"단추 {self.ok_btn.text()}")
    if IS_MAC:
        check(self.add_music.isChecked(), "Music 에 추가는 기본 켬")
    else:
        check(self.add_music.isHidden() and not self.add_music.isChecked(), "Windows: Music 에 추가 칸 없음")
    self.orig_box.setCurrentIndex(self.orig_box.findData("keep"))
    self._accept()
    return 1


fc.FormatConvertDialog.exec = run
from PySide6.QtWidgets import QMessageBox  # noqa: E402
msgs = []
QMessageBox.information = lambda *a, **k: msgs.append(a[2])
QMessageBox.warning = lambda *a, **k: msgs.append("경고 " + a[2])
w.table.clearSelection()
w.select_files([flac])
w.convert_formats()
for _ in range(600):
    app.processEvents()
    if msgs:
        break
    import time
    time.sleep(0.05)
check(msgs and msgs[0].startswith("1곡을 바꿨습니다") and (("Music 보관함에 1곡을 추가" in msgs[0]) == IS_MAC),
      f"결과 {msgs}")
out = next(p for p in (os.path.join(album, "02 곡 2.m4a"), os.path.join(album, "02 곡 2 (ALAC).m4a")) if os.path.exists(p))
check(os.path.exists(flac.path), "원본은 그대로")
check(added == ([out] if IS_MAC else []), f"새 파일만 Music 에 추가 (Windows 는 추가 안 함) {added}")
new = next(f for f in w.model.files if f.path == out)
check(new.codec == "ALAC" and new.bits == flac.bits, f"ALAC {new.bits}bit (원본 {flac.bits}bit)")
check(new in w.selected_files(), "새 파일을 목록에 넣고 고름")
got = {k: v for k, v in new.tags.items() if v}
check(all(got.get(k) == v for k, v in flac_tags.items()), f"태그 그대로 {flac_tags} → {got}")
if not IS_MAC:  # ffmpeg 가 자기 이름(Lavf…)을 태그로 넣지 않게
    check(set(got) == set(flac_tags), f"변환 프로그램이 넣은 태그 없음 {set(got) - set(flac_tags)}")
check([p.data for p in new.pictures] == [p.data for p in flac.pictures], "커버 그대로")
check(not [n for n in os.listdir(album) if n.startswith(".localtag-conv-")], "작업 사본 남지 않음")


def pcm(p):
    if not IS_MAC:  # ffmpeg 로 32비트 PCM 으로 풀어서
        r = subprocess.run([fc.encoder(), "-nostdin", "-loglevel", "error", "-i", p, "-f", "s32le", "-c:a", "pcm_s32le", "-"],
                           capture_output=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout
    tmp = p + ".wav"
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16", p, tmp], check=True)
    with open(tmp, "rb") as fh:
        data = fh.read()[44:]
    os.remove(tmp)
    return data


check(pcm(flac.path) == pcm(out), "무손실: 디코딩한 소리가 바이트 단위로 같음")

# Music 에 추가를 끄면 부르지 않음 (AAC 로 한 번 더)
added.clear()
msgs.clear()


def run_off(self):
    self.target_box.setCurrentIndex(self.target_box.findData("aac256"))
    self.add_music.setChecked(False)
    self._accept()
    return 1


fc.FormatConvertDialog.exec = run_off
w.select_files([flac])
w.convert_formats()
for _ in range(600):
    app.processEvents()
    if msgs:
        break
    time.sleep(0.05)
check(msgs and "Music" not in msgs[0] and not added, f"Music 에 추가 끔 → 부르지 않음 {msgs}")
if IS_MAC:
    check(not w.settings.value("convert/add_music", True, bool), "끈 것을 기억")

# Windows: ffmpeg 가 없으면 설치 방법(winget 한 줄)을 알려 준다
if not IS_MAC:
    msgs.clear()
    fc.encoder = lambda: None
    w.select_files([flac])
    w.convert_formats()
    check(msgs and fc.FFMPEG_INSTALL in msgs[0], f"ffmpeg 없음 → 설치 안내 {msgs}")
finish()
