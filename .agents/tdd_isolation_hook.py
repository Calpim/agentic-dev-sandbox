#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bridge runner for Antigravity Lifecycle Hook (tdd-isolation)
Ensures repo_root is in sys.path regardless of execution directory.
Fail-closed policy: Denies execution if the guard script is missing or fails.
"""

import json
import runpy
import sys

if hasattr(sys.stdin, "reconfigure"):
    try:
        getattr(sys.stdin, "reconfigure")(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stdout, "reconfigure"):
    try:
        getattr(sys.stdout, "reconfigure")(encoding="utf-8")
    except Exception:
        pass
from pathlib import Path

try:
    current_dir = Path(__file__).resolve().parent
    repo_root = current_dir.parent if current_dir.name == ".agents" else current_dir
    target_script = repo_root / "orchestrator" / "guards" / "tdd_isolation.py"

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    if target_script.exists():
        runpy.run_path(str(target_script), run_name="__main__")
    else:
        print(json.dumps({"decision": "deny", "reason": "TDD Isolation Guard script not found (fail-closed)"}))
except Exception as e:
    print(json.dumps({"decision": "deny", "reason": f"TDD Isolation Hook execution failed (fail-closed): {e}"}))
