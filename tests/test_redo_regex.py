"""다시 실행(⇧⌘Z / Ctrl+Y) · 일괄 변경의 '자주 쓰는 정규식' 과 도움말.

- 저장 → 실행 취소 → 다시 실행 → 실행 취소 를 오가도 태그·파일명·수정 시각이 맞는지
- 새 작업을 하면 다시 실행 기록이 비는지
- 자주 쓰는 정규식은 예시대로 바뀌고, 비슷하지만 다른 제목은 건드리지 않는지
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QDialog, QTextBrowser  # noqa: E402

from localtag import renamedialog  # noqa: E402
from localtag.plan import Plan  # noqa: E402
from localtag.rename import REGEX_HELP, REGEX_PRESETS, apply_rules, compile_rules, new_rule  # noqa: E402

app = QApplication.instance()
d = samples()
w = window()
w.load_paths([d], replace=True)
app.processEvents()


def by_path(p):
    return next(f for f in w.model.files if f.path == p)


# 1) 태그: 저장 → 취소 → 다시 → 취소
files = list(w.model.files)
orig = {f.path: (f.get("title"), os.stat(f.path).st_mtime) for f in files}
check(not w.redo_act.isEnabled() and not w.tb_redo.isEnabled(), "처음엔 다시 실행 꺼짐")
w.apply_plans([Plan(f, changes={"title": ["다시 실행"]}) for f in files], "제목 바꾸기")
app.processEvents()
saved = {f.path: os.stat(f.path).st_mtime for f in w.model.files}
w.undo()
app.processEvents()
check(all(by_path(p).get("title") == t for p, (t, _m) in orig.items()), "실행 취소 → 원래 제목")
check(w.redo_act.isEnabled() and "제목 바꾸기" in w.redo_act.text(), f"다시 실행 켜짐 ({w.redo_act.text()})")
w.redo()
app.processEvents()
check(all(by_path(p).get("title") == "다시 실행" for p in orig), "다시 실행 → 바꾼 제목")
check(all(abs(os.stat(p).st_mtime - m) < 0.01 for p, m in saved.items()), "다시 실행 → 저장 때 수정 시각")
check(not w.redo_stack and w.undo_stack[-1].label == "제목 바꾸기", "다시 실행한 작업은 다시 실행 취소 목록으로")
w.undo()
app.processEvents()
check(all(by_path(p).get("title") == t for p, (t, _m) in orig.items()), "한 번 더 실행 취소 → 원래 제목")
check(all(abs(os.stat(p).st_mtime - m) < 0.01 for p, (_t, m) in orig.items()), "원래 수정 시각")

# 2) 새 작업을 하면 다시 실행 기록이 빈다
check(bool(w.redo_stack), "다시 실행 기록 있음")
w.apply_plans([Plan(w.model.files[0], changes={"comment": ["새 작업"]})], "새 작업")
app.processEvents()
check(not w.redo_stack and not w.redo_act.isEnabled(), "새 작업 → 다시 실행 기록 비움")
w.undo()
app.processEvents()

# 3) 파일명 바꾸기도 다시 실행
folder = os.path.join(d, "아이유 - 꽃갈피")
before = sorted(os.listdir(folder))
target = [x for x in w.model.files if os.path.basename(x.folder) == "아이유 - 꽃갈피"]
w._select_rows([w.model.row_of(x) for x in target], QItemSelectionModel.ClearAndSelect)
app.processEvents()


def run(self):
    self.target.setCurrentIndex(self.target.findData("_filename"))
    self.rules = [dict(new_rule("affix"), suffix=" (다시)")]
    self._reload_list(0)
    self.refresh()
    self.accept()
    return 1


orig_exec = renamedialog.RenameDialog.exec
renamedialog.RenameDialog.exec = run
w.batch_rename()
app.processEvents()
renamed = sorted(os.listdir(folder))
check(renamed != before and all("(다시)" in n for n in renamed), "파일명 바꿈")
w.undo()
app.processEvents()
check(sorted(os.listdir(folder)) == before, "파일명 실행 취소")
w.redo()
app.processEvents()
check(sorted(os.listdir(folder)) == renamed, "파일명 다시 실행")
check(all(os.path.exists(f.path) for f in w.model.files), "목록의 경로도 새 이름")
w.undo()
app.processEvents()
check(sorted(os.listdir(folder)) == before, "다시 실행 뒤 실행 취소")

# 4) 자주 쓰는 정규식: 예시대로
for name, find, repl, src, want in REGEX_PRESETS:
    rule = dict(new_rule("replace"), find=find, repl=repl, regex=True)
    got = apply_rules(src, compile_rules([rule]))
    check(got == want, f"{name}: {src!r} → {got!r}")


def run_preset(name, src):
    p = next(x for x in REGEX_PRESETS if x[0] == name)
    return apply_rules(src, compile_rules([dict(new_rule("replace"), find=p[1], repl=p[2], regex=True)]))


# 비슷하지만 건드리면 안 되는 제목
check(run_preset("feat. 부분 지우기", "Gift Shop") == "Gift Shop", "feat: 'Gift Shop' 그대로")
check(run_preset("영상 꼬리말 지우기 (Official MV · Lyrics …)", "Video Killed the Radio Star")
      == "Video Killed the Radio Star", "꼬리말: 괄호 없는 제목은 그대로")
check(run_preset("앞 번호 지우기 (01. · 01 - · 1_)", "1_밤편지") == "밤편지", "앞 번호: 1_")
check(run_preset("앞 번호 지우기 (01. · 01 - · 1_)", "2002") == "2002", "앞 번호: 숫자뿐인 제목 그대로")
check(run_preset("'아티스트 - 제목' → 제목만", "K-Pop Star") == "K-Pop Star", "하이픈만 있는 낱말은 그대로")
check(len(REGEX_HELP) >= 8, "도움말 표")

# 5) 창에서: 단추로 정규식 채우기 · 도움말
shown = []
QDialog.exec = lambda self: shown.append(self.findChild(QTextBrowser).toPlainText()) or 0


def run2(self):
    self.rules = [new_rule("replace")]
    self._reload_list(0)
    self._use_regex(0, REGEX_PRESETS[0][1], REGEX_PRESETS[0][2])
    app.processEvents()
    r = self.rules[0]
    check(r["find"] == REGEX_PRESETS[0][1] and r["regex"], "단추로 찾을 말 채우고 정규식 켬")
    edits = [self.editor_form.itemAt(i).widget() for i in range(self.editor_form.count())]
    texts = [e.text() for e in edits if hasattr(e, "setPlaceholderText") and hasattr(e, "text")]
    check(REGEX_PRESETS[0][1] in texts, "편집 칸에도 보임")
    self._regex_help()
    check(shown and "정규식" in shown[-1] and "$1" in shown[-1], "도움말 창")
    self.reject()
    return 0


renamedialog.RenameDialog.exec = run2
w.batch_rename()
app.processEvents()
renamedialog.RenameDialog.exec = orig_exec
finish()
