"""액션과 액션 그룹 (Mp3tag의 Actions).

액션은 {"type": ..., 매개변수...} 형태의 dict이고, 액션 그룹은 이름과 액션 목록이다.
그룹은 ~/Library/Application Support/LocalTag/actions.json 에 저장된다.
"""
from __future__ import annotations

import copy
import dataclasses
import glob
import json
import os
import re

from .converters import build_target_path, check_conflicts, make_value_filter
from .plan import Plan
from .script import Context, Pattern, ScriptError, _caps, evaluate, split_values
from .tagio import FIELD_LABELS, FIELDS, SEP, AudioFile

ALL = "_all"
FILENAME = "_filename"
COVER = "_cover"

ACTION_TYPES = {
    "replace": "바꾸기",
    "regex": "정규식으로 바꾸기",
    "case": "대소문자 변환",
    "format": "값 포맷",
    "remove_fields": "필드 제거",
    "keep_fields": "선택한 필드만 남기기",
    "split": "구분자로 여러 값 나누기",
    "merge": "여러 값을 하나로 합치기",
    "guess": "값 추측",
    "import_cover": "파일에서 커버 가져오기",
    "export_cover": "커버를 파일로 내보내기",
    "remove_cover": "커버 제거",
}

CASE_MODES = {
    "lower": "모두 소문자",
    "upper": "모두 대문자",
    "sentence": "첫 글자만 대문자",
    "words": "단어마다 첫 글자 대문자",
}

DEFAULTS = {
    "replace": {"field": ALL, "find": "", "replace": "", "case": False, "word": False},
    "regex": {"field": ALL, "pattern": "", "replace": "", "case": False},
    "case": {"field": "title", "mode": "words", "exceptions": "feat. vs. DJ MC"},
    "format": {"field": "title", "format": "%title%"},
    "remove_fields": {"fields": ["comment"]},
    "keep_fields": {"fields": ["title", "artist", "album", "albumartist", "year", "track", "disc", "genre", COVER]},
    "split": {"field": "artist", "sep": "; "},
    "merge": {"field": "artist", "sep": "; "},
    "guess": {"source": "%title%", "pattern": "%artist% - %title%"},
    "import_cover": {"filename": "cover.jpg", "replace": True},
    "export_cover": {"filename": "cover", "overwrite": False},
    "remove_cover": {},
}


def field_label(name: str) -> str:
    return {ALL: "모든 필드", FILENAME: "파일명", COVER: "커버"}.get(name, FIELD_LABELS.get(name, name))


def new_action(kind: str) -> dict:
    a = {"type": kind}
    a.update(copy.deepcopy(DEFAULTS[kind]))
    return a


def describe(a: dict) -> str:
    t = a.get("type")
    name = ACTION_TYPES.get(t, t)
    f = field_label(a.get("field", ""))
    if t == "replace":
        return f"{name} [{f}]: '{a['find']}' → '{a['replace']}'"
    if t == "regex":
        return f"{name} [{f}]: {a['pattern']} → {a['replace']}"
    if t == "case":
        return f"{name} [{f}]: {CASE_MODES.get(a['mode'], a['mode'])}"
    if t == "format":
        return f"{name} [{f}]: {a['format']}"
    if t in ("remove_fields", "keep_fields"):
        return f"{name}: " + ", ".join(field_label(x) for x in a.get("fields", []))
    if t in ("split", "merge"):
        return f"{name} [{f}]: '{a['sep']}'"
    if t == "guess":
        return f"{name}: {a['source']} → {a['pattern']}"
    if t in ("import_cover", "export_cover"):
        return f"{name}: {a['filename']}"
    return name


# ------------------------------------------------------------------ 실행

class Work:
    """액션을 차례로 적용하는 동안의 작업 상태."""

    def __init__(self, af: AudioFile):
        self.af = af
        self.tags = {k: list(v) for k, v in af.tags.items()}
        self.pictures = list(af.pictures)
        self.pics_changed = False
        self.new_path = None
        self.exports = []

    def view(self) -> AudioFile:
        return dataclasses.replace(self.af, tags=self.tags, pictures=self.pictures,
                                   path=self.new_path or self.af.path)

    def targets(self, field):
        return list(self.tags) if field == ALL else [field]

    def map_values(self, field, fn):
        for k in self.targets(field):
            if k in self.tags:
                self.tags[k] = [fn(v) for v in self.tags[k]]


def _apply_case(s: str, mode: str, exceptions: list[str]) -> str:
    if mode == "lower":
        out = s.lower()
    elif mode == "upper":
        out = s.upper()
    elif mode == "sentence":
        out = s[:1].upper() + s[1:].lower()
    else:
        out = _caps(s, True)
    for w in exceptions:
        out = re.sub(r"(?<!\w)" + re.escape(w) + r"(?!\w)", lambda _m, w=w: w, out, flags=re.IGNORECASE)
    return out


def _load_cover(path):
    from .panel import picture_from_file  # Qt 이미지 변환이 필요
    return picture_from_file(path)


def apply_action(w: Work, a: dict, index: int, total: int):
    t = a["type"]
    if t == "replace":
        find = a.get("find", "")
        if not find:
            return
        flags = 0 if a.get("case") else re.IGNORECASE
        pat = re.escape(find)
        if a.get("word"):
            pat = r"(?<!\w)" + pat + r"(?!\w)"
        rx = re.compile(pat, flags)
        repl = a.get("replace", "")
        w.map_values(a["field"], lambda v: rx.sub(lambda _m: repl, v))
    elif t == "regex":
        try:
            rx = re.compile(a.get("pattern", ""), 0 if a.get("case") else re.IGNORECASE)
        except re.error as e:
            raise ScriptError(f"정규식 오류: {e}") from e
        repl = re.sub(r"\$(\d)", r"\\\1", a.get("replace", ""))
        w.map_values(a["field"], lambda v: rx.sub(repl, v))
    elif t == "case":
        exc = a.get("exceptions", "").split()
        w.map_values(a["field"], lambda v: _apply_case(v, a.get("mode", "words"), exc))
    elif t == "format":
        field = a["field"]
        if field == FILENAME:
            vf = make_value_filter(False)
            result = evaluate(a.get("format", ""), Context(w.view(), index, total, value_filter=vf))
            w.new_path = build_target_path(w.view(), result, False)
        else:
            vals = split_values(evaluate(a.get("format", ""), Context(w.view(), index, total)))
            if vals:
                w.tags[field] = vals
            else:
                w.tags.pop(field, None)
    elif t == "remove_fields":
        for k in a.get("fields", []):
            if k == COVER:
                if w.pictures:
                    w.pictures, w.pics_changed = [], True
            else:
                w.tags.pop(k, None)
    elif t == "keep_fields":
        keep = set(a.get("fields", []))
        for k in list(w.tags):
            if k not in keep:
                del w.tags[k]
        if COVER not in keep and w.pictures:
            w.pictures, w.pics_changed = [], True
    elif t == "split":
        sep, k = a.get("sep", ""), a["field"]
        if sep and k in w.tags:
            w.tags[k] = [p.strip() for v in w.tags[k] for p in v.split(sep) if p.strip()]
    elif t == "merge":
        k = a["field"]
        if k in w.tags and len(w.tags[k]) > 1:
            w.tags[k] = [a.get("sep", "; ").join(w.tags[k])]
    elif t == "guess":
        src = evaluate(a.get("source", ""), Context(w.view(), index, total))
        m = Pattern(a.get("pattern", "")).match(src)
        if m:
            for k, v in m.items():
                if v:
                    w.tags[k] = [v]
    elif t == "import_cover":
        rel = evaluate(a.get("filename", ""), Context(w.view(), index, total))
        folder = os.path.dirname(w.af.path)
        pattern = rel if os.path.isabs(rel) else os.path.join(folder, rel)
        matches = sorted(glob.glob(glob.escape(os.path.dirname(pattern)) + "/" + os.path.basename(pattern)))
        if not matches:
            return
        if w.pictures and not a.get("replace", True):
            return
        pic = _load_cover(matches[0])
        idx = next((i for i, p in enumerate(w.pictures) if p.type == 3), 0 if w.pictures else -1)
        if idx >= 0:
            w.pictures[idx] = pic
        else:
            w.pictures.insert(0, pic)
        w.pics_changed = True
    elif t == "export_cover":
        view = w.view()
        pic = view.cover
        if pic is None:
            return
        rel = evaluate(a.get("filename", ""), Context(view, index, total))
        if not rel.strip():
            return
        ext = ".png" if pic.mime == "image/png" else ".jpg"
        path = rel if os.path.isabs(rel) else os.path.join(os.path.dirname(w.af.path), rel)
        path += ext
        if any(p == path for p, _ in w.exports):
            return
        if os.path.exists(path) and not a.get("overwrite"):
            return
        w.exports.append((path, pic.data))
    elif t == "remove_cover":
        if w.pictures:
            w.pictures, w.pics_changed = [], True


def run_actions(actions: list[dict], files: list[AudioFile]) -> list[Plan]:
    plans = []
    seen_exports = set()
    for i, f in enumerate(files):
        w = Work(f)
        p = Plan(f, cells=[f.filename])
        try:
            for a in actions:
                apply_action(w, a, i, len(files))
        except (ScriptError, ValueError, OSError) as e:
            p.error = str(e)
            p.cells.append("")
            plans.append(p)
            continue
        old = f.tags
        keys = list(dict.fromkeys(list(old) + list(w.tags)))
        p.changes = {k: w.tags.get(k, []) for k in keys if w.tags.get(k, []) != old.get(k, [])}
        if w.pics_changed:
            p.pictures = w.pictures
        if w.new_path and w.new_path != f.path:
            p.new_path = w.new_path
        # 같은 폴더의 여러 파일이 같은 커버 파일로 내보내는 경우 한 번만
        p.exports = [(path, d) for path, d in w.exports if path not in seen_exports]
        seen_exports.update(path for path, _ in p.exports)
        p.unchanged = not p.actionable
        p.cells.append(p.describe())
        plans.append(p)
    check_conflicts(plans)
    return plans


# ------------------------------------------------------------------ 저장소

def _default_groups():
    return [
        {"name": "단어 첫 글자 대문자 (제목·아티스트·앨범)", "actions": [
            {"type": "case", "field": "title", "mode": "words", "exceptions": "feat. vs. DJ MC"},
            {"type": "case", "field": "artist", "mode": "words", "exceptions": "feat. vs. DJ MC"},
            {"type": "case", "field": "album", "mode": "words", "exceptions": "feat. vs. DJ MC"},
        ]},
        {"name": "밑줄을 공백으로, 앞뒤 공백 정리", "actions": [
            {"type": "replace", "field": ALL, "find": "_", "replace": " ", "case": False, "word": False},
            {"type": "regex", "field": ALL, "pattern": r"\s{2,}", "replace": " ", "case": False},
            {"type": "regex", "field": ALL, "pattern": r"^\s+|\s+$", "replace": "", "case": False},
        ]},
        {"name": "트랙 번호 두 자리로", "actions": [
            {"type": "format", "field": "track", "format": "$num(%track%,2)"},
        ]},
        {"name": "제목의 (feat. …) 를 아티스트로 옮기기", "actions": [
            {"type": "format", "field": "artist",
             "format": "$if($neql($strstr(%title%,'(feat. '),0),%artist% feat. "
                       "$regexp(%title%,'^.*\\(feat\\. (.*)\\)$',$1),%artist%)"},
            {"type": "regex", "field": "title", "pattern": r"\s*\(feat\. .*\)$", "replace": "", "case": False},
        ]},
        {"name": "폴더의 cover.jpg 를 커버로", "actions": [
            {"type": "import_cover", "filename": "cover.jpg", "replace": True},
        ]},
    ]


class ActionStore:
    def __init__(self, path: str):
        self.path = path
        self.groups: list[dict] = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.groups = json.load(f).get("groups", [])
        except FileNotFoundError:
            self.groups = _default_groups()
            self.save()
        except (OSError, ValueError):
            self.groups = []

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"groups": self.groups}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
