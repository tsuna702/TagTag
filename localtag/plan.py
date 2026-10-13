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


@dataclass(eq=False)
class UndoEntry:
    label: str
    items: list = field(default_factory=list)

    def size_estimate(self):
        return sum(i.snapshot.size_estimate() for i in self.items if i.snapshot)
