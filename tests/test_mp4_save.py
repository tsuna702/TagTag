"""m4a 저장: 항목 순서 계산을 빠르게 바꾼 뒤에도 커버·태그가 그대로 저장되는지 (mutagen _item_sort_key 패치)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, samples  # noqa: E402

import mutagen  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402

from localtag import tagio  # noqa: E402
from localtag.tagio import AudioFile, Options, Picture  # noqa: E402

d = samples()
path = os.path.join(d, "아이유 - 꽃갈피", "03 곡 3.m4a")
opts = Options()
big = os.urandom(300 * 1024)
jpeg = b"\xff\xd8\xff\xe0" + big
png = b"\x89PNG\r\n\x1a\n" + big[:1000]
f = AudioFile.load(path, opts)
f.save({"title": ["새 제목"], "comment": ["메모"], "LYRICIST": ["작사"]},
       [Picture(jpeg, "image/jpeg", 3, ""), Picture(png, "image/png", 0, "")], opts)
g = AudioFile.load(path, opts)
check(g.get("title") == "새 제목" and g.get("comment") == "메모" and g.get("LYRICIST") == "작사", "태그 저장")
check([p.data for p in g.pictures] == [jpeg, png], "커버 두 장 그대로 (순서·내용)")
keys = list(MP4(path).tags.keys())
check(keys.index("covr") > keys.index("©nam"), f"항목 순서는 iTunes 순서 그대로 {keys}")
check(mutagen.mp4._item_sort_key is tagio._item_sort_key, "패치가 걸려 있음")
check(tagio._item_sort_key("covr", ["x" * 10]) == tagio._mp4_sort_key("covr", ()), "covr 는 값과 상관없는 순서")
check(tagio._item_sort_key("©nam", "a") == tagio._mp4_sort_key("©nam", "a"), "다른 항목은 mutagen 과 같음")
finish()
