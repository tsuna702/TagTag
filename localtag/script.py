"""Mp3tag 스타일 포맷 문자열 엔진.

문법
  %field%        태그 값 (예: %artist%, %track%, %_filename%)
  $func(a,b,…)   스크립트 함수 (예: $num(%track%,2), $caps(%title%))
  [ … ]          안에 있는 필드 중 하나라도 값이 있을 때만 표시
  'text'         작은따옴표 안은 그대로 출력 (''는 작은따옴표 하나)
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

from .tagio import FIELDS, SEP, canonical_field, field_key

class ScriptError(Exception):
    pass


# ------------------------------------------------------------------ 파서

class Text:
    __slots__ = ("s",)

    def __init__(self, s):
        self.s = s


class Field:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class Func:
    __slots__ = ("name", "args")

    def __init__(self, name, args):
        self.name, self.args = name, args


class Cond:
    __slots__ = ("nodes",)

    def __init__(self, nodes):
        self.nodes = nodes


_FUNC_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_seq(s: str, i: int, stops: str, depth: int):
    nodes, buf = [], []

    def flush():
        if buf:
            nodes.append(Text("".join(buf)))
            buf.clear()

    while i < len(s):
        c = s[i]
        if c in stops:
            break
        if c == "'":
            j = i + 1
            lit = []
            while j < len(s):
                if s[j] == "'":
                    if j + 1 < len(s) and s[j + 1] == "'":
                        lit.append("'")
                        j += 2
                        continue
                    break
                lit.append(s[j])
                j += 1
            if i + 1 < len(s) and s[i + 1] == "'" and not lit:
                buf.append("'")  # '' 하나만 있으면 작은따옴표
                i += 2
                continue
            buf.append("".join(lit))
            i = j + 1
        elif c == "%":
            j = s.find("%", i + 1)
            if j < 0:
                buf.append(c)
                i += 1
            else:
                flush()
                nodes.append(Field(s[i + 1:j]))
                i = j + 1
        elif c == "$":
            m = _FUNC_NAME.match(s, i + 1)
            if m and m.end() < len(s) and s[m.end()] == "(":
                flush()
                name = m.group(0).lower()
                i = m.end() + 1
                args = []
                if i < len(s) and s[i] == ")":
                    i += 1
                else:
                    while True:
                        arg, i = _parse_seq(s, i, ",)", depth + 1)
                        args.append(arg)
                        if i >= len(s):
                            raise ScriptError(f"${name}( 의 닫는 괄호 ')'가 없습니다")
                        if s[i] == ")":
                            i += 1
                            break
                        i += 1  # ','
                nodes.append(Func(name, args))
            else:
                buf.append(c)
                i += 1
        elif c == "[":
            flush()
            inner, i = _parse_seq(s, i + 1, "]", depth + 1)
            if i >= len(s):
                raise ScriptError("'[' 의 닫는 괄호 ']'가 없습니다")
            nodes.append(Cond(inner))
            i += 1
        else:
            buf.append(c)
            i += 1
    flush()
    return nodes, i


@lru_cache(maxsize=256)
def compile_format(fmt: str):
    nodes, i = _parse_seq(fmt, 0, "", 0)
    return nodes


# ------------------------------------------------------------------ 평가

class Context:
    """포맷 문자열에서 필드 값을 꺼내 오는 곳."""

    def __init__(self, af=None, index=0, total=0, overrides=None, value_filter=None):
        self.af = af
        self.index = index      # 0부터
        self.total = total
        self.overrides = {k.lower(): v for k, v in (overrides or {}).items()}
        self.value_filter = value_filter  # 파일명 변환 때 값 안의 '/' 등을 바꾸는 함수

    def values(self, name: str) -> list[str]:
        n = name.lower()
        if n in self.overrides:
            v = self.overrides[n]
            return v if isinstance(v, list) else [v]
        af = self.af
        if n.startswith("_"):
            return [self._special(n)]
        if af is None:
            return []
        return af.tags.get(field_key(n), [])

    def field(self, name: str) -> str:
        vals = self.values(name)
        s = ", ".join(vals)
        if self.value_filter and not name.lower().startswith("_"):
            s = self.value_filter(s)
        return s

    def _special(self, n: str) -> str:
        af = self.af
        if n == "_counter":
            return str(self.index + 1)
        if n == "_total":
            return str(self.total)
        if af is None:
            return ""
        from .model import fmt_length  # 순환 import 방지
        table = {
            "_filename": lambda: af.stem,
            "_filename_ext": lambda: af.filename,
            "_extension": lambda: af.ext.lstrip("."),
            "_ext": lambda: af.ext.lstrip("."),
            "_path": lambda: af.path,
            "_folderpath": lambda: af.folder + os.sep,
            "_directory": lambda: os.path.basename(af.folder),
            "_parent_directory": lambda: os.path.basename(os.path.dirname(af.folder)),
            "_length": lambda: fmt_length(af.length),
            "_length_seconds": lambda: str(int(round(af.length))),
            "_bitrate": lambda: str(af.bitrate // 1000) if af.bitrate else "",
            "_samplerate": lambda: str(af.sample_rate) if af.sample_rate else "",
            "_channels": lambda: str(af.channels) if af.channels else "",
            "_codec": lambda: af.ext.lstrip(".").upper(),
            "_tag": lambda: af.tag_type,
            "_file_size": lambda: str(af.size),
            "_covers": lambda: str(len(af.pictures)),
        }
        fn = table.get(n)
        return fn() if fn else ""


def _to_int(s: str) -> int:
    m = re.match(r"\s*(-?\d+)", s)
    return int(m.group(1)) if m else 0


def _caps(s: str, rest_lower: bool) -> str:
    out, new_word = [], True
    for ch in s:
        if ch.isalpha() or ch.isdigit():
            if new_word:
                out.append(ch.upper())
            else:
                out.append(ch.lower() if rest_lower else ch)
            new_word = False
        else:
            out.append(ch)
            # 작은따옴표 뒤(don't)는 새 단어로 보지 않는다
            new_word = ch not in "'’"
    return "".join(out)


_INVALID_WIN = '\\/:*?"<>|'


def _bool(v: bool):
    return ("1" if v else "", v)


def _need(name, args, lo, hi=None):
    hi = lo if hi is None else hi
    if not lo <= len(args) <= hi:
        want = f"{lo}" if lo == hi else f"{lo}~{hi}"
        raise ScriptError(f"${name}() 인자 개수가 맞지 않습니다 (필요: {want}개, 받음: {len(args)}개)")


def _str_fn(fn, lo, hi=None):
    """문자열 함수: 결과의 참/거짓은 인자 중 하나라도 참이면 참."""
    def wrapper(name, args, ctx):
        _need(name, args, lo, hi)
        truth = any(t for _, t in args)
        return fn(*[s for s, _ in args]), truth
    return wrapper


def _bool_fn(fn, lo, hi=None):
    def wrapper(name, args, ctx):
        _need(name, args, lo, hi)
        return _bool(fn(*[s for s, _ in args]))
    return wrapper


def _f_if(name, args, ctx):
    _need(name, args, 2, 3)
    if args[0][1]:
        return args[1]
    return args[2] if len(args) == 3 else ("", False)


def _f_if2(name, args, ctx):
    _need(name, args, 2)
    return args[0] if args[0][1] else args[1]


def _f_if3(name, args, ctx):
    if len(args) < 2:
        raise ScriptError("$if3() 인자가 2개 이상 필요합니다")
    for a in args[:-1]:
        if a[1]:
            return a
    return args[-1]


def _f_ifgreater(name, args, ctx):
    _need(name, args, 4)
    return args[2] if _to_int(args[0][0]) > _to_int(args[1][0]) else args[3]


def _f_iflonger(name, args, ctx):
    _need(name, args, 4)
    return args[2] if len(args[0][0]) > _to_int(args[1][0]) else args[3]


def _f_ifequal(name, args, ctx):
    _need(name, args, 4)
    return args[2] if _to_int(args[0][0]) == _to_int(args[1][0]) else args[3]


def _f_and(name, args, ctx):
    return _bool(all(t for _, t in args))


def _f_or(name, args, ctx):
    return _bool(any(t for _, t in args))


def _f_not(name, args, ctx):
    _need(name, args, 1)
    return _bool(not args[0][1])


def _f_xor(name, args, ctx):
    return _bool(sum(1 for _, t in args if t) % 2 == 1)


def _f_replace(name, args, ctx):
    if len(args) < 3 or len(args) % 2 == 0:
        raise ScriptError("$replace(문자열,찾을것,바꿀것[,찾을것,바꿀것…]) 형식이어야 합니다")
    s = args[0][0]
    for i in range(1, len(args), 2):
        if args[i][0]:
            s = s.replace(args[i][0], args[i + 1][0])
    return s, args[0][1]


def _f_regexp(name, args, ctx):
    _need(name, args, 3, 4)
    flags = re.IGNORECASE if len(args) == 4 and _to_int(args[3][0]) else 0
    repl = re.sub(r"\$(\d)", r"\\\1", args[2][0])  # Mp3tag식 $1 → \1
    try:
        return re.sub(args[1][0], repl, args[0][0], flags=flags), args[0][1]
    except re.error as e:
        raise ScriptError(f"$regexp 정규식 오류: {e}") from e


def _f_meta(name, args, ctx):
    _need(name, args, 1, 2)
    vals = ctx.values(args[0][0])
    if len(args) == 2:
        n = _to_int(args[1][0])
        s = vals[n] if 0 <= n < len(vals) else ""
    else:
        s = ", ".join(vals)
    return s, bool(s)


def _f_meta_sep(name, args, ctx):
    _need(name, args, 2)
    s = args[1][0].join(ctx.values(args[0][0]))
    return s, bool(s)


def _f_validate(name, args, ctx):
    _need(name, args, 2)
    s, c = args[0][0], args[1][0]
    return "".join(c if ch in _INVALID_WIN else ch for ch in s), args[0][1]


def _f_longest(name, args, ctx):
    return max(args, key=lambda a: len(a[0])) if args else ("", False)


def _f_shortest(name, args, ctx):
    return min(args, key=lambda a: len(a[0])) if args else ("", False)


def _num(s, n):
    v = _to_int(s)
    if not re.match(r"\s*-?\d", s):
        return ""
    return str(v).zfill(_to_int(n))


def _abbr(s):
    return "".join(w[0] for w in re.split(r"[\s\-_/]+", s) if w and (w[0].isalnum()))


def _div(a, b):
    b = _to_int(b)
    return str(int(_to_int(a) / b)) if b else ""


def _mod(a, b):
    b = _to_int(b)
    return str(_to_int(a) % b) if b else ""


FUNCTIONS = {
    # 조건
    "if": _f_if, "if2": _f_if2, "if3": _f_if3,
    "ifgreater": _f_ifgreater, "iflonger": _f_iflonger, "ifequal": _f_ifequal,
    "and": _f_and, "or": _f_or, "not": _f_not, "xor": _f_xor,
    "eql": _bool_fn(lambda a, b: a == b, 2),
    "neql": _bool_fn(lambda a, b: a != b, 2),
    "grtr": _bool_fn(lambda a, b: _to_int(a) > _to_int(b), 2),
    "geql": _bool_fn(lambda a, b: _to_int(a) >= _to_int(b), 2),
    "less": _bool_fn(lambda a, b: _to_int(a) < _to_int(b), 2),
    "leql": _bool_fn(lambda a, b: _to_int(a) <= _to_int(b), 2),
    "isdigit": _bool_fn(lambda a: a.strip().isdigit(), 1),
    # 문자열
    "left": _str_fn(lambda s, n: s[:max(_to_int(n), 0)], 2),
    "right": _str_fn(lambda s, n: s[-_to_int(n):] if _to_int(n) > 0 else "", 2),
    "mid": _str_fn(lambda s, p, n: s[max(_to_int(p) - 1, 0):max(_to_int(p) - 1, 0) + max(_to_int(n), 0)], 3),
    "cutleft": _str_fn(lambda s, n: s[max(_to_int(n), 0):], 2),
    "cutright": _str_fn(lambda s, n: s[:-_to_int(n)] if _to_int(n) > 0 else s, 2),
    "len": _str_fn(lambda s: str(len(s)), 1),
    "lower": _str_fn(str.lower, 1),
    "upper": _str_fn(str.upper, 1),
    "caps": _str_fn(lambda s: _caps(s, True), 1),
    "caps2": _str_fn(lambda s: _caps(s, False), 1),
    "caps3": _str_fn(lambda s: s[:1].upper() + s[1:].lower(), 1),
    "trim": _str_fn(str.strip, 1),
    "trimleft": _str_fn(str.lstrip, 1),
    "trimright": _str_fn(str.rstrip, 1),
    "replace": _f_replace,
    "regexp": _f_regexp,
    "num": _str_fn(_num, 2),
    "repeat": _str_fn(lambda s, n: s * max(_to_int(n), 0), 2),
    "strstr": _str_fn(lambda s, t: str(s.find(t) + 1), 2),
    "strrchr": _str_fn(lambda s, t: str(s.rfind(t) + 1), 2),
    "abbr": _str_fn(_abbr, 1),
    "char": _str_fn(lambda n: chr(_to_int(n)) if 0 < _to_int(n) < 0x110000 else "", 1),
    "validate": _f_validate,
    "longest": _f_longest,
    "shortest": _f_shortest,
    # 숫자
    "add": _str_fn(lambda a, b: str(_to_int(a) + _to_int(b)), 2),
    "sub": _str_fn(lambda a, b: str(_to_int(a) - _to_int(b)), 2),
    "mul": _str_fn(lambda a, b: str(_to_int(a) * _to_int(b)), 2),
    "div": _str_fn(_div, 2),
    "mod": _str_fn(_mod, 2),
    "neg": _str_fn(lambda a: str(-_to_int(a)), 1),
    # 메타데이터
    "meta": _f_meta,
    "meta_sep": _f_meta_sep,
}


def _eval_nodes(nodes, ctx):
    parts, truth = [], False
    for node in nodes:
        if isinstance(node, Text):
            parts.append(node.s)
        elif isinstance(node, Field):
            v = ctx.field(node.name)
            parts.append(v)
            truth = truth or bool(v)
        elif isinstance(node, Cond):
            s, t = _eval_nodes(node.nodes, ctx)
            if t:
                parts.append(s)
                truth = True
        else:
            fn = FUNCTIONS.get(node.name)
            if fn is None:
                raise ScriptError(f"알 수 없는 함수: ${node.name}()")
            args = [_eval_nodes(a, ctx) for a in node.args]
            s, t = fn(node.name, args, ctx)
            parts.append(s)
            truth = truth or t
    return "".join(parts), truth


def evaluate(fmt: str, ctx: Context) -> str:
    return _eval_nodes(compile_format(fmt), ctx)[0]


def split_values(s: str) -> list[str]:
    """포맷 결과를 태그 값 목록으로 (\\\\ 로 여러 값)."""
    if s == "":
        return []
    return s.split(SEP)


# ------------------------------------------------------------------ 패턴 → 필드 (파일명→태그 등)

_PATTERN_FIELD = re.compile(r"%([^%]+)%")


class Pattern:
    """'%track% - %title%' 같은 패턴으로 문자열을 잘라 필드 값을 뽑는다."""

    def __init__(self, pattern: str, allow_any_name=False):
        self.pattern = pattern
        self.names: list[str] = []
        regex, pos = [], 0
        for m in _PATTERN_FIELD.finditer(pattern):
            regex.append(re.escape(pattern[pos:m.start()]))
            raw = m.group(1).strip().lower()
            if raw == "dummy":
                regex.append(".*?")
            else:
                name = raw if allow_any_name else field_key(raw)
                if not name:
                    raise ScriptError("빈 필드 이름 %% 이 있습니다")
                group = f"g{len(self.names)}"
                self.names.append(name)
                regex.append(f"(?P<{group}>.*?)")
            pos = m.end()
        regex.append(re.escape(pattern[pos:]))
        self.regex = re.compile("".join(regex), re.DOTALL)
        # 패턴에 경로 구분자가 있으면 그만큼 상위 폴더까지 대상에 포함
        self.depth = pattern.count("/") + pattern.count("\\")

    def match(self, s: str) -> dict[str, str] | None:
        m = self.regex.fullmatch(s)
        if not m:
            return None
        out = {}
        for i, name in enumerate(self.names):
            out[name] = m.group(f"g{i}").strip()
        return out

    def source_for_path(self, af) -> str:
        """파일 경로에서 패턴과 맞춰 볼 부분 (확장자 제외). 구분자는 패턴 쪽에 맞춘다."""
        parts = [af.stem]
        folder = af.folder
        for _ in range(self.depth):
            parts.insert(0, os.path.basename(folder))
            folder = os.path.dirname(folder)
        sep = "\\" if "\\" in self.pattern else "/"
        return sep.join(parts)


# ------------------------------------------------------------------ 도움말

HELP_HTML = """
<h3>포맷 문자열 문법</h3>
<table cellspacing=4>
<tr><td><code>%field%</code></td><td>태그 값. 예: <code>%artist% - %title%</code></td></tr>
<tr><td><code>$func(…)</code></td><td>함수. 예: <code>$num(%track%,2)</code></td></tr>
<tr><td><code>[ … ]</code></td><td>안의 필드 중 하나라도 값이 있을 때만 표시. 예: <code>[%album% - ]%title%</code></td></tr>
<tr><td><code>'…'</code></td><td>안의 글자를 그대로 표시 (특수 문자 <code>%$[],()</code> 를 쓸 때)</td></tr>
<tr><td><code>/</code> 또는 <code>\\</code></td><td>태그→파일명에서 폴더 구분 (예: <code>%artist%/%album%/%title%</code>)</td></tr>
<tr><td><code>\\\\</code></td><td>태그에 여러 값을 넣을 때 구분자</td></tr>
</table>

<h3>필드</h3>
<p><code>%title% %artist% %album% %albumartist% %year% %track% %disc% %genre% %composer% %comment%</code><br>
다른 이름: <code>%date%</code>=연도, <code>%tracknumber%</code>=트랙, <code>%discnumber%</code>=디스크, <code>%band%</code>=앨범 아티스트</p>
<p><b>파일 정보</b>: <code>%_filename%</code> (확장자 제외) <code>%_filename_ext%</code> <code>%_extension%</code>
<code>%_path%</code> <code>%_folderpath%</code> <code>%_directory%</code> (폴더 이름) <code>%_parent_directory%</code>
<code>%_length%</code> <code>%_length_seconds%</code> <code>%_bitrate%</code> <code>%_samplerate%</code>
<code>%_channels%</code> <code>%_codec%</code> <code>%_tag%</code> <code>%_file_size%</code> <code>%_covers%</code>
<code>%_counter%</code> (1부터 번호) <code>%_total%</code></p>
<p>파일명→태그 / 텍스트→태그 패턴에서는 <code>%dummy%</code> 로 필요 없는 부분을 건너뜁니다.</p>

<h3>함수</h3>
<table cellspacing=4>
<tr><td colspan=2><b>조건</b></td></tr>
<tr><td><code>$if(x,a,b)</code></td><td>x가 참(값이 있음)이면 a, 아니면 b</td></tr>
<tr><td><code>$if2(a,b)</code></td><td>a가 있으면 a, 없으면 b</td></tr>
<tr><td><code>$if3(a1,a2,…,else)</code></td><td>처음으로 값이 있는 것</td></tr>
<tr><td><code>$ifgreater(n1,n2,a,b)</code></td><td>n1&gt;n2 이면 a</td></tr>
<tr><td><code>$iflonger(s,n,a,b)</code></td><td>s 길이가 n보다 길면 a</td></tr>
<tr><td><code>$ifequal(n1,n2,a,b)</code></td><td>n1=n2 이면 a</td></tr>
<tr><td><code>$eql $neql $grtr $geql $less $leql</code></td><td>비교 (같음, 다름, &gt;, ≥, &lt;, ≤)</td></tr>
<tr><td><code>$and $or $not $xor $isdigit(x)</code></td><td>논리 연산</td></tr>
<tr><td colspan=2><b>문자열</b></td></tr>
<tr><td><code>$left(s,n) $right(s,n)</code></td><td>앞/뒤에서 n글자</td></tr>
<tr><td><code>$mid(s,pos,n)</code></td><td>pos(1부터)에서 n글자</td></tr>
<tr><td><code>$cutLeft(s,n) $cutRight(s,n)</code></td><td>앞/뒤 n글자 잘라내기</td></tr>
<tr><td><code>$len(s)</code></td><td>길이</td></tr>
<tr><td><code>$lower $upper</code></td><td>소문자 / 대문자</td></tr>
<tr><td><code>$caps(s)</code></td><td>단어 첫 글자만 대문자, 나머지 소문자</td></tr>
<tr><td><code>$caps2(s)</code></td><td>단어 첫 글자만 대문자 (나머지 유지)</td></tr>
<tr><td><code>$caps3(s)</code></td><td>문장 첫 글자만 대문자</td></tr>
<tr><td><code>$trim $trimLeft $trimRight</code></td><td>공백 제거</td></tr>
<tr><td><code>$replace(s,a,b,…)</code></td><td>a를 b로 바꾸기 (여러 쌍 가능)</td></tr>
<tr><td><code>$regexp(s,식,바꿀것[,1])</code></td><td>정규식 바꾸기 (<code>$1</code> 그룹 참조, 마지막 1은 대소문자 무시)</td></tr>
<tr><td><code>$num(x,n)</code></td><td>숫자를 n자리로 (예: 3 → 03)</td></tr>
<tr><td><code>$repeat(s,n) $abbr(s) $char(n)</code></td><td>반복, 약어, 문자 코드</td></tr>
<tr><td><code>$strstr(s,t) $strrchr(s,t)</code></td><td>t의 위치 (처음/마지막, 없으면 0)</td></tr>
<tr><td><code>$validate(s,c)</code></td><td>파일명에 못 쓰는 문자를 c로</td></tr>
<tr><td><code>$longest(…) $shortest(…)</code></td><td>가장 긴/짧은 것</td></tr>
<tr><td colspan=2><b>숫자</b></td></tr>
<tr><td><code>$add $sub $mul $div $mod $neg</code></td><td>사칙연산 (정수)</td></tr>
<tr><td colspan=2><b>메타데이터</b></td></tr>
<tr><td><code>$meta(field[,n])</code></td><td>필드 값 (n번째 값, 0부터)</td></tr>
<tr><td><code>$meta_sep(field,sep)</code></td><td>여러 값을 sep으로 이어서</td></tr>
</table>
"""
