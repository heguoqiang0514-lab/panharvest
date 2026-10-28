#!/usr/bin/env python3
"""把 panharvest 发布到 GitHub —— 走 REST API，不碰 git 协议。

为什么需要它：本机 github.com 的 git 协议经常被 reset（`git ls-remote` 直接断），
而 `api.github.com` 多数时候可达。这个脚本用「建仓库 + Contents API 逐文件上传」
把仓库发出去，不需要 git push。

用法：
    python publish.py --dry-run                  # 不联网，只列出会传哪些文件
    python publish.py --token ghp_xxx            # 真发（仓库不存在会先建）
    python publish.py --token ghp_xxx --check    # 只验 token + 确认仓库在

特性：
- 白名单：只传代码与文档，绝不传 data/ library/ tools/ 配置与登录态
- 幂等：已存在的文件先取 sha 再更新，可反复跑
- 二进制安全：png 走 base64
- 单文件 > 1MB 直接跳过并提示（Content API 硬限制）

token 权限：classic token 勾 `repo`，或 fine-grained 勾 Contents: Read and write。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def _resolve_root() -> Path:
    """在仓库根、工作区根、dist/panharvest 三种位置下都能跑。"""
    here = Path(__file__).resolve().parent
    for cand in (here, here / "dist" / "panharvest"):
        if (cand / "panharvest").is_dir():
            return cand
    return here


ROOT = _resolve_root()

REPO = "panharvest"
DESC = ("一句话 → 网盘资源 → 筛掉假的 → 转存复核真实文件清单 → 下载解压入库。"
        "百度/夸克双后端，纯标准库，37 项自检。")
TOPICS = ["baidu-netdisk", "quark", "pansou", "comic", "ebook", "automation",
          "python", "cli", "self-hosted", "download-manager"]

# —— 白名单：明确列出「要发的东西」，比黑名单安全 ——
ALLOW_FILES = [
    "README.md", "LICENSE", ".gitignore", ".gitattributes", "CONTRIBUTING.md",
    "config.example.json", "harvest.bat", "start-ui.bat",
    "harvest.sh", "start-ui.sh", "publish.py",
]
ALLOW_DIRS = {
    "panharvest": {".py"},
    "tests": {".py", ".js"},
    "docs": {".png"},
    ".github": {".yml", ".yaml"},
}
# 永远不传（防御性二次过滤）
DENY_PARTS = {"__pycache__", "data", "library", "reports", "work", "learning",
              "edited_pages", "slide-forge", "report_figs", "report_figs2", "tools"}
DENY_NAMES = {"config.json", "state.db"}


def collect() -> list[Path]:
    out: list[Path] = []
    for rel in ALLOW_FILES:
        p = ROOT / rel
        if p.is_file():
            out.append(p)
    for d, suffixes in ALLOW_DIRS.items():
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in suffixes:
                continue
            rel_parts = set(p.relative_to(ROOT).parts)
            if rel_parts & DENY_PARTS or p.name in DENY_NAMES:
                continue
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(ROOT).as_posix())


def gh(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "panharvest-publisher")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"message": body[:300]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"message": str(exc)}


def ensure_repo(token: str, user: str, repo: str) -> tuple[bool, str]:
    code, data = gh("GET", f"/repos/{user}/{repo}", token)
    if code == 200:
        return True, "仓库已存在"
    if code not in (404, 0):
        return False, f"查询失败 {code}: {data.get('message')}"
    if code == 0:
        return False, f"连不上 api.github.com：{data.get('message')}（先解决网络或走代理）"
    code, data = gh("POST", "/user/repos", token, {
        "name": repo, "description": DESC, "private": False,
        "has_issues": True, "has_wiki": False, "auto_init": False,
    })
    if code in (200, 201):
        # 顺手把 topics 打上
        gh("PUT", f"/repos/{user}/{repo}/topics", token, {"names": TOPICS})
        return True, f"已创建 {data.get('html_url')}"
    if code == 422:
        return True, "仓库已存在（422）"
    return False, f"创建失败 {code}: {data.get('message')}"


def upload(path: Path, user: str, repo: str, token: str, branch: str, msg: str) -> tuple[bool, str]:
    rel = path.relative_to(ROOT).as_posix()
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        return False, f"{rel}: {len(raw) / 1024:.0f}KB 超过 1MB，Content API 传不了"
    payload = {
        "message": f"{msg}: {rel}",
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": branch,
    }
    quoted = urllib.parse.quote(rel)
    code, data = gh("GET", f"/repos/{user}/{repo}/contents/{quoted}?ref={branch}", token)
    if code == 200 and isinstance(data, dict) and data.get("sha"):
        payload["sha"] = data["sha"]
    code, data = gh("PUT", f"/repos/{user}/{repo}/contents/{quoted}", token, payload)
    if code in (200, 201):
        return True, rel
    return False, f"{rel}: {code} {data.get('message')}"


def main() -> int:
    ap = argparse.ArgumentParser(description="用 REST API 把 panharvest 发到 GitHub")
    ap.add_argument("--token", default="")
    ap.add_argument("--user", default="")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="feat: panharvest 0.1.0")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="只验 token / 仓库，不上传")
    args = ap.parse_args()

    files = collect()
    total = sum(f.stat().st_size for f in files)
    print(f"待上传 {len(files)} 个文件，合计 {total / 1024:.0f} KB")
    if args.dry_run:
        for f in files:
            print(f"  {f.relative_to(ROOT).as_posix():<44} {f.stat().st_size / 1024:>8.1f} KB")
        print("\n（dry-run 不联网。带 --token 真发。）")
        return 0

    if not args.token:
        print("\n缺少 token：python publish.py --token ghp_xxx", file=sys.stderr)
        print("  https://github.com/settings/tokens → classic → 勾 repo", file=sys.stderr)
        return 2

    code, me = gh("GET", "/user", args.token)
    if code != 200:
        print(f"token 无效：{code} {me.get('message')}", file=sys.stderr)
        return 2
    user = me.get("login") or args.user
    print(f"token 有效：{user}")

    ok, msg = ensure_repo(args.token, user, args.repo)
    print(f"仓库：{msg}")
    if not ok or args.check:
        return 0 if ok else 1

    failed: list[str] = []
    for i, f in enumerate(files, 1):
        good, info = upload(f, user, args.repo, args.token, args.branch, args.message)
        print(f"[{i:>2}/{len(files)}] {'OK  ' if good else 'FAIL'} {info}")
        if not good:
            failed.append(info)
        time.sleep(0.35)

    url = f"https://github.com/{user}/{args.repo}"
    print()
    if failed:
        print(f"有 {len(failed)} 个文件失败：")
        for item in failed:
            print("  -", item)
        return 1
    print(f"✅ 发布完成：{url}")
    print("下一步：仓库 About 填描述 → topics 已自动设置 → 打 v0.1.0 tag → 发 Release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
