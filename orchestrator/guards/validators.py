import re
import tempfile
from pathlib import Path
from typing import Union

PBI_ID_PATTERN = re.compile(r"^PBI-[A-Za-z0-9_-]+$")


def validate_pbi_id(pbi_id: str) -> str:
    """
    PBI 識別子の形式を検証する (Issue #16).
    想定形式: 'PBI-101', 'PBI-USER-001' 等
    パストラバーサル (../) や不正文字を含む場合は ValueError を発生させる。
    """
    if not pbi_id or not isinstance(pbi_id, str):
        raise ValueError("PBI ID must be a non-empty string.")

    cleaned = pbi_id.strip()
    if not PBI_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid PBI ID: '{pbi_id}'. PBI ID must match pattern '^PBI-[A-Za-z0-9_-]+$' (e.g. 'PBI-101')."
        )
    return cleaned


def validate_safe_path(
    base_dir: Path,
    target_path: Union[Path, str],
    allow_temp: bool = False,
) -> Path:
    """
    解決済みパスが想定ルート配下に収まっているかを検証する (パストラバーサル防止).
    allow_temp=True の場合、システム一時ディレクトリ配下も許可する (テスト用).
    """
    base_resolved = base_dir.resolve()
    target_resolved = Path(target_path).resolve()

    # 1. base_dir 配下のチェック
    try:
        target_resolved.relative_to(base_resolved)
        return target_resolved
    except ValueError:
        pass

    # 2. allow_temp が有効な場合、一時ディレクトリ配下のチェック
    if allow_temp:
        temp_dir = Path(tempfile.gettempdir()).resolve()
        try:
            target_resolved.relative_to(temp_dir)
            return target_resolved
        except ValueError:
            pass

    raise ValueError(f"Path traversal detected: '{target_path}' is outside base directory '{base_dir}'.")
