"""YouTube 업로드 순서로 번호 매기기: 제목·[영상ID]로 짝짓고 날짜 순서대로 (인터넷 대신 가짜 응답).

실행: python tests/test_yt_order.py
"""
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import QApplication, check, finish, samples, window  # noqa: E402

from localtag import tagio  # noqa: E402
from localtag import ytorder as yo  # noqa: E402

app = QApplication.instance()
d = samples()
album = os.path.join(d, "아이유 - 꽃갈피")
os.rename(os.path.join(album, "새 파일.ogg"), os.path.join(album, "새 파일 [AbCdEfGh_12].ogg"))
w = window()
w.load_paths([album], replace=True)
app.processEvents()
by = lambda: {f.filename: f for f in w.model.files}  # noqa: E731
titles = {n: yo.file_title(f) for n, f in by().items()}
print(titles)

# ---- 낱말 맞추기 (인터넷 없음)
check(yo.variants("ADAMAS / Eurobeat Remix") >= {"adamas"}, "' / ' 뒤 꼬리말을 뗀 조각")
check("belovedtomboyishgirl" in yo.variants("【東方Eurobeat】 Beloved Tomboyish Girl 「TurboAutism」 【No Vocals, lol】"),
      "【】·「」 속 군더더기 빼기")
check(yo.variants("Bon Appétit♡S / Eurobeat Remix") & yo.variants("Bon Appetit S [Eurobeat Remix]"), "악센트·기호 무시")
check(yo.parse_date("2019.08.14 20:01").hour == 20 and yo.parse_date("2019-13-01") is None, "날짜 직접 입력 읽기")


class F:
    def __init__(self, stem, title="", track="", disc="", length=0.0, **tags):
        self.stem, self.length = stem, length
        self.tags = {"title": [title]} if title else {}
        self.tags.update({k: [v] for k, v in tags.items()})
        if track:
            self.tags["track"] = [track]
        if disc:
            self.tags["disc"] = [disc]

    def get(self, name):
        return "; ".join(self.tags.get(name, []))


check(yo.file_video_id(F("Song [dQw4w9WgXcQ]")) == "dQw4w9WgXcQ", "파일 이름의 [영상ID]")
check(yo.file_video_id(F("Song [AbCdEfGhIjK]")) == "AbCdEfGhIjK" and yo.file_video_id(F("Song [EurobeatMix]")) is None,
      "낱말 괄호는 영상 ID 로 보지 않음")
check(yo.file_video_id(F("x", comment="from https://youtu.be/dQw4w9WgXcQ")) == "dQw4w9WgXcQ", "태그 안의 YouTube 주소")

vids = [yo.Video(f"vid{n:08d}", t, s) for n, (t, s) in enumerate([
    ("곡 3 / Eurobeat Remix", 0), ("【MV】 곡 1 「cover」", 0), ("곡 2 / Eurobeat Remix", 0),
    ("ADAMAS / Eurobeat Remix", 265), ("Awake / Eurobeat Remix", 200), ("Dota / Eurobeat Remix", 180),
    ("Silhouette / Eurobeat Remix", 210)])]
fs = [F("a", "ADAMAS [Eurobeat Remix]", length=264.7), F("b", "Dota [Eurobeat Remix]", length=300),
      F("c", "Kiss Of Death [Eurobeat Remix]")]
pairs, scores = yo.match(fs, vids)
check(pairs[0] == vids[3].id and scores[0] >= yo.SURE, f"제목·길이가 같으면 확실한 짝 {scores[0]:.2f}")
check(pairs[1] == vids[5].id and scores[1] < yo.SURE, f"길이가 많이 다르면 확실하지 않은 짝 (주황) {scores[1]:.2f}")
check(pairs[2] is None, "없는 곡은 짝 없음 (채널 꼬리말 'Eurobeat Remix' 만으로는 짝짓지 않음)")

# ---- 번호: 날짜 순서 · 연도마다 디스크 · 날짜 없는 곡
t = lambda *a: datetime(*a).astimezone()  # noqa: E731
fs = [F("w", "w", "1"), F("x", "x", "2"), F("y", "y", "3"), F("z", "z", "4")]
dates = [t(2020, 1, 1), t(2019, 5, 1, 9), None, t(2019, 5, 1, 8)]
check(yo.assign(fs, dates, "total", "keep", "keep") == [(None, "3/3"), (None, "2/3"), (None, None), (None, "1/3")],
      "날짜·시각 순서, 날짜 없는 곡은 그대로")
check(yo.assign(fs, dates, "plain", "year", "end") == [("2", "1"), ("1", "2"), ("3", "1"), ("1", "1")],
      "연도마다 디스크, 날짜 없는 곡은 맨 뒤 디스크")
check(yo.assign(fs, dates, "total", "clear", "keep")[0] == ("", "3/3"), "디스크 지우기")

# ---- 창: 채널 불러오기 → 짝 → 날짜 → 적용 → 실행 취소
DATES = {"vid00000000": t(2019, 1, 1, 12), "vid00000001": t(2020, 5, 5), "vid00000002": t(2019, 6, 1),
         "AbCdEfGh_12": t(2018, 3, 3), "Manual12345": t(2016, 2, 2)}
asked = []
yo.ex.youtube_channel = lambda url: (asked.append(url), ("Turbo", [(v.id, v.title, v.seconds) for v in vids]))[1]


def fake_info(vid):
    asked.append(vid)
    if vid not in DATES:
        raise yo.OrderError(f"{vid} 없음")
    yo._DATES[vid] = DATES[vid]
    yo._INFO[vid] = yo.Video(vid, f"영상 {vid}")
    return yo._INFO[vid]


yo.video_info = fake_info
w.settings.setValue("ytorder/disc", "year")
dlg = yo.YtOrderDialog(w, list(w.model.files), w.settings)


def wait():
    for _ in range(300):
        app.processEvents()
        if not getattr(dlg, "_tasks", None):
            break
        time.sleep(0.02)
    app.processEvents()


ogg = next(i for i, f in enumerate(dlg.files) if f.ext == ".ogg")
check(dlg.pairs[ogg] == "AbCdEfGh_12" and dlg.how[ogg] == "파일", "열자마자 파일 이름의 영상 ID 를 씀 (인터넷은 아직)")
check(not asked, "불러오기 전에는 인터넷을 쓰지 않음")
dlg.url.setText("https://www.youtube.com/@TurboA/videos")
dlg.load()
wait()
check(asked[0] == "https://www.youtube.com/@TurboA", f"채널 주소 읽기 {asked[:1]}")
check(set(asked[1:]) == {"vid00000000", "vid00000001", "vid00000002", "AbCdEfGh_12"}, f"짝지은 영상의 날짜만 받음 {asked[1:]}")
res = {dlg.files[i].ext: r for i, r in enumerate(dlg.result())}
check(res == {".ogg": ("1/3", "1/1"), ".m4a": ("2/3", "1/2"), ".flac": ("2/3", "2/2"), ".mp3": ("3/3", "1/1")},
      f"업로드 순서 · 연도마다 디스크 {res}")
check([dlg.files[i].ext for i in dlg.rows] == [".ogg", ".m4a", ".flac", ".mp3"], "표는 새 순서로")

# 업로드 칸에 영상 링크를 넣으면 그 영상으로, 날짜를 넣으면 그 날짜로
mp3_row = next(r for r, i in enumerate(dlg.rows) if dlg.files[i].ext == ".mp3")
dlg.table.item(mp3_row, 3).setText("https://youtu.be/Manual12345")
wait()
mp3 = dlg.rows[mp3_row]
check(dlg.pairs[mp3] == "Manual12345" and dlg.how[mp3] == "직접", "업로드 칸에 영상 링크 → 그 영상의 날짜")
check(dlg.result()[mp3] == ("1/3", "1/1"), f"가장 이른 날짜라 맨 앞 디스크 {dlg.result()[mp3]}")
dlg.table.item(mp3_row, 3).setText("2021-07-07")
check(dlg.manual[mp3] and dlg.result()[mp3] == ("3/3", "1/1"), "날짜 직접 입력")
dlg.sort_new()
dlg.accept()
check(len(dlg.plans) == 4, f"네 곡 모두 바뀜 {len(dlg.plans)}")
w.apply_plans(dlg.plans, "YouTube 업로드 순서로 번호")
app.processEvents()
disk = {n: tagio.AudioFile.load(f.path, w.opts) for n, f in by().items()}
got = {f.ext: (f.get("disc"), f.get("track")) for f in disk.values()}
check(got == {".ogg": ("1/3", "1/1"), ".m4a": ("2/3", "1/2"), ".flac": ("2/3", "2/2"), ".mp3": ("3/3", "1/1")},
      f"파일에 저장 {got}")
before = {n: f.get("track") for n, f in disk.items()}
w.undo()
app.processEvents()
after = {n: tagio.AudioFile.load(f.path, w.opts).get("track") for n, f in by().items()}
check(after != before, f"실행 취소로 원래 번호 {after}")

finish()
