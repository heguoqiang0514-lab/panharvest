"""本地网页界面的冒烟测试：起假 PanSou → 起服务 → 提交任务 → 读 SSE → 看结果。

用法：
    python tests/test_web.py
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pansou import MockPanSou  # noqa: E402
from panharvest import server, util  # noqa: E402
from panharvest.config import Config  # noqa: E402
from panharvest.state import State  # noqa: E402

FAILS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        util.log(f"[PASS] {name}")
    else:
        util.log(f"[FAIL] {name}  -- {detail}", "ERROR")
        FAILS.append(name)


def main() -> int:
    sandbox = ROOT / "data" / "web"
    shutil.rmtree(sandbox, ignore_errors=True)
    util.ensure_dir(sandbox)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901

    with MockPanSou() as mock:
        cfg_data = {
            "pansou": {"base": mock.base, "timeout": 15},
            "paths": {
                "data": str(sandbox / "data"), "downloads": str(sandbox / "data" / "downloads"),
                "library": str(sandbox / "library"), "inbox": str(sandbox / "data" / "inbox"),
                "tools": str(ROOT / "tools"), "reports": str(sandbox / "reports"),
            },
            "backends": {"transfer": "manual", "download": "manual"},
            "ui": {"open_browser": False},
            "open_when_done": False,
        }
        cfg_path = sandbox / "config.json"
        cfg_path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg = Config.load(cfg_path, root=sandbox)
        state = State(cfg.path_of("paths.data") / "state.db")

        thread = threading.Thread(target=server.serve, args=(cfg, state),
                                  kwargs={"port": port, "open_browser": False}, daemon=True)
        thread.start()
        time.sleep(1.2)
        base = f"http://127.0.0.1:{port}"

        # 1) 首页
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            page = resp.read().decode("utf-8")
        ok("首页可访问", resp.status == 200 and "网盘资源收割机" in page, str(resp.status))
        ok("首页带运行日志区", 'id="log"' in page and 'id="prompt"' in page, "")

        # 2) 书库接口
        with urllib.request.urlopen(base + "/api/library", timeout=10) as resp:
            lib = json.loads(resp.read().decode("utf-8"))
        ok("/api/library 返回数组", isinstance(lib, list), str(type(lib)))

        # 3) 提交任务
        req = urllib.request.Request(
            base + "/api/run", method="POST",
            data=json.dumps({"prompt": "葬送的芙莉莲 漫画 第1-10卷", "mode": "search"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            started = json.loads(resp.read().decode("utf-8"))
        job_id = started.get("job_id", "")
        ok("提交任务成功", bool(job_id), str(started))

        # 4) 读 SSE
        events, done_result = [], None
        with urllib.request.urlopen(f"{base}/api/stream?job={job_id}", timeout=60) as resp:
            deadline = time.time() + 45
            while time.time() < deadline:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if not text.startswith("data:"):
                    continue
                event = json.loads(text[5:].strip())
                events.append(event)
                if event.get("type") == "done":
                    done_result = event.get("result")
                    break
        logs = [e for e in events if e.get("type") == "log"]
        ok("SSE 收到日志", len(logs) >= 5, f"{len(logs)} 条")
        ok("SSE 收到结束事件", done_result is not None, str(events[-1] if events else ""))
        if done_result:
            ok("结果里有候选", len(done_result.get("candidates") or []) >= 3,
               str(len(done_result.get("candidates") or [])))
            ok("结果里有报告路径", bool(done_result.get("report")), str(done_result.get("report")))
            ok("日志里能看到阶段", any("search" in str(e.get("line", "")) for e in logs),
               str([e.get("line", "")[:40] for e in logs[:3]]))
        state.close()

    util.log("=" * 60)
    if FAILS:
        util.log(f"网页界面测试失败：{FAILS}", "ERROR")
        return 1
    util.log("网页界面测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
