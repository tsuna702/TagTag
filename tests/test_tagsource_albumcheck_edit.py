"""목록 칸에서 고치기 · 앨범 점검 · Apple Music 에서 태그 가져오기 (인터넷 없이, 가짜 앨범으로).

실행: python tests/test_tagsource_albumcheck_edit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from localtag import albumcheck, tagsource as ts  # noqa: E402
from localtag.model import COLUMN_KEYS  # noqa: E402
from localtag.tagsourcedialog import TagSourceDialog  # noqa: E402

app = QApplication.instance()
d = samples()
album_dir = os.path.join(d, "아이유 - 꽃갈피")
w = window()
w.load_paths([album_dir], replace=True)
app.processEvents()
by = lambda: {f.filename: f for f in w.model.files}  # noqa: E731


def proxy_index(name, key):
    f = by()[name]
    src = w.model.index(w.model.row_of(f), COLUMN_KEYS.index(key))
    return w.proxy.mapFromSource(src)


# ---- 4. 목록 칸에서 고치기
ix = proxy_index("01 곡 1.mp3", "title")
check(bool(ix.flags() & Qt.ItemIsEditable), "제목 칸은 고칠 수 있음")
check(not (proxy_index("01 곡 1.mp3", "bitrate").flags() & Qt.ItemIsEditable), "비트레이트 칸은 못 고침")
check(ix.data(Qt.EditRole) == "곡 1", "편집기에 지금 값")
w.proxy.setData(ix, "고친 제목")
app.processEvents()
check(by()["01 곡 1.mp3"].get("title") == "고친 제목", "칸에서 고친 제목 저장")
w.proxy.setData(proxy_index("02 곡 2.flac", "filename"), "02 이름 바꿈.flac")  # 확장자까지 쳐도 한 번만
app.processEvents()
check(os.path.exists(os.path.join(album_dir, "02 이름 바꿈.flac")), f"칸에서 파일명 바꾸기 {sorted(os.listdir(album_dir))}")
w.undo()
app.processEvents()
check(os.path.exists(os.path.join(album_dir, "02 곡 2.flac")), "파일명 되돌리기")
w.undo()
app.processEvents()
check(by()["01 곡 1.mp3"].get("title") == "곡 1", "제목 되돌리기")
played = []
w.play = lambda: played.append(1)
vp = w.table.viewport()
for key, want_edit in (("bitrate", False), ("title", True)):
    ix = proxy_index("01 곡 1.mp3", key)
    rect = w.table.visualRect(ix)
    if rect.isEmpty():  # 숨긴 열이면 잠깐 보이게
        w.table.horizontalHeader().setSectionHidden(COLUMN_KEYS.index(key), False)
        app.processEvents()
        rect = w.table.visualRect(ix)
    pos = QPointF(rect.center())
    ev = QMouseEvent(QEvent.MouseButtonDblClick, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(vp, ev)
    app.processEvents()
    editing = w.table.state() == w.table.State.EditingState
    check(editing == want_edit and (not played) == want_edit, f"더블클릭 {key}: 편집 {editing}, 재생 {bool(played)}")
    if editing:
        w.table.closePersistentEditor(ix)
        w.table.setFocus()
    played.clear()

# ---- 2. 앨범 점검 (새 파일.ogg 는 앨범 아티스트·커버가 없다)
reports = albumcheck.check_albums(list(w.model.files))
check(len(reports) == 1, f"차이 있는 앨범 1개 ({len(reports)})")
kinds = {i.kind for i in reports[0].issues}
check({"albumartist", "_cover"} <= kinds, f"앨범 아티스트·커버 차이 찾음 {kinds}")


def run_check(self):
    for it, issue, cb in self.rows:
        it.setCheckState(0, Qt.Checked if issue.kind in ("albumartist", "_cover") else Qt.Unchecked)
    self._fix()
    return 1


albumcheck.AlbumCheckDialog.exec = run_check
w.check_albums()
app.processEvents()
reports = albumcheck.check_albums(list(w.model.files))
left = {i.kind for r in reports for i in r.issues}
check(not ({"albumartist", "_cover"} & left), f"맞춘 뒤 남은 차이 {left}")
ogg = by()["새 파일.ogg"]
check(ogg.get("albumartist") == "아이유" and ogg.pictures, "새 파일.ogg 에 앨범 아티스트·커버")
w.undo()
app.processEvents()
check(not by()["새 파일.ogg"].get("albumartist"), "앨범 점검 되돌리기")

# ---- 1. Apple Music 에서 태그 가져오기 (가짜 앨범)
rel = ts.Release("1", "꽃갈피 (Remastered)", "IU", "2014", "K-Pop", 4, "", country="kr")
rel.tracks = [ts.Track(1, 1, 1, 4, "나의 옛날이야기", "IU", 127), ts.Track(1, 1, 2, 4, "꽃", "IU", 134),
              ts.Track(1, 1, 3, 4, "삐에로는 우릴 보고 웃지", "IU", 141), ts.Track(1, 1, 4, 4, "새 제목", "IU", 148)]
files = sorted(w.model.files, key=lambda f: f.filename)
w.table.clearSelection()
w._select_rows([w.proxy.mapFromSource(w.model.index(w.model.row_of(f), 0)).row() for f in files],
               QItemSelectionModel.ClearAndSelect)
app.processEvents()


def run_source(self):
    # 검색 결과 표: 검색 순서 그대로, 정렬해도 고른 줄의 앨범을 받는다
    picked = []
    self._pick_release = lambda row: picked.append(self.releases[row].id if row >= 0 else None)
    self._show_results([ts.Release("a", "나중 앨범", "IU", "2020", count=10, country="kr"),
                        ts.Release("b", "먼저 앨범", "IU", "2014", count=4, country="jp", source="mb")])
    cells = [[self.results.item(r, c).text() for c in range(6)] for r in range(2)]
    check(cells == [["나중 앨범", "IU", "2020", "10", "한국", "Apple"], ["먼저 앨범", "IU", "2014", "4", "일본", "MB"]],
          f"검색 결과 표 {cells}")
    self.results.sortByColumn(2, Qt.AscendingOrder)
    self.results.setCurrentCell(0, 0)
    check(self.results.item(0, 0).text() == "먼저 앨범" and picked[-1] == "b", f"연도로 정렬 뒤 고르기 {picked}")
    del self._pick_release
    rh = self.results.horizontalHeader()
    check(all(rh.sectionResizeMode(c) == rh.ResizeMode.Interactive for c in range(6)), "검색 결과 칸 너비 조정 가능")
    self.results.setColumnWidth(0, 321)
    self._set_release(rel)
    check(self.preview.text() == "커버 없음", "커버 없는 앨범은 '커버 없음'")
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage
    img, buf = QImage(300, 300, QImage.Format_RGB32), QBuffer()
    img.fill(0x3366cc)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    self._set_preview(bytes(buf.data()))
    pm = self.preview.pixmap()
    check(not pm.isNull() and max(pm.width(), pm.height()) <= 110, "커버 미리보기 그림")
    self.check_all(False)
    check(not any(cb.isChecked() for cb in [*self.checks.values(), self.totals, self.cover]), "모두 해제")
    self.check_all(True)
    check(all(cb.isChecked() for cb in [*self.checks.values(), self.totals, self.cover]), "모두 선택")
    names = [f.filename for f in self.files]
    pairs = dict(zip(names, self.pairs))
    # 트랙 번호·길이로 1→0, 2→1, 3→2, 새 파일(제목 같음, 4번) → 3
    check(pairs == {"01 곡 1.mp3": 0, "02 곡 2.flac": 1, "03 곡 3.m4a": 2, "새 파일.ogg": 3}, f"자동 짝 {pairs}")
    # 직접 짝 바꾸기: 1번 곡 ↔ 3번 트랙 (겹침 표시)
    i = names.index("01 곡 1.mp3")
    self.table.cellWidget(i, 1).setCurrentIndex(3)
    check(self.pairs[i] == 2 and self.table.item(i, 5).text() == "겹침", "직접 고른 짝 · 겹침 표시")
    self.table.cellWidget(i, 1).setCurrentIndex(1)
    check(self.table.item(i, 5).text() == "✓", "직접 고친 짝은 확인됨")
    # 순서대로 / 모두 풀기
    self.pair_clear()
    check(self.pairs == [None] * 4 and not self.ok_btn.isEnabled(), "짝 모두 풀기")
    self.pair_ordered()
    check(self.pairs == [0, 1, 2, 3], "순서대로 짝짓기")
    self.cover.setChecked(False)
    self.checks["genre"].setChecked(False)
    self.apply()
    return 1


TagSourceDialog.exec = run_source
w.tag_source()
app.processEvents()
again = TagSourceDialog(w, list(w.model.files), w.settings)
check(again.results.columnWidth(0) == 321, f"조정한 칸 너비 기억 ({again.results.columnWidth(0)})")
again.deleteLater()
got = {f.filename: (f.get("title"), f.get("album"), f.get("albumartist"), f.get("track")) for f in w.model.files}
check(got["02 곡 2.flac"] == ("꽃", "꽃갈피 (Remastered)", "IU", "2/4"), f"태그 적용 {got['02 곡 2.flac']}")
check(by()["02 곡 2.flac"].get("genre") == "K-Pop", "끈 필드(장르)는 그대로")
w.undo()
app.processEvents()
check(by()["02 곡 2.flac"].get("title") == "곡 2", "태그 가져오기 되돌리기")
finish()
