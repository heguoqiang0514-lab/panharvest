"""端到端测试：假 PanSou + 假 BaiduPCS-Go，跑完整流水线。

运行：
    python tests/test_e2e.py

它验证的是真实代码路径：搜索 → 存活检测 → 打分 → 转存 → 列目录复核 → 下载
→ 解压（真 zip）→ 清洗 → 入库 → 报告。只有"网盘"和"搜索引擎"是假的。
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pansou import MockPanSou  # noqa: E402
from panharvest import pipeline as pipeline_mod  # noqa: E402
from panharvest import pcs, util  # noqa: E402
from panharvest.config import Config  # noqa: E402
from panharvest.state import State  # noqa: E402

FAILS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        util.log(f"[PASS] {name}")
    else:
        util.log(f"[FAIL] {name}  -- {detail}", "ERROR")
        FAILS.append(name)


def make_volume_zip(path: Path, volume: int, pages: int = 3) -> Path:
    util.ensure_dir(path.parent)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, pages + 1):
            zf.writestr(f"第{volume:02d}卷/{i:03d}.jpg", b"\xff\xd8" + b"j" * 2000)
        # 广告文件，用来验证清洗
        zf.writestr(f"第{volume:02d}卷/更多资源.txt", "公众号 加群 扫码 www.example.com")
        zf.writestr("__MACOSX/._junk", "x")
    return path


class FakePCS(pcs.BaiduPCS):
    """把 BaiduPCS-Go 的四条命令换成固定响应。"""

    def __init__(self, staging: Path, reject_dead: bool = False):
        self.staging = staging
        self.reject_dead = reject_dead
        self.remote_root = "/资源收割"
        self.calls: list[str] = []
        self.remote_created: list[str] = []
        self.transferred: list[str] = []

    # --- 账号
    def whoami(self):
        return {"uid": 12345, "name": "测试账号", "logged_in": True, "raw": ""}

    def logged_in(self):
        return True

    def ensure_dir(self, path):
        self.calls.append(f"mkdir:{path}")
        self.remote_created.append(path)
        return True

    def cd(self, path):
        self.calls.append(f"cd:{path}")
        return True

    def transfer(self, url, password, dest_dir, collect=True):
        self.calls.append(f"transfer:{url}")
        self.transferred.append(url)
        if "deadshare" in url:
            return {"ok": False, "saved": "", "message": "链接已失效", "raw": ""}
        return {"ok": True, "saved": "葬送的芙莉莲 1-10卷", "message": "", "raw": ""}

    def ls(self, path=".", max_depth=2):
        self.calls.append(f"ls:{path}")
        entries = []
        for vol in range(1, 11):
            entries.append({"name": f"葬送的芙莉莲 第{vol:02d}卷.zip",
                            "size": 30 * 1024 * 1024, "isdir": False, "wrapped": False})
        entries.append({"name": "更多资源.txt", "size": 120, "isdir": False, "wrapped": False})
        return {"ok": True, "entries": entries,
                "total_size": sum(e["size"] for e in entries),
                "file_total": len(entries), "dir_total": 0}

    def download(self, remote_paths, local_dir):
        self.calls.append(f"download:{local_dir}")
        saved = []
        for zip_path in sorted(self.staging.glob("*.zip")):
            dest = Path(local_dir) / zip_path.name
            util.ensure_dir(dest.parent)
            shutil.copy(zip_path, dest)
            saved.append(str(dest))
        return {"ok": bool(saved), "saved": saved, "skipped": [], "failed": [],
                "message": "", "raw": ""}

    def rm(self, path):
        self.calls.append(f"rm:{path}")
        return True


def build_config(sandbox: Path, pansou_base: str, backend: str = "pcs") -> Config:
    data = {
        "pansou": {"base": pansou_base, "timeout": 15},
        "paths": {
            "data": str(sandbox / "data"),
            "downloads": str(sandbox / "data" / "downloads"),
            "library": str(sandbox / "library"),
            "inbox": str(sandbox / "data" / "inbox"),
            "tools": str(ROOT / "tools"),
            "reports": str(sandbox / "reports"),
        },
        "backends": {"transfer": backend, "download": backend},
        "limits": {"max_candidates": 10, "min_score": 45, "max_total_gb": 60,
                   "max_files": 5000, "verify_top": 5},
        "ui": {"open_browser": False},
        "open_when_done": False,
    }
    cfg_path = sandbox / "config.json"
    util.ensure_dir(sandbox)
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return Config.load(cfg_path, root=sandbox)


def test_full(sandbox: Path, mock: MockPanSou) -> None:
    util.log("=" * 70)
    util.log("用例 1：全自动（假 PCS）")
    shutil.rmtree(sandbox, ignore_errors=True)
    cfg = build_config(sandbox, mock.base)
    state = State(Path(cfg.path_of("paths.data")) / "state.db")
    staging = sandbox / "staging"
    for vol in range(1, 11):
        make_volume_zip(staging / f"葬送的芙莉莲 第{vol:02d}卷.zip", vol)
    fake = FakePCS(staging)
    original_backend = pipeline_mod.Pipeline.backend
    original_client = pipeline_mod.Pipeline.pcs_client
    pipeline_mod.Pipeline.backend = lambda self: "pcs"
    pipeline_mod.Pipeline.pcs_client = lambda self, exe=None: fake
    try:
        pipe = pipeline_mod.Pipeline(cfg, state)
        result = pipe.run("葬送的芙莉莲 漫画 第1-10卷 高清", mode="auto")
    finally:
        pipeline_mod.Pipeline.backend = original_backend
        pipeline_mod.Pipeline.pcs_client = original_client

    ok("流程成功", bool(result.get("ok")), result.get("message", ""))
    ok("入库目录存在", Path(result.get("library") or "").is_dir(), str(result.get("library")))
    lib = Path(result.get("library") or ".")
    images = list(lib.rglob("*.jpg"))
    ok("图片被解压并入库", len(images) == 30, f"{len(images)} 张")
    ads = list(lib.rglob("更多资源.txt"))
    ok("广告文件被清掉", not ads, str(ads))
    mac = list(lib.rglob("__MACOSX"))
    ok("__MACOSX 被清掉", not mac, str(mac))
    ok("报告已生成", Path(result.get("report") or "").is_file(), str(result.get("report")))
    ok("转存只对第一个可用候选", len([c for c in fake.calls if c.startswith("transfer")]) == 1,
       str([c for c in fake.calls if c.startswith("transfer")]))
    ok("下载被调用", any(c.startswith("download") for c in fake.calls))
    cands = result.get("candidates") or []
    ok("候选有排序结果", bool(cands) and cands[0].get("verdict") == "accept",
       str([(c.get("score"), c.get("verdict")) for c in cands]))
    ok("失效链接被排除", any(c.get("verdict") == "reject" for c in cands),
       str([(c.get("title"), c.get("verdict")) for c in cands]))
    state.close()


def test_search_only(sandbox: Path, mock: MockPanSou) -> None:
    util.log("=" * 70)
    util.log("用例 2：只要链接（search 模式）")
    shutil.rmtree(sandbox, ignore_errors=True)
    cfg = build_config(sandbox, mock.base, backend="manual")
    state = State(Path(cfg.path_of("paths.data")) / "state.db")
    pipe = pipeline_mod.Pipeline(cfg, state)
    result = pipe.run("葬送的芙莉莲 漫画 第1-10卷", mode="search")
    ok("search 模式不下载", not result.get("library"), str(result.get("library")))
    ok("search 模式给出候选", len(result.get("candidates") or []) >= 3,
       str(len(result.get("candidates") or [])))
    ok("search 模式出报告", Path(result.get("report") or "").is_file())
    state.close()


def test_manual_inbox(sandbox: Path, mock: MockPanSou) -> None:
    util.log("=" * 70)
    util.log("用例 3：人工转存 + inbox 续跑")
    shutil.rmtree(sandbox, ignore_errors=True)
    cfg = build_config(sandbox, mock.base, backend="manual")
    state = State(Path(cfg.path_of("paths.data")) / "state.db")
    pipe = pipeline_mod.Pipeline(cfg, state)
    result = pipe.run("葬送的芙莉莲 漫画 第1-10卷", mode="auto")
    ok("manual 模式生成引导页", bool(result.get("handoff")), str(result))
    ok("manual 模式不下载", not result.get("library"))

    # 模拟用户把下载好的压缩包丢进 inbox，然后 resume
    inbox = Path(cfg.path_of("paths.inbox"))
    util.ensure_dir(inbox)
    for vol in (1, 2):
        make_volume_zip(inbox / f"葬送的芙莉莲 第{vol:02d}卷.zip", vol)
    job_id = result.get("job_id")
    pipe2 = pipeline_mod.Pipeline(cfg, state)
    result2 = pipe2.run("葬送的芙莉莲 漫画 第1-10卷", mode="inbox", job_id=job_id)
    ok("resume 后入库成功", bool(result2.get("ok")), result2.get("message", ""))
    imgs = list(Path(result2.get("library") or ".").rglob("*.jpg"))
    ok("inbox 的文件被解压入库", len(imgs) == 6, f"{len(imgs)} 张")
    state.close()


def main() -> int:
    sandbox = ROOT / "data" / "e2e"
    with MockPanSou() as mock:
        util.log(f"假 PanSou：{mock.base}")
        test_full(sandbox / "full", mock)
        test_search_only(sandbox / "search", mock)
        test_manual_inbox(sandbox / "manual", mock)
    util.log("=" * 70)
    if FAILS:
        util.log(f"端到端测试失败 {len(FAILS)} 项：{FAILS}", "ERROR")
        return 1
    util.log("端到端测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
