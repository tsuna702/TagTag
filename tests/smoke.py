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
finish()
