"""원본 백업 (앨범태그의 저장 백업·되돌리기).

작업 하나마다 폴더를 만들고, 바꾸기 전 원본 파일을 통째로 복사해 둔다.
앱을 다시 켜도 백업 관리자에서 되돌릴 수 있다.

  Backups/20261013-213000-ab12cd/journal.json
  Backups/20261013-213000-ab12cd/journal.log   (작업 중에만: 파일마다 한 줄씩 덧붙이는 기록)
  Backups/20261013-213000-ab12cd/<uuid>.mp3
  Backups/20261013-213000-ab12cd/tags.pack     (Windows: 곡마다 오디오 밖의 부분을 이어 붙인 것)

Windows(NTFS)에는 복제본(clone)이 없어 통째로 복사하면 저장할 때마다 곡 크기만큼 디스크를 쓴다 (300곡 4GB, 곡당 약 5 ms).
그런데 안전 저장은 오디오 위치가 그대로면 원본의 오디오를 읽지도 쓰지도 않으므로, 오디오 밖(태그)만 보관해도 원래대로 되돌릴 수 있다.
오디오까지 다시 쓰게 되면(태그가 커짐) 쓰기 직전에 원본 전체를 보관한다 (keep_whole). 오디오 위치를 알 수 없는 형식은 늘 통째로.

작업 도중에는 journal.log 에 한 줄씩 덧붙이고(파일이 많아도 매번 전체를 다시 쓰지 않는다),
작업이 끝나면 journal.json 하나로 합친다. 앱이 도중에 꺼지면 다음에 읽을 때 journal.log 를 합쳐 보여 준다.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime

from .safewrite import (
    CHUNK, IS_WIN, IoBatch, MemStage, SafeWriteError, _fsync, _outside, copy_preserving, payload_regions, write_back,
)
from .tagio import move_file

LOG_NAME = "journal.log"
PACK_NAME = "tags.pack"
TAGS_ONLY = IS_WIN  # 오디오 밖만 보관 (맥은 복제본이라 통째로 두어도 공간·시간이 들지 않는다)
RESTORE_BATCH = 32


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


def _replay_log(folder, data):
    """끝나지 않은 작업의 journal.log 를 journal.json 내용에 합친다."""
    try:
        with open(os.path.join(folder, LOG_NAME), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    entries = data.setdefault("entries", [])
    for line in lines:
        try:
            rec = json.loads(line)
            i, entry = rec["i"], rec["e"]
        except (ValueError, KeyError, TypeError):
            continue  # 꺼지는 순간 반쯤 쓰인 마지막 줄
        while len(entries) <= i:
            entries.append(None)
        entries[i] = entry
    data["entries"] = [e for e in entries if e is not None]


def _rebuild(out_path, cur, now, regions, spans, pack):
    """pack 의 태그 조각과 cur 파일의 오디오(now 위치)로 regions 배치의 원본을 out_path 에 다시 만든다."""
    pieces = sorted([(lo, hi - lo, None) for lo, hi in spans]
                    + [(r[0], r[1], n[0]) for r, n in zip(sorted(regions), sorted(now))])
    with open(out_path, "xb") as out, open(cur, "rb") as f:
        for _start, n, src in pieces:
            if src is None:
                data = pack.read(n)
                if len(data) != n:
                    raise SafeWriteError("백업이 손상되었습니다")
                out.write(data)
                continue
            f.seek(src)
            while n:
                data = f.read(min(CHUNK, n))
                if not data:
                    raise SafeWriteError("지금 파일에서 오디오를 읽지 못했습니다")
                out.write(data)
                n -= len(data)


def _dir_size(folder):
    total = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    total += e.stat().st_size
                except OSError:
                    pass
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
        self._index = {}  # id(entry) → 번호
        self._pack = None
        self._save()
        self._log = open(os.path.join(self.folder, LOG_NAME), "a", encoding="utf-8")

    def _save(self):
        _write_json(self.journal_path, self.data)

    def _append(self, entry, sync):
        self._log.write(json.dumps({"i": self._index[id(entry)], "e": entry}, ensure_ascii=False) + "\n")
        self._log.flush()
        if sync:
            os.fsync(self._log.fileno())

    def sync(self):
        """덧붙인 기록을 디스크에 확정한다 (before(sync=False) 를 몇 개 부른 뒤 한 번에)."""
        if self._pack is not None:
            self._pack.flush()
            os.fsync(self._pack.fileno())
        self._log.flush()
        os.fsync(self._log.fileno())

    def before(self, path: str, copy_file: bool, sync: bool = True) -> dict:
        """파일을 바꾸기 직전에 부른다. copy_file이 False면 이름 바꾸기만 기록.
        sync=False 면 기록의 디스크 확정은 sync() 로 따로 한다 — 원본을 바꾸기 전에 반드시 sync()."""
        entry = {"before": path, "after": path, "backup": None, "state": "planned", "size": 0}
        if copy_file:
            regions = payload_regions(path) if TAGS_ONLY else None
            if regions is None:
                self._copy_whole(entry, path)
            else:
                self._keep_tags(entry, path, regions)
        self._index[id(entry)] = len(self.data["entries"])
        self.data["entries"].append(entry)
        self._append(entry, sync=False)
        if sync:
            self.sync()  # 원본을 바꾸기 전에 기록이 디스크에 남아 있어야 한다
        return entry

    def _check_space(self, need):
        if shutil.disk_usage(self.folder).free < need + 64 * 1024 * 1024:
            raise SafeWriteError("백업할 디스크 공간이 부족합니다. 백업을 끄거나 공간을 확보해 주세요.")

    def _copy_whole(self, entry, path):
        name = uuid.uuid4().hex + os.path.splitext(path)[1]
        dest = os.path.join(self.folder, name)
        self._check_space(os.path.getsize(path))
        copy_preserving(path, dest)
        if os.path.getsize(dest) != os.path.getsize(path):
            raise SafeWriteError("백업 사본 확인에 실패했습니다. 파일을 바꾸지 않았습니다.")
        entry["backup"] = name
        entry["size"] = os.path.getsize(dest)

    def _keep_tags(self, entry, path, regions):
        """오디오 밖의 부분만 tags.pack 에 덧붙인다."""
        size = os.path.getsize(path)
        spans = _outside(size, regions)
        need = sum(hi - lo for lo, hi in spans)
        self._check_space(need)
        if self._pack is None:
            self._pack = open(os.path.join(self.folder, PACK_NAME), "ab")
        offset = self._pack.tell()
        with open(path, "rb") as f:
            for lo, hi in spans:
                f.seek(lo)
                data = f.read(hi - lo)
                if len(data) != hi - lo:
                    raise SafeWriteError("백업 사본 확인에 실패했습니다. 파일을 바꾸지 않았습니다.")
                self._pack.write(data)
        entry["backup"] = PACK_NAME
        entry["size"] = need
        entry["delta"] = {"size": size, "regions": [list(r) for r in regions], "offset": offset}

    def keep_whole(self, entry: dict):
        """저장이 오디오까지 다시 쓰게 되면 쓰기 직전에 부른다: 원본 전체를 보관해 두고 디스크에 확정한다."""
        d = entry.get("delta")
        if not d:
            return
        path = entry["before"]
        now = payload_regions(path)
        regions = [tuple(r) for r in d["regions"]]
        if now is None or [n for _, n in now] != [n for _, n in regions]:
            raise SafeWriteError("백업 사본 확인에 실패했습니다. 파일을 바꾸지 않았습니다.")
        name = uuid.uuid4().hex + os.path.splitext(path)[1]
        dest = os.path.join(self.folder, name)
        self._check_space(d["size"])
        self._pack.flush()
        try:
            # 앞선 작업이 이미 태그를 바꿨을 수 있으므로 복사하지 않고 보관한 태그와 오디오로 원본을 다시 만든다
            with open(os.path.join(self.folder, PACK_NAME), "rb") as pack:
                pack.seek(d["offset"])
                _rebuild(dest, path, now, regions, _outside(d["size"], regions), pack)
            _fsync(dest)
        except BaseException:
            if os.path.exists(dest):
                os.unlink(dest)
            raise
        entry["backup"] = name
        entry["size"] = d["size"]
        del entry["delta"]
        self._append(entry, sync=False)
        self.sync()

    def after(self, entry: dict, new_path: str):
        entry["after"] = new_path
        entry["after_identity"] = _identity(new_path)
        entry["state"] = "done"
        self._append(entry, sync=False)

    def failed(self, entry: dict, error: str):
        entry["state"] = "failed"
        entry["error"] = error
        self._append(entry, sync=False)

    def finish(self):
        self._log.close()
        if self._pack is not None:
            self._pack.close()
            self._pack = None
        # 실제로 바뀐 파일이 없으면 백업 폴더를 남기지 않는다
        entries = [e for e in self.data["entries"] if e["state"] == "done"]
        if not entries:
            shutil.rmtree(self.folder, ignore_errors=True)
            return None
        for e in self.data["entries"]:
            if e["state"] != "done" and e.get("backup") and not e.get("delta"):
                try:
                    os.unlink(os.path.join(self.folder, e["backup"]))
                except OSError:
                    pass
        self.data["entries"] = entries
        self.data["status"] = "finished"
        self._save()
        try:
            os.unlink(os.path.join(self.folder, LOG_NAME))
        except OSError:
            pass
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
            if j.get("status") == "running":
                _replay_log(j["folder"], j)
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
        todo = [e for e in reversed(job["entries"]) if e.get("state") != "restored"]
        with IoBatch() as io:
            for k, e in enumerate(todo):
                if k % RESTORE_BATCH == 0:
                    io.next([x["after"] for x in todo[k:k + RESTORE_BATCH]])
                cur, orig = e["after"], e["before"]
                try:
                    if not os.path.exists(cur):
                        raise SafeWriteError("파일이 없습니다")
                    if not force and e.get("after_identity") and _identity(cur) != e["after_identity"]:
                        raise SafeWriteError("백업한 뒤 다시 바뀐 파일이라 건너뛰었습니다")
                    if e.get("delta"):
                        self._restore_tags(job, e, cur, orig)
                        if cur != orig:
                            io.moved(cur, orig)
                    elif e.get("backup"):
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
        errors += [f"{os.path.basename(p)}: {why}" for p, why in io.errors]
        job_data = {k: v for k, v in job.items() if k not in ("folder", "bytes")}
        if all(e.get("state") == "restored" for e in job["entries"]):
            job_data["status"] = "restored"
        _write_json(os.path.join(job["folder"], "journal.json"), job_data)
        try:
            os.unlink(os.path.join(job["folder"], LOG_NAME))  # 합친 내용이 journal.json 에 들어갔다
        except OSError:
            pass
        return done, errors

    def _restore_tags(self, job, e, cur, orig):
        """오디오 밖만 보관한 곡 되돌리기. 파일 번호가 그대로라 Music 이 곡을 놓치지 않는다."""
        d = e["delta"]
        regions = [tuple(r) for r in d["regions"]]
        now = payload_regions(cur)
        if now is None or [n for _, n in now] != [n for _, n in regions]:
            raise SafeWriteError("오디오가 백업한 뒤 달라져 되돌릴 수 없습니다 (이 백업에는 태그 부분만 들어 있습니다)")
        if cur != orig and os.path.exists(orig) and not os.path.samefile(cur, orig):
            raise SafeWriteError(f"원래 위치에 다른 파일이 있습니다: {os.path.basename(orig)}")
        spans = _outside(d["size"], regions)
        with open(os.path.join(job["folder"], PACK_NAME), "rb") as pack:
            pack.seek(d["offset"])
            if now == regions:
                stage = MemStage.over(cur)
                try:
                    for lo, hi in spans:
                        data = pack.read(hi - lo)
                        if len(data) != hi - lo:
                            raise SafeWriteError("백업이 손상되었습니다")
                        stage.seek(lo)
                        stage.write(data)
                    stage.truncate(d["size"])
                    write_back(stage, cur, regions)
                finally:
                    stage.close()
            else:  # 그 뒤의 저장으로 오디오 위치가 달라졌다 — 통째로 다시 만든다
                tmp = os.path.join(os.path.dirname(cur), ".localtag-restore-" + uuid.uuid4().hex)
                try:
                    _rebuild(tmp, cur, now, regions, spans, pack)
                    write_back(tmp, cur)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
        if cur != orig:
            move_file(cur, orig)

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
