import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict

from orchestrator.guards.validators import validate_pbi_id, validate_safe_path

logger = logging.getLogger("orchestrator.task_state")

_in_process_lock = Lock()


@contextmanager
def file_lock(lock_file: Path, timeout: float = 10.0, poll_interval: float = 0.05):
    """
    クロスプラットフォームなアトミックファイルロック (Issue #15).
    os.O_CREAT | os.O_EXCL により並行競合を排他し、stale lock の自動回復を備える。
    """
    deadline = time.time() + timeout
    fd = None

    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            # 15秒以上経過した古いロックファイルは stale とみなして解除
            try:
                if lock_file.exists() and (time.time() - lock_file.stat().st_mtime) > 15.0:
                    lock_file.unlink(missing_ok=True)
                    continue
            except OSError:
                pass

            if time.time() >= deadline:
                raise TimeoutError(f"Could not acquire file lock on '{lock_file}' within {timeout}s")
            time.sleep(poll_interval)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


def get_task_state(pbi_id: str, base_dir: Path) -> Dict[str, Any]:
    """tasks/{pbi_id}/state.json を排他ロック下で安全に読み取る (Issue #15)"""
    pbi_id = validate_pbi_id(pbi_id)
    task_dir = validate_safe_path(base_dir / "tasks", base_dir / "tasks" / pbi_id)
    state_file = task_dir / "state.json"
    lock_file = task_dir / "state.json.lock"

    if not state_file.exists():
        return {}

    with _in_process_lock:
        with file_lock(lock_file):
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to read state.json for {pbi_id}: {e}")
                return {}


def update_task_state(
    pbi_id: str,
    base_dir: Path,
    mutator: Callable[[Dict[str, Any]], None],
) -> Dict[str, Any]:
    """
    tasks/{pbi_id}/state.json を排他ロック下でアトミックに読み取り・変更・書き込みする (Issue #15).
    """
    pbi_id = validate_pbi_id(pbi_id)
    task_dir = validate_safe_path(base_dir / "tasks", base_dir / "tasks" / pbi_id)
    state_file = task_dir / "state.json"
    lock_file = task_dir / "state.json.lock"
    tmp_file = task_dir / "state.json.tmp"

    task_dir.mkdir(parents=True, exist_ok=True)

    with _in_process_lock:
        with file_lock(lock_file):
            data: Dict[str, Any] = {}
            if state_file.exists():
                try:
                    data = json.loads(state_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"Failed to parse existing state.json for {pbi_id}, initializing: {e}")

            mutator(data)

            # アトミック書き込み (一時ファイルに書き込んでから os.replace)
            tmp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp_file), str(state_file))
            return data


def update_pipeline_phase_atomic(
    pbi_id: str,
    base_dir: Path,
    component: str,
    phase: str,
    status: str = "IN_PROGRESS",
) -> Dict[str, Any]:
    """コンポーネントごとのパイプラインフェーズをアトミックに更新する (Issue #15)"""

    def _mutate(data: Dict[str, Any]) -> None:
        if "pipelines" not in data or not isinstance(data["pipelines"], dict):
            data["pipelines"] = {}
        if component not in data["pipelines"] or not isinstance(data["pipelines"][component], dict):
            data["pipelines"][component] = {}
        data["pipelines"][component]["phase"] = phase
        data["pipelines"][component]["status"] = status

    return update_task_state(pbi_id, base_dir, _mutate)
