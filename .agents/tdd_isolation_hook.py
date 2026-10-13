#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import runpy
import sys
from pathlib import Path

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

try:
    cur = Path(__file__).resolve().parent
    repo_root = cur
    while repo_root.parent != repo_root:
        if (repo_root / "orchestrator" / "guards" / "tdd_isolation.py").exists():
            break
        repo_root = repo_root.parent
    target_script = repo_root / "orchestrator" / "guards" / "tdd_isolation.py"

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    if target_script.exists():
        runpy.run_path(str(target_script), run_name="__main__")
    else:
        print(json.dumps({"decision": "deny", "reason": "TDD Isolation Guard script not found (fail-closed)"}))
except Exception as e:
    print(json.dumps({"decision": "deny", "reason": f"TDD Isolation Hook execution failed (fail-closed): {e}"}))
