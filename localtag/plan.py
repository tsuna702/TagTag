"""파일 하나에 적용할 작업(Plan)과 실행 취소 기록."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .tagio import FIELD_LABELS, SEP, AudioFile, Snapshot


@dataclass(eq=False)
class Plan:
    file: AudioFile
    changes: dict = field(default_factory=dict)     # {필드: [값…]} 빈 리스트는 삭제
    pictures: list | None = None                    # None이면 커버 변경 없음
    new_path: str | None = None
    remove_tags: bool = False
    exports: list = field(default_factory=list)     # [(경로, bytes)] 커버 내보내기
    cells: list = field(default_factory=list)       # 미리보기 표에 보일 칸
    error: str | None = None
    unchanged: bool = False

    @property
    def touches_tags(self):
        return bool(self.changes) or self.pictures is not None or self.remove_tags

    @property
    def actionable(self):
        return (not self.error and not self.unchanged
                and (self.touches_tags or bool(self.new_path) or bool(self.exports)))

    def describe(self) -> str:
        """'제목: A → B · 커버 바뀜' 같은 요약."""
        f = self.file
        parts = []
        if self.remove_tags:
            parts.append("태그 모두 제거")
        for name, vals in self.changes.items():
            old = f.get(name)
            new = SEP.join(vals)
            parts.append(f"{FIELD_LABELS.get(name, name)}: {old or '(없음)'} → {new or '(삭제)'}")
        if self.pictures is not None:
            parts.append(f"커버 {len(f.pictures)}개 → {len(self.pictures)}개"
                         if len(self.pictures) != len(f.pictures) else "커버 바뀜")
        if self.new_path:
            parts.append(f"파일명 → {os.path.basename(self.new_path)}")
        for path, _ in self.exports:
            parts.append(f"커버 내보내기 → {os.path.basename(path)}")
        return " · ".join(parts)


@dataclass(eq=False)
class UndoItem:
    old_path: str
    new_path: str
    snapshot: Snapshot | None
    after: tuple | None = None  # 저장 직후 태그 상태 (tag_state) — 되돌리기 전에 다른 곳에서 바뀌었는지 본다


def tag_state(af: AudioFile) -> tuple:
    """비교용 태그 상태: (필드 값, 커버 크기들). 커버 데이터는 담지 않는다 (메모리)."""
    return ({k: tuple(v) for k, v in af.tags.items() if v}, tuple(len(p.data) for p in af.pictures))


def state_diff(before: tuple, now: tuple) -> list[str]:
    """두 태그 상태에서 달라진 필드 이름 (사람이 읽는)."""
    a, b = before[0], now[0]
    out = [FIELD_LABELS.get(k, k) for k in sorted(set(a) | set(b), key=str) if a.get(k) != b.get(k)]
    if before[1] != now[1]:
        out.append("커버")
    return out


@dataclass(eq=False)
class UndoEntry:
    label: str
    items: list = field(default_factory=list)

    def size_estimate(self, seen: set | None = None):
        return sum(i.snapshot.size_estimate(seen) for i in self.items if i.snapshot)
