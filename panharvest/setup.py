"""一键补齐外部组件：7-Zip 控制台（7zr + 7za）、BaiduPCS-Go。

设计要点：
- GitHub 直连经常不通，自动按镜像列表重试（ghproxy.net / gh-proxy.com …）；
- 7-Zip 走官网：先下 7zr.exe（单文件），再用它解出官方 Extra 包里的 7za.exe；
- 所有下载都校验大小，失败不覆盖已有文件。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from . import util
from .config import Config

GITHUB_MIRRORS = ["", "https://ghproxy.net/", "https://gh-proxy.com/"]

SEVEN_OFFICIAL = "https://www.7-zip.org/"
SEVEN_DOWNLOAD = SEVEN_OFFICIAL + "download.html"


def _try_urls(urls: list[str], dest: Path, desc: str, min_bytes: int = 1024) -> bool:
    for url in urls:
        try:
            util.log(f"下载 {desc}：{url}")
            util.http_download(url, dest, timeout=180)
            if dest.is_file() and dest.stat().st_size >= min_bytes:
                util.log(f"完成 {dest.name}（{util.human_size(dest.stat().st_size)}）")
                return True
            util.log(f"文件过小，可能是错误页：{url}", "WARN")
            dest.unlink(missing_ok=True)
        except Exception as exc:
            util.log(f"失败：{exc}", "WARN")
    return False


def install_7zip(tools: Path) -> dict:
    """安装 7zr.exe（7z 解压）+ 7za.exe（zip/7z/iso/AES）。"""
    tools = util.ensure_dir(tools)
    seven_dir = util.ensure_dir(tools / "7z")
    report = {"sevenzr": "", "sevenzip": ""}

    sevenzr = tools / "7zr.exe"
    if not sevenzr.is_file() or sevenzr.stat().st_size < 100_000:
        if _try_urls([SEVEN_OFFICIAL + "a/7zr.exe"], sevenzr, "7zr.exe", 200_000):
            report["sevenzr"] = str(sevenzr)
    else:
        report["sevenzr"] = str(sevenzr)

    sevenzip = seven_dir / "7za.exe"
    if sevenzip.is_file() and sevenzip.stat().st_size > 100_000:
        report["sevenzip"] = str(sevenzip)
        return report

    # 1) 官方 Extra 包（现代版本）：下载 → 用 7zr 解开
    if report["sevenzr"]:
        extra = None
        try:
            _st, raw = util.http_request(SEVEN_DOWNLOAD, timeout=40, retries=1)
            html = util.decode_console(raw)
            found = sorted(set(re.findall(r'href="(a/7z[\d.]+-extra\.7z)"', html)))
            if found:
                extra = SEVEN_OFFICIAL + found[-1]
        except Exception as exc:
            util.log(f"读取 7-Zip 下载页失败：{exc}", "WARN")
        pkg = tools / "7z-extra.7z"
        if extra and _try_urls([extra], pkg, "7-Zip Extra", 200_000):
            res = util.run([report["sevenzr"], "x", "-y", f"-o{seven_dir}", str(pkg)], timeout=300)
            if (seven_dir / "7za.exe").is_file():
                report["sevenzip"] = str(seven_dir / "7za.exe")
                pkg.unlink(missing_ok=True)
                return report
            util.log(f"解包 7-Zip Extra 失败：{util.tail(res.text, 3)}", "WARN")

    # 2) 兜底：老版独立 7za920.zip（9.20，够用且只有一个 exe）
    legacy = tools / "7za920.zip"
    if _try_urls([SEVEN_OFFICIAL + "a/7za920.zip"], legacy, "7za 9.20", 100_000):
        try:
            with zipfile.ZipFile(legacy) as zf:
                for name in zf.namelist():
                    if name.lower().endswith("7za.exe"):
                        with zf.open(name) as src, open(seven_dir / "7za.exe", "wb") as out:
                            out.write(src.read())
                        report["sevenzip"] = str(seven_dir / "7za.exe")
        except zipfile.BadZipFile as exc:
            util.log(f"7za 解包失败：{exc}", "WARN")
        legacy.unlink(missing_ok=True)
    return report


def install_baidupcs(tools: Path) -> str:
    """下载 BaiduPCS-Go（优先问 GitHub API 要最新版，失败则用已知版本）。"""
    tools = util.ensure_dir(tools)
    target_dir = util.ensure_dir(tools / "baidupcs")
    existing = next(iter(tools.rglob("BaiduPCS-Go.exe")), None)
    if existing:
        return str(existing)

    asset_name = "BaiduPCS-Go-v4.0.2-windows-x64.zip"
    release_path = f"qjfoidnh/BaiduPCS-Go/releases/download/v4.0.2/{asset_name}"
    try:
        data = util.http_json(
            "https://api.github.com/repos/qjfoidnh/BaiduPCS-Go/releases/latest",
            headers={"Accept": "application/vnd.github+json"}, timeout=25, retries=1)
        tag = data.get("tag_name") or ""
        for asset in data.get("assets") or []:
            name = asset.get("name") or ""
            if "windows-x64" in name:
                asset_name = name
                release_path = f"qjfoidnh/BaiduPCS-Go/releases/download/{tag}/{name}"
                break
    except Exception as exc:
        util.log(f"查询最新版本失败（用内置版本号）：{exc}", "WARN")

    pkg = tools / "baidupcs.zip"
    urls = [f"{mirror}https://github.com/{release_path}" for mirror in GITHUB_MIRRORS]
    if not _try_urls(urls, pkg, "BaiduPCS-Go", 1_000_000):
        raise RuntimeError(
            "下载 BaiduPCS-Go 失败。可以手动下载后解压到 tools/baidupcs/：\n"
            f"    https://github.com/{release_path}")
    with zipfile.ZipFile(pkg) as zf:
        zf.extractall(target_dir)
    pkg.unlink(missing_ok=True)
    exe = next(iter(target_dir.rglob("BaiduPCS-Go.exe")), None)
    if not exe:
        raise RuntimeError("压缩包里没找到 BaiduPCS-Go.exe")
    return str(exe)


def run_setup(cfg: Config, want_baidupcs: bool = True) -> dict:
    tools = cfg.path_of("paths.tools")
    util.log(f"组件目录：{tools}")
    out: dict = {}
    out.update(install_7zip(tools))
    if want_baidupcs:
        try:
            out["baidupcs"] = install_baidupcs(tools)
        except Exception as exc:
            out["baidupcs"] = ""
            util.log(str(exc), "ERROR")
    util.log("=" * 56)
    for key, value in out.items():
        util.log(f"{key:<10} {'✔ ' + value if value else '✘ 未安装'}")
    util.log("提示：装了 BaiduPCS-Go 之后还需要登录一次（harvest login）才能自动转存。")
    return out


def write_example_config(path: Path) -> Path:
    from .config import DEFAULTS

    util.ensure_dir(path.parent)
    path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
