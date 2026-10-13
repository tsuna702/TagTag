"""목록 필터 (Mp3tag의 Filter).

  artist HAS 아이유                      포함
  album IS "꽃갈피"                      같음 (대소문자 무시)
  title MATCHES "^\\d+"                  정규식
  year GREATER 2010 / LESS / EQUAL       숫자 비교
  genre MISSING / comment PRESENT        값이 없음 / 있음
  NOT, AND, OR, ( )                      조합
  "%artist% - %title%" HAS 사랑          포맷 문자열로 비교
  $len(%title%) GREATER 30
그 밖의 입력은 파일명과 모든 필드에서 찾는다 (여러 단어는 모두 포함).
"""
from __future__ import annotations

import re

from .script import Context, ScriptError, evaluate
from .tagio import FIELDS

BINARY = {"IS", "HAS", "MATCHES", "GREATER", "LESS", "EQUAL"}
UNARY = {"PRESENT", "MISSING"}
KEYWORDS = BINARY | UNARY | {"AND", "OR", "NOT"}
ALIASES = {"filename": "%_filename_ext%", "path": "%_path%", "folder": "%_directory%",
           "ext": "%_extension%", "length": "%_length_seconds%", "bitrate": "%_bitrate%"}


class FilterError(Exception):
    pass


def _tokenize(s: str):
    toks, i = [], 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
        elif c in "()":
            toks.append((c, c))
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(s):
                if s[j] == '"':
                    if j + 1 < len(s) and s[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
        elif c == "$":
            # $func(...) 는 괄호 짝이 맞을 때까지 한 덩어리
            j, depth = i, 0
            while j < len(s):
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                elif s[j].isspace() and depth == 0:
                    break
                j += 1
            toks.append(("word", s[i:j]))
            i = j
        else:
            j = i
            while j < len(s) and not s[j].isspace() and s[j] not in '()"':
                j += 1
            w = s[i:j]
            toks.append(("kw", w.upper()) if w.upper() in KEYWORDS else ("word", w))
            i = j
    return toks


def _operand_format(text: str) -> str:
    if "%" in text or "$" in text:
        return text
    low = text.lower()
    if low in ALIASES:
        return ALIASES[low]
    return f"%{text}%"


class _Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def parse(self):
        node = self.or_expr()
        if self.i != len(self.toks):
            raise FilterError("해석할 수 없는 부분이 있습니다")
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.peek() == ("kw", "OR"):
            self.take()
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.unary()
        while self.peek() == ("kw", "AND"):
            self.take()
            node = ("and", node, self.unary())
        return node

    def unary(self):
        t = self.peek()
        if t == ("kw", "NOT"):
            self.take()
            return ("not", self.unary())
        if t[0] == "(":
            self.take()
            node = self.or_expr()
            if self.take()[0] != ")":
                raise FilterError("닫는 괄호 ')'가 없습니다")
            return node
        return self.condition()

    def condition(self):
        kind, left = self.take()
        if kind not in ("word", "str"):
            raise FilterError("필드 이름이 와야 합니다")
        kind, op = self.take()
        if kind != "kw" or op not in BINARY | UNARY:
            raise FilterError("IS, HAS, MATCHES 같은 연산자가 와야 합니다")
        fmt = _operand_format(left)
        if op in UNARY:
            return ("cmp", op, fmt, None)
        kind, right = self.take()
        if kind not in ("word", "str"):
            raise FilterError(f"{op} 뒤에 값이 와야 합니다")
        if op == "MATCHES":
            try:
                right = re.compile(right, re.IGNORECASE)
            except re.error as e:
                raise FilterError(f"정규식 오류: {e}") from e
        return ("cmp", op, fmt, right)


def _num(s: str):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _eval(node, ctx):
    t = node[0]
    if t == "and":
        return _eval(node[1], ctx) and _eval(node[2], ctx)
    if t == "or":
        return _eval(node[1], ctx) or _eval(node[2], ctx)
    if t == "not":
        return not _eval(node[1], ctx)
    _, op, fmt, right = node
    left = evaluate(fmt, ctx)
    if op == "PRESENT":
        return left != ""
    if op == "MISSING":
        return left == ""
    if op == "MATCHES":
        return right.search(left) is not None
    if isinstance(right, str) and ("%" in right or "$" in right):
        right = evaluate(right, ctx)
    if op == "IS":
        return left.casefold() == right.casefold()
    if op == "HAS":
        return right.casefold() in left.casefold()
    a, b = _num(left), _num(right)
    if a is None or b is None:
        return False
    return {"GREATER": a > b, "LESS": a < b, "EQUAL": a == b}[op]


def compile_filter(text: str):
    """문자열 → (AudioFile → bool) 함수. 빈 문자열이면 None."""
    text = text.strip()
    if not text:
        return None
    toks = _tokenize(text)
    if any(k == "kw" and v in BINARY | UNARY for k, v in toks):
        node = _Parser(toks).parse()

        def fn(af):
            try:
                return _eval(node, Context(af))
            except ScriptError:
                return False
        # 포맷 문자열 오류는 미리 한 번 확인해서 알려 준다
        try:
            _eval(node, Context(None))
        except ScriptError as e:
            raise FilterError(str(e)) from e
        return fn

    words = [w.casefold() for k, w in toks if k in ("word", "str", "kw")]

    def plain(af):
        hay = " ".join([af.filename] + [af.get(n) for n in FIELDS]).casefold()
        return all(w in hay for w in words)
    return plain
