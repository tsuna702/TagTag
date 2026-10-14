"""곡명·파일명 일괄 변경 규칙 (파일 이름 일괄 변경 프로그램처럼 규칙을 위에서부터 차례로 적용).

규칙은 {"type": ..., "on": True, 매개변수...} dict 이다.
"""
from __future__ import annotations

import copy
import re

from .actions import CASE_MODES, _apply_case
from .script import Context, ScriptError, evaluate

RULE_TYPES = {
    "replace": "바꾸기",
    "affix": "앞·뒤에 붙이기",
    "insert": "끼워 넣기",
    "remove": "글자 지우기",
    "number": "번호 붙이기",
    "case": "대소문자",
    "tidy": "공백 정리",
    "width": "전각 ↔ 반각",
    "format": "새 이름 (포맷 문자열)",
}

REMOVE_MODES = {"first": "앞에서 N글자", "last": "뒤에서 N글자", "range": "M번째 글자부터 N글자",
                "before": "찾을 말 앞쪽 전부", "after": "찾을 말 뒤쪽 전부"}
WHERE = {"prefix": "앞에", "suffix": "뒤에"}
WIDTH_MODES = {"half": "전각 영숫자·기호 → 반각 (ＡＢＣ１２３ → ABC123)",
               "full": "반각 영숫자·기호 → 전각 (ABC123 → ＡＢＣ１２３)"}

DEFAULTS = {
    "replace": {"find": "", "repl": "", "regex": False, "case": False, "all": True},
    "affix": {"prefix": "", "suffix": ""},
    "insert": {"text": "", "pos": 0, "from_end": False},
    "remove": {"mode": "first", "count": 1, "pos": 1, "find": "", "keep_find": False},
    "number": {"start": 1, "step": 1, "digits": 2, "where": "prefix", "sep": " "},
    "case": {"mode": "words", "exceptions": "feat. vs. DJ MC"},
    "tidy": {"trim": True, "spaces": True, "underscore": False, "dots": False},
    "width": {"mode": "half"},
    "format": {"format": "%_value%"},
}

# 편집 칸: (키, 제목, 종류[, 선택지])  종류: text · format · bool · int · choice
FORMS = {
    "replace": [("find", "찾을 말", "text"), ("repl", "바꿀 말 (정규식이면 $1 = 첫 그룹)", "text"),
                ("regex", "정규식", "bool"), ("case", "대소문자 구분", "bool"), ("all", "모두 바꾸기 (끄면 첫 번째만)", "bool")],
    "affix": [("prefix", "앞에 붙일 말 (포맷 문자열 가능)", "format"), ("suffix", "뒤에 붙일 말", "format")],
    "insert": [("text", "끼워 넣을 말 (포맷 문자열 가능)", "format"), ("pos", "위치 (몇 글자 다음에)", "int"),
               ("from_end", "뒤에서부터 센다", "bool")],
    "remove": [("mode", "지울 곳", "choice", REMOVE_MODES), ("count", "N (글자 수)", "int"),
               ("pos", "M (몇 번째 글자부터, 1부터)", "int"), ("find", "찾을 말 (앞쪽·뒤쪽 지우기)", "text"),
               ("keep_find", "찾을 말 자체는 남기기", "bool")],
    "number": [("start", "시작 번호", "int"), ("step", "증가", "int"), ("digits", "자릿수 (앞을 0으로)", "int"),
               ("where", "붙일 곳", "choice", WHERE), ("sep", "구분 문자", "text")],
    "case": [("mode", "변환", "choice", CASE_MODES), ("exceptions", "예외 단어 (그대로 둠, 공백으로 구분)", "text")],
    "tidy": [("trim", "앞뒤 공백 지우기", "bool"), ("spaces", "연속 공백을 하나로", "bool"),
             ("underscore", "밑줄(_)을 공백으로", "bool"), ("dots", "마침표(.)를 공백으로", "bool")],
    "width": [("mode", "바꾸기", "choice", WIDTH_MODES)],
    "format": [("format", "새 이름 — %_value% 는 지금까지 바뀐 이름, %title% %artist% %track% 등", "format")],
}

INT_RANGES = {"pos": (0, 999), "count": (0, 999), "start": (0, 99999), "step": (-999, 999), "digits": (1, 6)}


def new_rule(kind: str) -> dict:
    r = {"type": kind, "on": True}
    r.update(copy.deepcopy(DEFAULTS[kind]))
    return r


def describe(r: dict) -> str:
    t = r.get("type")
    name = RULE_TYPES.get(t, t)
    if t == "replace":
        return f"{name}: '{r['find']}' → '{r['repl']}'" + (" (정규식)" if r.get("regex") else "")
    if t == "affix":
        parts = [f"앞 '{r['prefix']}'" if r.get("prefix") else "", f"뒤 '{r['suffix']}'" if r.get("suffix") else ""]
        return f"{name}: " + " · ".join(p for p in parts if p) if any(parts) else name
    if t == "insert":
        return f"{name}: '{r['text']}' {'뒤에서' if r.get('from_end') else '앞에서'} {r['pos']}글자 다음"
    if t == "remove":
        m = r.get("mode")
        if m == "first":
            return f"{name}: 앞에서 {r['count']}글자"
        if m == "last":
            return f"{name}: 뒤에서 {r['count']}글자"
        if m == "range":
            return f"{name}: {r['pos']}번째부터 {r['count']}글자"
        return f"{name}: '{r['find']}' {'앞쪽' if m == 'before' else '뒤쪽'}"
    if t == "number":
        step = "" if r["step"] == 1 else f" (+{r['step']})"
        return f"{name}: {WHERE.get(r['where'])} {str(r['start']).zfill(r['digits'])}{step}"
    if t == "case":
        return f"{name}: {CASE_MODES.get(r['mode'], '')}"
    if t == "tidy":
        bits = [lbl for k, lbl, _ in FORMS["tidy"] if r.get(k)]
        return f"{name}: " + ", ".join(bits) if bits else name
    if t == "width":
        return f"{name}: {'반각으로' if r['mode'] == 'half' else '전각으로'}"
    if t == "format":
        return f"{name}: {r['format']}"
    return name


def _half(s: str) -> str:
    return "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else " " if c == "　" else c for c in s)


def _full(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in s)


def compile_rules(rules):
    """정규식을 미리 컴파일하고 틀린 것은 ValueError 로 알린다. [(규칙, 컴파일한 정규식 또는 None)]."""
    out = []
    for i, r in enumerate(rules):
        if not r.get("on", True):
            continue
        rx = None
        if r["type"] == "replace" and r.get("find"):
            pat = r["find"] if r.get("regex") else re.escape(r["find"])
            try:
                rx = re.compile(pat, 0 if r.get("case") else re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"{i + 1}번 규칙 정규식 오류: {e}") from e
        out.append((r, rx))
    return out


def apply_rules(value: str, compiled, af=None, index=0, total=0) -> str:
    """value 에 규칙을 차례로. index 는 목록에서 몇 번째 곡인지 (번호 붙이기용)."""
    s = value

    def fmt(text):
        if "%" not in text and "$" not in text:
            return text
        return evaluate(text, Context(af, index, total, overrides={"_value": s}))

    for r, rx in compiled:
        t = r["type"]
        if t == "replace":
            if rx is None:
                continue
            if r.get("regex"):
                repl = re.sub(r"\$(\d)", r"\\\1", r.get("repl", ""))
            else:
                plain = r.get("repl", "")
                repl = lambda _m, plain=plain: plain  # noqa: E731
            try:
                s = rx.sub(repl, s, count=0 if r.get("all", True) else 1)
            except (re.error, IndexError) as e:
                raise ValueError(f"바꿀 말 오류: {e}") from e
        elif t == "affix":
            s = fmt(r.get("prefix", "")) + s + fmt(r.get("suffix", ""))
        elif t == "insert":
            pos = max(0, min(len(s), int(r.get("pos", 0))))
            at = len(s) - pos if r.get("from_end") else pos
            s = s[:at] + fmt(r.get("text", "")) + s[at:]
        elif t == "remove":
            m, n = r.get("mode"), max(0, int(r.get("count", 0)))
            if m == "first":
                s = s[n:]
            elif m == "last":
                s = s[:len(s) - n] if n else s
            elif m == "range":
                p = max(1, int(r.get("pos", 1))) - 1
                s = s[:p] + s[p + n:]
            elif r.get("find"):
                i = s.find(r["find"])
                if i >= 0:
                    keep = r.get("keep_find")
                    if m == "before":
                        s = s[i if keep else i + len(r["find"]):]
                    else:
                        s = s[:i + len(r["find"]) if keep else i]
        elif t == "number":
            num = str(int(r.get("start", 1)) + index * int(r.get("step", 1))).zfill(int(r.get("digits", 2)))
            sep = r.get("sep", "")
            s = num + sep + s if r.get("where") == "prefix" else s + sep + num
        elif t == "case":
            s = _apply_case(s, r.get("mode", "words"), r.get("exceptions", "").split())
        elif t == "tidy":
            if r.get("underscore"):
                s = s.replace("_", " ")
            if r.get("dots"):
                s = s.replace(".", " ")
            if r.get("spaces"):
                s = re.sub(r"[ 　]{2,}", " ", s)
            if r.get("trim"):
                s = s.strip()
        elif t == "width":
            s = _half(s) if r.get("mode") == "half" else _full(s)
        elif t == "format":
            try:
                s = fmt(r.get("format", "%_value%"))
            except ScriptError as e:
                raise ValueError(str(e)) from e
    return s
