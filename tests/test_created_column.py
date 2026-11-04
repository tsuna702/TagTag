"""목록의 '생성된 날짜' 열: 파일이 생성된 시각을 보여 주고, 태그를 저장해도 그대로다."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import app, check, finish, samples, window  # noqa: E402, F401

from PySide6.QtCore import Qt  # noqa: E402

from localtag.model import COLUMN_KEYS, COLUMNS  # noqa: E402
from localtag.tagio import AudioFile, Options  # noqa: E402

check(COLUMN_KEYS[-1] == "btime", "새 열은 끝에 (저장해 둔 열 배치가 어긋나지 않게)")
d = samples()
path = os.path.join(d, "아이유 - 꽃갈피", "03 곡 3.m4a")
later = time.time() + 86400 * 2
os.utime(path, (later, later))  # 수정한 날짜만 다르게
st = os.stat(path)
born = st.st_birthtime if hasattr(st, "st_birthtime") else st.st_ctime

w = window()
col = COLUMN_KEYS.index("btime")
check(w.table.horizontalHeader().isSectionHidden(col), "수정한 날짜처럼 기본으로는 숨김")
w.load_paths([path], replace=True)
f = next(x for x in w.model.files if x.path == path)
check(abs(f.btime - born) < 1, f"생성 시각을 읽음 {f.btime} {born}")
row = w.model.files.index(f)
shown = w.model.data(w.model.index(row, col), Qt.DisplayRole)
check(shown == time.strftime("%Y-%m-%d %H:%M", time.localtime(born)), f"표시 {shown}")
mcol = COLUMN_KEYS.index("mtime")
check(shown != w.model.data(w.model.index(row, mcol), Qt.DisplayRole), "수정한 날짜와 따로")
check(COLUMNS[col][3](f) == f.btime, "생성된 날짜로 정렬")

opts = Options()
saved = f.save({"title": ["새 제목"]}, None, opts)
check(abs(saved.btime - born) < 1, f"저장해도 생성 시각은 그대로 {saved.btime} {born}")
st2 = os.stat(path)
born2 = st2.st_birthtime if hasattr(st2, "st_birthtime") else st2.st_ctime
check(abs(born2 - born) < 1 and abs(AudioFile.load(path, opts).btime - born) < 1, "파일에서도 그대로")
finish()
