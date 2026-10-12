import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Union

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


def extract_biz_ids_from_example_map(example_map_content: str) -> List[str]:
    """Example Map コンテンツから BIZ-xxx 識別子を重複なく出現順で抽出する (Issue #13)"""
    matches = re.findall(r"##\s+(BIZ-\d+)", example_map_content)
    seen = set()
    result = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def validate_gherkin_coverage(
    example_map_content: str,
    biz_gherkin: str,
    app_gherkin: str,
    bff_gherkin: str,
    strict: bool = False,
) -> Dict[str, Any]:
    """
    Gherkin 仕様が Example Map 内の全 BIZ ルールを網羅しているか検証する (Issue #13).
    - ビジネス層: 各ルールに @BIZ-xxx タグが付与されていること
    - App層: 各ルールに対して @covers:BIZ-xxx タグが付与されていること
    - BFF層: 各ルールに対して @covers:BIZ-xxx タグが付与されていること
    strict=True の場合、カバレッジ不足時に ValueError を発生させる (fail-closed)。
    """
    biz_ids = extract_biz_ids_from_example_map(example_map_content)
    if not biz_ids:
        biz_ids = ["BIZ-001", "BIZ-002"]

    missing_biz = [bid for bid in biz_ids if f"@{bid}" not in biz_gherkin]
    missing_app = [bid for bid in biz_ids if f"@covers:{bid}" not in app_gherkin]
    missing_bff = [bid for bid in biz_ids if f"@covers:{bid}" not in bff_gherkin]

    is_covered = len(missing_biz) == 0 and len(missing_app) == 0 and len(missing_bff) == 0

    result = {
        "is_covered": is_covered,
        "biz_ids": biz_ids,
        "missing_biz": missing_biz,
        "missing_app": missing_app,
        "missing_bff": missing_bff,
    }

    if strict and not is_covered:
        raise ValueError(
            f"Gherkin specification coverage validation failed (fail-closed)! "
            f"Required BIZ rules: {biz_ids}. "
            f"Missing in business: {missing_biz}, "
            f"missing in app: {missing_app}, "
            f"missing in bff: {missing_bff}"
        )

    return result
