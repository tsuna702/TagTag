"""앨범아트 추출: 링크·YouTube 를 붙여 넣으면 먼저 미리 보고, 체크한 것만 받는다 (인터넷 대신 가짜 응답).

실행: python tests/test_extract_preview.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from localtag import extract as ex  # noqa: E402
from localtag.extractdialog import ExtractDialog  # noqa: E402

app = QApplication.instance()
out_dir = os.path.join(samples(), "추출")
w = window()


def jpeg(size, color):
    img = QImage(size, size, QImage.Format_RGB32)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPEG", 90)
    return bytes(ba)


fetched = []
album = ex.Album("꽃갈피", "아이유", [ex.Item(f"0{i}", f"곡 {i}", url=f"https://x/{i}/100x100bb.jpg") for i in (1, 2, 3)])
ex.link_album = lambda text: ex.Album(album.title, album.artist, [ex.Item(i.num, i.name, url=i.url) for i in album.items])
ex.download_item = lambda it, size: (fetched.append((it.name, size)), jpeg(min(size, 400), "#3366cc"))[1]
ex.youtube_items = lambda text, whole: (ex.Album("영상 제목", items=[ex.Item("", "영상 제목", url="VID")]), True)
ex.youtube_thumbnail = lambda vid, q: (fetched.append(("yt", q)), jpeg(480, "#cc3333"))[1]

dlg = ExtractDialog(w)
dlg.folder.setText(out_dir)
dlg.tabs.setCurrentIndex(0)


def wait():
    for _ in range(300):
        app.processEvents()
        if not dlg.job:
            break
        time.sleep(0.02)
    app.processEvents()


# 붙여 넣으면 잠시 뒤 알아서 미리 보기 (저장은 안 함)
dlg.link.setText("https://music.apple.com/kr/album/a/879281073")
dlg._preview_timer.timeout.emit()
wait()
check(dlg.results.count() == 3 and all(dlg.results.item(r).checkState() == Qt.Checked for r in range(3)),
      "미리 보기: 곡마다 그림, 모두 체크")
check(all(size == 300 for _n, size in fetched) and not os.path.exists(out_dir), f"작은 그림만 받고 저장은 안 함 {fetched}")
check(dlg.go_btn.text() == "3개 추출하기" and not dlg.pick_all.isHidden(), f"단추 {dlg.go_btn.text()}")
check(not dlg._big[0].isNull() and dlg._big[0].width() == 300, "크게 볼 그림을 가지고 있음")

# 그림을 누르면 체크 해제 → 고른 것만 받기
dlg._toggle_pick(dlg.results.item(1))
check(dlg.go_btn.text() == "2개 추출하기" and "3개 중 2개" in dlg.pick_note.text(), "누르면 해제")
fetched.clear()
dlg.start()
wait()
saved = sorted(os.listdir(os.path.join(out_dir, "아이유 - 꽃갈피")))
check(saved == ["01 곡 1.jpg", "03 곡 3.jpg"], f"체크한 것만 저장 {saved}")
check(sorted(n for n, s in fetched) == ["곡 1", "곡 3"] and all(s == 3000 or s == dlg.size.currentData() for _n, s in fetched),
      f"고른 크기로 다시 받음 {fetched}")
check(dlg.results.item(0).text().startswith("✅") and not dlg.results.item(1).text().startswith("✅"), "받은 칸만 ✅")

dlg._pick_all(False)
dlg.start()
check("하나 이상 체크" in dlg.msg.text() and not dlg.job, "하나도 안 고르면 받지 않음")

# 링크가 바뀌면 예전 미리 보기는 쓰지 않음 (전부 받기)
dlg.link.setText("https://music.apple.com/kr/album/b/1")
check(dlg._preview_for(0, dlg.link.text()) is None, "다른 링크 → 미리 보기 다시")

# YouTube: 자르기를 켜면 잘린 모양으로 미리 보기
dlg.tabs.setCurrentIndex(3)
dlg.yt_crop.setChecked(True)
fetched.clear()
dlg.yt.setText("https://youtu.be/VID")
dlg.do_preview()
wait()
img = dlg._big.get(0)
check(dlg.results.count() == 1 and img is not None and img.width() == img.height() == 300, "YouTube 미리 보기 (정사각형)")
check(fetched == [("yt", 2)], f"미리 보기는 480×360 섬네일로 {fetched}")
check(dlg.go_btn.text() == "추출하기", "한 개면 그냥 추출하기")
dlg.start()
wait()
check(os.path.exists(os.path.join(out_dir, "영상 제목.jpg")), "체크한 섬네일 저장")
finish()
