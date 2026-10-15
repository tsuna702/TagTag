"""핵심 기능 빠른 확인 (macOS · Windows 공통). 실행: python tests/smoke.py

저장소 samples/ 를 실행 폴더로 복사해서 쓰므로 원본 샘플은 바뀌지 않는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import QItemSelectionModel  # noqa: E402

from localtag import renamedialog  # noqa: E402
from localtag.dupes import find_groups  # noqa: E402
from localtag.filterexpr import compile_filter  # noqa: E402
from localtag.plan import Plan  # noqa: E402
from localtag.rename import new_rule  # noqa: E402
from localtag.ytclean import clean  # noqa: E402

app = QApplication.instance()
d = samples()
w = window()
w.load_paths([d], replace=True)
app.processEvents()
files = {f.filename: f for f in w.model.files}
print("   불러온 파일:", sorted(files))
check(len(files) == 5, f"샘플 5개 불러오기 ({len(files)})")
codecs = {os.path.splitext(n)[1]: f.codec for n, f in files.items()}
check(codecs.get(".mp3") == "MP3" and codecs.get(".flac") == "FLAC", f"코덱 {codecs}")

# 태그 저장 → 다시 읽기 → 되돌리기
mp3 = next(f for n, f in files.items() if n.endswith(".mp3"))
old_title = mp3.get("title")
w.apply_plans([Plan(mp3, changes={"title": ["테스트 제목"]})], "테스트")
app.processEvents()
mp3b = next(f for f in w.model.files if f.path == mp3.path)
check(mp3b.get("title") == "테스트 제목", "태그 저장 (안전 저장)")
w.undo()
app.processEvents()
mp3c = next(f for f in w.model.files if f.path == mp3.path)
check(mp3c.get("title") == old_title, "되돌리기")

# 필터
f = compile_filter("lossless PRESENT")
check({x.ext for x in w.model.files if f(x)} >= {".flac"}, "필터 lossless")

# YouTube 제목 정리 · 같은 곡 찾기 (순수 함수)
check(clean("【MV】YOASOBI - アイドル (Official Music Video) [4K]") == ("アイドル", "YOASOBI"), "YouTube 제목 정리")
check(isinstance(find_groups(list(w.model.files)), list), "같은 곡 찾기 실행")

# 일괄 변경으로 파일명 바꾸기 → 되돌리기
before = sorted(os.listdir(os.path.join(d, "아이유 - 꽃갈피")))
target = [x for x in w.model.files if os.path.basename(x.folder) == "아이유 - 꽃갈피"]
w._select_rows([w.model.row_of(x) for x in target], QItemSelectionModel.ClearAndSelect)
app.processEvents()


def run(self):
    self.target.setCurrentIndex(self.target.findData("_filename"))
    self.rules = [dict(new_rule("affix"), suffix=" (테스트)")]
    self._reload_list(0)
    self.refresh()
    self.accept()
    return 1


renamedialog.RenameDialog.exec = run
w.batch_rename()
app.processEvents()
after = sorted(os.listdir(os.path.join(d, "아이유 - 꽃갈피")))
check(all("(테스트)" in n for n in after), f"일괄 변경 파일명 {after}")
w.undo()
app.processEvents()
check(sorted(os.listdir(os.path.join(d, "아이유 - 꽃갈피"))) == before, "파일명 되돌리기")

# 운영체제별 부분
from localtag import osutil, safewrite  # noqa: E402

shown = osutil.keys("위로 (⌥⌘↑) · 보기 (⇧⌘R) · (⌥9)")
expect = "위로 (⌥⌘↑) · 보기 (⇧⌘R) · (⌥9)" if osutil.IS_MAC else "위로 (Ctrl+Alt+↑) · 보기 (Ctrl+Shift+R) · (Alt+9)"
check(shown == expect, f"단축키 표기 {shown}")

# 원본에 달라진 부분만 다시 쓰기 — 줄 끝 바이트(\r\n)가 바뀌지 않아야 한다 (Windows 텍스트 모드 방지)
src, dst = os.path.join(d, "wb-src.bin"), os.path.join(d, "wb-dst.bin")
data = bytes(range(256)) * 9000 + b"\r\n\n\r"
with open(dst, "wb") as fp:
    fp.write(data)
with open(src, "wb") as fp:
    fp.write(b"TAG" + data[3:-1])  # 앞은 바뀌고 끝은 한 바이트 짧게
safewrite.write_back(src, dst)
with open(dst, "rb") as fp:
    check(fp.read() == b"TAG" + data[3:-1], "달라진 부분만 다시 쓰기")
copy = os.path.join(d, "wb-copy.bin")
safewrite.copy_preserving(dst, copy)
check(os.path.getmtime(copy) == os.path.getmtime(dst), "복사할 때 수정 시각 유지")

# 옮겨진 파일 찾기: 앨범 폴더 이름이 바뀌어도 파일 번호로 새 위치를 찾는다 (Music 폴더 정리처럼)
album = os.path.join(d, "아이유 - 꽃갈피")
check(all(x.ino for x in w.model.files), "파일 번호 읽기")
os.rename(album, album + " 2")
w.refresh()
app.processEvents()
moved = [x.path for x in w.model.files if os.path.dirname(x.path) == album + " 2"]
check(len(moved) == 4 and len(w.model.files) == 5, f"옮겨진 파일 다시 찾기 ({len(moved)}개)")
os.rename(album + " 2", album)
w.refresh()
app.processEvents()

# 다른 프로그램이 파일을 잡고 있을 때: 원본은 그대로, 알림이 뜬다
if osutil.IS_WIN:
    import ctypes
    from ctypes import wintypes

    from PySide6.QtWidgets import QMessageBox

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    shown_msgs = []
    QMessageBox.warning = staticmethod(lambda *a, **k: shown_msgs.append(a[2]))
    for share, name in ((1, "읽기만 허용 (재생 중인 플레이어처럼)"), (0, "공유 안 함")):
        mp3 = next(x for x in w.model.files if x.path.endswith(".mp3"))
        with open(mp3.path, "rb") as fp:
            raw = fp.read()
        h = k32.CreateFileW(mp3.path, 0x80000000, share, None, 3, 0x80, None)  # GENERIC_READ, OPEN_EXISTING
        shown_msgs.clear()
        try:
            w.apply_plans([Plan(mp3, changes={"title": ["잠긴 파일"]})], "테스트")
            app.processEvents()
        finally:
            k32.CloseHandle(h)
        with open(mp3.path, "rb") as fp:
            same = fp.read() == raw
        print("   알림:", " / ".join(m.replace("\n", " ") for m in shown_msgs)[:200])
        check(same and shown_msgs and safewrite.IN_USE in shown_msgs[0], f"파일을 잡고 있을 때 원본 그대로 · 알림 — {name}")
        check(not [n for n in os.listdir(os.path.dirname(mp3.path)) if n.startswith(safewrite.STAGE_PREFIX)],
              f"작업 사본 남지 않음 — {name}")
finish()
