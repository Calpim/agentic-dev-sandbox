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
VALID_PIPELINE_COMPONENTS = {"app", "bff"}
VALID_PIPELINE_PHASES = {
    "INIT",
    "DESIGN",
    "RED",
    "GREEN",
    "REFACTOR",
    "VERIFY",
    "ANALYZER",
    "DONE",
}
VALID_PIPELINE_STATUSES = {
    "PLANNED",
    "IN_PROGRESS",
    "COMPLETED",
    "FAILED",
    "BLOCKED",
}


def _acquire_os_lock(fd: int) -> bool:
    """OS レベルの非ブロッキング排他ロックを取得する (Windows / POSIX 対応)"""
    if os.name == "nt":
        import msvcrt

        try:
            # ファイル先頭の 1 バイトを非ブロッキング排他ロック
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except (OSError, PermissionError):
            return False
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return True
        except (OSError, IOError):
            return False


def _release_os_lock(fd: int) -> None:
    """OS レベルの排他ロックを解放する"""
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except Exception:
            pass


@contextmanager
def file_lock(lock_file: Path, timeout: float = 10.0, poll_interval: float = 0.05):
    """
    クロスプラットフォームな OS ネイティブ排他ファイルロック (Issue #15 レビュー是正).
    Windows (msvcrt) および POSIX (fcntl) のカーネルレベル排他制御を利用。
    プロセス強制終了時も OS が自動的にロックを解放するため stale lock の強奪やデッドロックが起きない。
    """
    deadline = time.time() + timeout
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # ロックファイルを開く（読み書き可能）
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
    # ファイルサイズが 0 の場合は 1 バイト書き込んでロック対象領域を確保
    if os.lseek(fd, 0, os.SEEK_END) == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)

    locked = False
    try:
        while True:
            if _acquire_os_lock(fd):
                locked = True
                break
            if time.time() >= deadline:
                raise TimeoutError(f"Could not acquire OS file lock on '{lock_file}' within {timeout}s")
            time.sleep(poll_interval)

        yield
    finally:
        if locked:
            _release_os_lock(fd)
        try:
            os.close(fd)
        except Exception:
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
            if not state_file.exists():
                return {}
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to read state.json for {pbi_id}: {e}")
                return {}


def update_task_state(
    pbi_id: str,
    base_dir: Path,
    mutator: Callable[[Dict[str, Any]], None],
    must_exist: bool = False,
    create_only: bool = False,
) -> Dict[str, Any]:
    """
    tasks/{pbi_id}/state.json を排他ロック下でアトミックに読み取り・変更・書き込みする (Issue #15).
    state.json が壊れている場合は空で上書きせず ValueError を送出して元データを保全。
    must_exist=True の場合、ファイルが存在しなければ何もせず空辞書を返す。
    create_only=True の場合、既存 state を変更せず FileExistsError を送出する。
    """
    pbi_id = validate_pbi_id(pbi_id)
    task_dir = validate_safe_path(base_dir / "tasks", base_dir / "tasks" / pbi_id)
    state_file = task_dir / "state.json"
    lock_file = task_dir / "state.json.lock"

    if must_exist and not state_file.exists():
        return {}

    task_dir.mkdir(parents=True, exist_ok=True)

    with _in_process_lock:
        with file_lock(lock_file):
            if must_exist and not state_file.exists():
                return {}
            if create_only and state_file.exists():
                raise FileExistsError(f"Task state already exists for {pbi_id}.")

            data: Dict[str, Any] = {}
            if state_file.exists():
                raw_text = state_file.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw_text)
                except Exception as e:
                    raise ValueError(
                        f"state.json for {pbi_id} is corrupted: {e}. Preserving original file to prevent data loss."
                    )

            mutator(data)

            # アトミック書き込み (ユニーク一時ファイルに書いてから os.replace)
            unique_tmp = task_dir / f"state.json.{os.getpid()}_{time.time_ns()}.tmp"
            try:
                unique_tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                for attempt in range(5):
                    try:
                        os.replace(str(unique_tmp), str(state_file))
                        break
                    except (PermissionError, OSError):
                        if attempt == 4:
                            raise
                        time.sleep(0.01 * (attempt + 1))
            finally:
                if unique_tmp.exists():
                    try:
                        unique_tmp.unlink()
                    except Exception:
                        pass
            return data


def update_pipeline_phase_atomic(
    pbi_id: str,
    base_dir: Path,
    component: str,
    phase: str,
    status: str = "IN_PROGRESS",
) -> Dict[str, Any]:
    """コンポーネントごとのパイプラインフェーズをアトミックに更新する (ファイルが存在する場合のみ更新)"""
    component = component.strip().lower()
    phase = phase.strip().upper()
    status = status.strip().upper()
    if component not in VALID_PIPELINE_COMPONENTS:
        raise ValueError(f"Invalid component '{component}'. Expected one of: {', '.join(sorted(VALID_PIPELINE_COMPONENTS))}.")
    if phase not in VALID_PIPELINE_PHASES:
        raise ValueError(f"Invalid phase '{phase}'. Expected one of: {', '.join(sorted(VALID_PIPELINE_PHASES))}.")
    if status not in VALID_PIPELINE_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Expected one of: {', '.join(sorted(VALID_PIPELINE_STATUSES))}.")

    def _mutate(data: Dict[str, Any]) -> None:
        if "pipelines" not in data or not isinstance(data["pipelines"], dict):
            data["pipelines"] = {}
        if component not in data["pipelines"] or not isinstance(data["pipelines"][component], dict):
            data["pipelines"][component] = {}
        data["pipelines"][component]["phase"] = phase
        data["pipelines"][component]["status"] = status

    return update_task_state(pbi_id, base_dir, _mutate, must_exist=True)
