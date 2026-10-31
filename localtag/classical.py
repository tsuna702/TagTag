"""클래식 작품·악장 나누기: 'Waltzes, Op. 64: 2. Tempo Giusto' → 작품 · 악장 번호 · 악장 이름.

Music 은 m4a 의 작품(©wrk)·악장 이름(©mvn)·번호(©mvi)·악장 수(©mvc)·'작품 및 악장 보기'(shwm)를 읽어
같은 작품의 악장을 묶어 보여 준다. 다른 형식에는 같은 이름의 필드로 넣는다 (Music 은 m4a 만 보여 줌).
"""
from __future__ import annotations

import re

FIELDS = ("WORK", "MOVEMENT", "MOVEMENTTOTAL", "MOVEMENTNAME", "SHOWMOVEMENT")
# 작품과 악장 사이: 첫 ': ' (작품 이름 안의 'No. 6 June: Barcarolle' 같은 콜론은 악장 쪽에 남는다)
_SPLIT = re.compile(r"^(?P<work>.+?):\s+(?P<rest>.+)$")
# 악장 앞 번호: '2. ' · 'II. ' · 'No. 3, ' · 'No. 6 ' · 'Nr. 1 '
_NUM = re.compile(r"^(?P<pre>(?:No|Nr|N°|Mvt|Mov)\.?\s*)?(?P<num>\d{1,3}|[IVXLC]{1,7})(?P<sep>[.,:)]\s*|\s+)(?P<name>\S.*)$",
                  re.IGNORECASE)
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman(s: str) -> int:
    """로마 숫자 → 정수. 로마 숫자가 아니면 0."""
    s = s.upper()
    if not s or any(c not in _ROMAN for c in s):
        return 0
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total


def split_title(title: str):
    """제목 → (작품, 악장 번호, 악장 이름). 나눌 수 없으면 None. 번호가 없으면 0."""
    m = _SPLIT.match((title or "").strip())
    if not m:
        return None
    work, rest = m["work"].strip(), m["rest"].strip()
    num, name = 0, rest
    n = _NUM.match(rest)
    if n:
        raw = n["num"]
        val = int(raw) if raw.isdigit() else roman(raw)
        # 띄어쓰기만 있으면 'No. 6 June' 처럼 앞에 No. 가 있을 때만 번호 ('10 Pieces', 'I Will' 은 이름)
        if val and (n["pre"] or n["sep"].strip()):
            num = val
            # 'No. 3 in G-Flat Major' 은 조성만 남으면 어색하므로 이름에 번호를 그대로 둔다
            if not n["name"].lower().startswith("in "):
                name = n["name"].strip()
    if not work or not name:
        return None
    return work, num, name


TOTAL_MODES = {
    "none": "비워 두기 (Music 에 '2' 처럼 번호만)",
    "max": "목록에서 같은 작품의 가장 큰 번호",
    "count": "목록에서 같은 작품의 곡 수",
}


def totals(rows, mode: str) -> dict:
    """rows: [(작품, 번호)] → {작품: 악장 수}. mode 가 none 이면 빈 dict."""
    out = {}
    if mode == "none":
        return out
    for work, num in rows:
        if mode == "max":
            out[work] = max(out.get(work, 0), num)
        else:
            out[work] = out.get(work, 0) + 1
    return out


def changes_for(tags: dict, work: str, num: int, name: str, total: int = 0, show: bool = True,
                title: str | None = None) -> dict:
    """한 곡에 넣을 변경 {필드: [값]} — 지금 값과 같은 것은 빼고. title 이 있으면 제목도 바꾼다."""
    want = {"WORK": [work], "MOVEMENTNAME": [name],
            "MOVEMENT": [str(num)] if num else [],
            "MOVEMENTTOTAL": [str(total)] if total and num else [],
            "SHOWMOVEMENT": ["1"] if show else []}
    if title is not None:
        want["title"] = [title]
    return {k: v for k, v in want.items() if (tags.get(k) or []) != v}
