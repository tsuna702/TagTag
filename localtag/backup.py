"""원본 백업 (앨범태그의 저장 백업·되돌리기).

작업 하나마다 폴더를 만들고, 바꾸기 전 원본 파일을 통째로 복사해 둔다.
앱을 다시 켜도 백업 관리자에서 되돌릴 수 있다.

  Backups/20261013-213000-ab12cd/journal.json
  Backups/20261013-213000-ab12cd/<uuid>.mp3
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime

from .safewrite import copy_preserving, SafeWriteError, write_back
from .tagio import move_file


def _identity(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def _write_json(path, value):
    tmp = path + ".writing"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _dir_size(folder):
    total = 0
    for name in os.listdir(folder):
        try:
            total += os.path.getsize(os.path.join(folder, name))
        except OSError:
            pass
    return total


class BackupJob:
    def __init__(self, store, label):
        # 이름순 = 시간순이 되도록 마이크로초까지 넣는다
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + uuid.uuid4().hex[:6]
        self.folder = os.path.join(store.root, self.id)
        os.makedirs(self.folder, mode=0o700)
        self.journal_path = os.path.join(self.folder, "journal.json")
        self.data = {"id": self.id, "label": label, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "status": "running", "entries": []}
        self._save()

    def _save(self):
        _write_json(self.journal_path, self.data)

    def before(self, path: str, copy_file: bool) -> dict:
        """파일을 바꾸기 직전에 부른다. copy_file이 False면 이름 바꾸기만 기록."""
        entry = {"before": path, "after": path, "backup": None, "state": "planned", "size": 0}
        if copy_file:
            name = uuid.uuid4().hex + os.path.splitext(path)[1]
            dest = os.path.join(self.folder, name)
            if shutil.disk_usage(self.folder).free < os.path.getsize(path) + 64 * 1024 * 1024:
                raise SafeWriteError("백업할 디스크 공간이 부족합니다. 백업을 끄거나 공간을 확보해 주세요.")
            copy_preserving(path, dest)
            if os.path.getsize(dest) != os.path.getsize(path):
                raise SafeWriteError("백업 사본 확인에 실패했습니다. 파일을 바꾸지 않았습니다.")
            entry["backup"] = name
            entry["size"] = os.path.getsize(dest)
        self.data["entries"].append(entry)
        self._save()
        return entry

    def after(self, entry: dict, new_path: str):
        entry["after"] = new_path
        entry["after_identity"] = _identity(new_path)
        entry["state"] = "done"
        self._save()

    def failed(self, entry: dict, error: str):
        entry["state"] = "failed"
        entry["error"] = error
        self._save()

    def finish(self):
        # 실제로 바뀐 파일이 없으면 백업 폴더를 남기지 않는다
        entries = [e for e in self.data["entries"] if e["state"] == "done"]
        if not entries:
            shutil.rmtree(self.folder, ignore_errors=True)
            return None
        for e in self.data["entries"]:
            if e["state"] != "done" and e.get("backup"):
                try:
                    os.unlink(os.path.join(self.folder, e["backup"]))
                except OSError:
                    pass
        self.data["entries"] = entries
        self.data["status"] = "finished"
        self._save()
        return self.id


class BackupStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)

    def begin(self, label: str) -> BackupJob:
        return BackupJob(self, label)

    def jobs(self) -> list[dict]:
        out = []
        for name in sorted(os.listdir(self.root), reverse=True):
            jp = os.path.join(self.root, name, "journal.json")
            try:
                with open(jp, encoding="utf-8") as f:
                    j = json.load(f)
            except (OSError, ValueError):
                continue
            j["folder"] = os.path.join(self.root, name)
            j["bytes"] = _dir_size(j["folder"])
            out.append(j)
        return out

    def total_bytes(self) -> int:
        return sum(j["bytes"] for j in self.jobs())

    def check(self, job: dict) -> list[str]:
        """되돌리기 전에 확인: 백업 이후 다시 바뀐 파일 목록."""
        changed = []
        for e in job["entries"]:
            if e.get("state") == "restored":
                continue
            cur = e["after"]
            if not os.path.exists(cur):
                changed.append(f"{os.path.basename(cur)}: 파일이 없습니다 (옮겨졌거나 지워짐)")
            elif e.get("after_identity") and _identity(cur) != e["after_identity"]:
                changed.append(f"{os.path.basename(cur)}: 백업한 뒤 다시 바뀌었습니다")
        return changed

    def restore(self, job: dict, force=False) -> tuple[list[tuple[str, str]], list[str]]:
        """원래 상태로 되돌린다. (복원한 [(현재 경로, 원래 경로)], 오류 목록)"""
        done, errors = [], []
        for e in reversed(job["entries"]):
            if e.get("state") == "restored":
                continue
            cur, orig = e["after"], e["before"]
            try:
                if not os.path.exists(cur):
                    raise SafeWriteError("파일이 없습니다")
                if not force and e.get("after_identity") and _identity(cur) != e["after_identity"]:
                    raise SafeWriteError("백업한 뒤 다시 바뀐 파일이라 건너뛰었습니다")
                if e.get("backup"):
                    backup = os.path.join(job["folder"], e["backup"])
                    if not os.path.exists(backup):
                        raise SafeWriteError("백업 파일이 없습니다")
                    # 원래 위치에 임시 사본을 만든 뒤 한 번에 교체
                    os.makedirs(os.path.dirname(orig), exist_ok=True)
                    stage = os.path.join(os.path.dirname(orig), ".localtag-restore-" + uuid.uuid4().hex)
                    copy_preserving(backup, stage)
                    if cur != orig and os.path.exists(orig) and not os.path.samefile(cur, orig):
                        os.unlink(stage)
                        raise SafeWriteError(f"원래 위치에 다른 파일이 있습니다: {os.path.basename(orig)}")
                    if os.path.exists(orig):
                        write_back(stage, orig)  # 파일 번호 유지 (Music이 곡을 놓치지 않게)
                        if os.path.exists(stage):
                            os.unlink(stage)
                    else:
                        os.replace(stage, orig)
                    if cur != orig and os.path.exists(cur) and not os.path.samefile(cur, orig):
                        os.unlink(cur)
                elif cur != orig:
                    move_file(cur, orig)
                e["state"] = "restored"
                done.append((cur, orig))
            except (OSError, SafeWriteError) as ex:
                errors.append(f"{os.path.basename(cur)}: {ex}")
        job_data = {k: v for k, v in job.items() if k not in ("folder", "bytes")}
        if all(e.get("state") == "restored" for e in job["entries"]):
            job_data["status"] = "restored"
        _write_json(os.path.join(job["folder"], "journal.json"), job_data)
        return done, errors

    def delete(self, job: dict):
        shutil.rmtree(job["folder"], ignore_errors=True)

    def prune(self, max_days: int = 30, max_bytes: int = 5 * 1024 ** 3):
        """오래된 백업과, 전체 용량이 한도를 넘는 만큼 오래된 것부터 지운다."""
        jobs = self.jobs()
        cutoff = time.time() - max_days * 86400
        removed = 0
        keep = []
        for j in jobs:
            try:
                created = time.mktime(time.strptime(j["created"], "%Y-%m-%d %H:%M:%S"))
            except (KeyError, ValueError):
                created = time.time()
            if max_days and created < cutoff:
                self.delete(j)
                removed += 1
            else:
                keep.append(j)
        total = sum(j["bytes"] for j in keep)
        for j in reversed(keep):  # 오래된 것부터
            if not max_bytes or total <= max_bytes:
                break
            total -= j["bytes"]
            self.delete(j)
            removed += 1
        return removed
