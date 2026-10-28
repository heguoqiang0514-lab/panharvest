"""把 server.py 里内嵌的前端脚本导出到 data/_ui.js，方便用 node 做语法/行为检查。

    python tests/dump_ui.py && node tests/test_ui.js
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from panharvest import server  # noqa: E402

match = re.search(r"<script>(.*)</script>", server.PAGE, re.S)
if not match:
    raise SystemExit("server.PAGE 里没有 <script> 块")
out = ROOT / "data" / "_ui.js"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(match.group(1), encoding="utf-8")
print(f"wrote {out} ({len(match.group(1))} chars)")
