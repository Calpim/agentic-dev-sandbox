#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import runpy
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

target = repo_root / "orchestrator" / "guards" / "tdd_isolation.py"
if target.exists():
    runpy.run_path(str(target), run_name="__main__")
else:
    import json

    print(json.dumps({"decision": "allow"}))
