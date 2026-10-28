"""配置加载：config.json + 内置默认值 + 路径解析 + 外部工具探测。"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import util

ROOT = Path(__file__).resolve().parent.parent  # 项目根目录

DEFAULTS: dict[str, Any] = {
    "pansou": {
        # PanSou 服务地址：docker run -d -p 8888:8888 ghcr.io/fish2018/pansou:latest
        "base": "http://127.0.0.1:8888",
        "token": "",
        "timeout": 90,
        "refresh": False,
    },
    "paths": {
        "data": "data",
        "downloads": "data/downloads",
        "library": "library",
        "inbox": "data/inbox",
        "tools": "tools",
        "reports": "reports",
    },
    "backends": {
        # auto   = 有 BaiduPCS-Go 且已登录就走自动，否则退回 manual
        # pcs    = 强制自动（未就绪直接报错）
        # manual = 只出链接和提取码，人工转存后放进 inbox
        "transfer": "auto",
        "download": "auto",
    },
    "baidupcs": {
        "exe": "",
        "config_dir": "data/baidupcs-config",
        "remote_root": "/资源收割",
        "threads": 0,          # 0 = 用 BaiduPCS-Go 自身默认
        "max_files": 2,        # 同时下载文件数
        "keep_remote": True,   # 下载完是否保留网盘内转存副本
        "download_mode": "locate",
    },
    "limits": {
        "max_candidates": 20,
        "min_score": 45,       # 低于此分不自动下载
        "max_total_gb": 60,    # 单个资源体积上限
        "max_files": 5000,     # 单个资源文件数上限
        "search_variants": 3,
        "verify_top": 12,      # 最多对前 N 个候选做深度校验
    },
    "extract": {
        "sevenzip": "",
        "sevenzr": "",
        "unrar": "",
        "max_depth": 3,
        "delete_archives": False,
        "timeout": 3600,
        "passwords": [
            "1234", "123456", "2048", "1024", "123", "0000",
            "www.pan.baidu.com", "baidu", "www.baidu.com",
            "tmd", "解压密码", "66668888",
        ],
    },
    "organize": {
        "flatten": True,
        "min_image_bytes": 2048,
        "junk_suffix": [".url", ".lnk", ".htm", ".html", ".torrent", ".txt"],
        "junk_keywords": [
            "公众号", "微信", "加群", "群号", "推广", "广告", "更多资源",
            "免费下载", "扫码", "关注", "www.", "http", "解压密码", "免责声明",
            "本资源", "版权",
        ],
        "media_ext": [
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
            ".pdf", ".epub", ".mobi", ".azw3", ".azw", ".txt", ".cbz", ".cbr",
            ".mp4", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".rmvb",
            ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
        ],
    },
    "komga": {
        "url": "",
        "user": "",
        "password": "",
        "library_id": "",
    },
    "ui": {
        "port": 8765,
        "open_browser": True,
    },
    "clouds": ["baidu", "quark", "aliyun", "115", "uc", "xunlei", "123"],
    "prefer_clouds": ["baidu"],
    "open_when_done": True,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


class Config:
    def __init__(self, data: dict[str, Any], root: Path = ROOT, source: str = "<defaults>"):
        self.raw = data
        self.root = Path(root)
        self.source = source

    # ------------------------------------------------------------ 构造
    @classmethod
    def load(cls, path: str | Path | None = None, root: Path = ROOT) -> "Config":
        root = Path(root)
        candidates: list[Path] = []
        if path:
            candidates.append(Path(path))
        else:
            candidates += [
                root / "config.json",
                Path.cwd() / "config.json",
                root / "config.example.json",
            ]
        source = "<defaults>"
        data = copy.deepcopy(DEFAULTS)
        for cand in candidates:
            if cand.is_file():
                try:
                    user = json.loads(cand.read_text(encoding="utf-8-sig"))
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"配置文件 {cand} 不是合法 JSON：{exc}") from None
                data = _deep_merge(data, user)
                source = str(cand)
                break
        cfg = cls(data, root, source)
        cfg._autodetect()
        return cfg

    def _autodetect(self) -> None:
        """自动找到 7-Zip / 7za / 7zr / UnRAR，用户手填优先。"""
        ex = self.raw["extract"]
        tools = self.path_of("tools")
        zone = [str(tools / "7z" / "7za.exe"), str(tools / "7za.exe"),
                str(tools / "7za920" / "7za.exe")]
        pf = [r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"]
        unrar_cands = [
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
        ]
        ex["sevenzip"] = (ex.get("sevenzip") or util.which("7z", pf)
                          or util.which("7za", zone) or "")
        ex["sevenzr"] = ex.get("sevenzr") or util.which(
            "7zr", [str(tools / "7zr.exe")]
        ) or ""
        ex["unrar"] = ex.get("unrar") or util.which("unrar", unrar_cands) or ""

    # ------------------------------------------------------------ 读取
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_of(self, dotted: str) -> Path:
        """取路径类配置，并把相对路径解析到项目根目录。"""
        val = self.get(dotted) or dotted.split(".")[-1]
        p = Path(str(val)).expanduser()
        if not p.is_absolute():
            p = self.root / p
        return p

    def paths(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for key in ("data", "downloads", "library", "inbox", "tools", "reports"):
            out[key] = self.path_of(f"paths.{key}")
        return out

    def ensure_dirs(self) -> dict[str, Path]:
        paths = self.paths()
        for p in paths.values():
            util.ensure_dir(p)
        return paths

    # ------------------------------------------------------------ 诊断
    def summary(self) -> list[tuple[str, str, str]]:
        """(项目, 状态, 说明) —— 给 doctor / Web UI 用。"""
        ex = self.raw["extract"]
        rows = [
            ("PanSou", self.get("pansou.base"), "搜索 + 链接存活检测服务"),
            ("Python", sys.version.split()[0], sys.executable),
            ("7-Zip", ex.get("sevenzip") or "未安装", "解压 .7z / AES 加密 zip"),
            ("7zr", ex.get("sevenzr") or "未安装", "解压 .7z（可自动下载）"),
            ("UnRAR", ex.get("unrar") or "未安装", "解压 .rar / 分卷"),
            ("BaiduPCS-Go", self.resolve_pcs_exe() or "未安装", "自动转存 + 下载"),
            ("下载目录", str(self.path_of("paths.downloads")), ""),
            ("书库目录", str(self.path_of("paths.library")), ""),
        ]
        return rows

    def resolve_pcs_exe(self) -> str | None:
        configured = self.get("baidupcs.exe")
        if configured:
            p = Path(configured)
            if not p.is_absolute():
                p = self.root / p
            if p.is_file():
                return str(p)
        tools = self.path_of("paths.tools")
        for name in ("BaiduPCS-Go.exe", "BaiduPCS-Go"):
            direct = util.which(name, [str(tools / name)])
            if direct:
                return direct
        if tools.is_dir():
            for cand in tools.rglob("BaiduPCS-Go.exe"):
                return str(cand)
            for cand in tools.rglob("BaiduPCS-Go"):
                if cand.is_file():
                    return str(cand)
        return util.which("BaiduPCS-Go")

    def save_example(self, path: Path) -> Path:
        util.ensure_dir(path.parent)
        path.write_text(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path


def load(path: str | Path | None = None) -> Config:
    return Config.load(path)


if __name__ == "__main__":  # pragma: no cover
    cfg = load()
    print(json.dumps(cfg.raw, ensure_ascii=False, indent=2))
    for row in cfg.summary():
        print(" | ".join(row))
