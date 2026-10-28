"""清洗 + 归档：把解压出来的东西整理成"能直接打开看"的书库结构。

做的事：
1. 删广告/系统垃圾（公众号.txt、__MACOSX、Thumbs.db、0 字节文件…）
2. 剥掉多余的一层壳目录（很多人压缩时多套了一层文件夹）
3. 统一命名（非法字符、超长名、重复后缀）
4. 搬进 library/<系列名>/，同名同大小自动跳过（可重复执行）
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import util
from .intent import Intent

JUNK_DIRS = {"__MACOSX", ".git", "@eadir", ".@__thumb", "$recycle.bin", "system volume information"}
JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini", "readme.txt", "readme.md", ".gitignore"}
JUNK_EXT = {".url", ".lnk", ".torrent", ".db", ".ini", ".cfg", ".log", ".bak", ".part", ".crdownload"}
# 这些扩展名是"内容"，无论什么类型都保留
ALWAYS_KEEP = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff",
               ".pdf", ".epub", ".mobi", ".azw3", ".azw", ".djvu", ".cbz", ".cbr",
               ".mp4", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".rmvb", ".m2ts", ".mov",
               ".zip", ".7z", ".rar", ".tar", ".gz", ".txt", ".doc", ".docx",
               ".ppt", ".pptx", ".xls", ".xlsx", ".html", ".htm"}

_AD_WORDS = ("公众号", "微信", "加群", "群号", "扫码", "关注", "推广", "广告",
             "更多资源", "免费下载", "免责声明", "本资源", "版权", "www.", "http")
_PWD_NAMES = ("密码", "解压密码", "password", "说明", "必看", "readme")


@dataclass
class OrganizeResult:
    target: str = ""
    moved: int = 0
    skipped: int = 0
    junk: int = 0
    renamed: int = 0
    files: int = 0
    size: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target": self.target, "moved": self.moved, "skipped": self.skipped,
            "junk": self.junk, "renamed": self.renamed, "files": self.files,
            "size": self.size, "notes": self.notes,
        }


def _looks_like_ad(path: Path) -> bool:
    name = path.name.lower()
    if any(name == j or name.startswith(j) for j in JUNK_FILES):
        return True
    if path.suffix.lower() in JUNK_EXT:
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return True
    if path.suffix.lower() in {".txt", ".htm", ".html"} and size < 64 * 1024:
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:600]
        except OSError:
            return False
        hits = sum(1 for w in _AD_WORDS if w.lower() in head.lower())
        return hits >= 2
    return False


def clean_junk(root: str | Path, intent: Intent | None = None) -> tuple[int, list[str]]:
    """删掉垃圾文件，返回 (删除数量, 删除清单)。"""
    root = Path(root)
    removed = 0
    notes: list[str] = []
    keep_txt = bool(intent and intent.type == "book")

    for cur, dirs, files in os.walk(root, topdown=False):
        cur_path = Path(cur)
        for d in list(dirs):
            if d.lower() in JUNK_DIRS:
                shutil.rmtree(cur_path / d, ignore_errors=True)
                dirs.remove(d)
                removed += 1
                notes.append(f"删除目录 {d}")
        for f in files:
            p = cur_path / f
            if keep_txt and p.suffix.lower() == ".txt":
                # 书籍类型：大 txt 可能是正文，不当作广告
                try:
                    if p.stat().st_size > 8 * 1024:
                        continue
                except OSError:
                    pass
            if _looks_like_ad(p):
                try:
                    p.unlink()
                    removed += 1
                    notes.append(f"删除 {p.name}")
                except OSError:
                    continue
        try:
            if cur_path != root and not any(cur_path.iterdir()):
                cur_path.rmdir()
        except OSError:
            pass
    return removed, notes


def _content_root(path: Path, max_unwrap: int = 3) -> Path:
    """剥掉多余的单层壳目录。"""
    cur = path
    for _ in range(max_unwrap):
        entries = [e for e in cur.iterdir() if not e.name.startswith(".")]
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        if len(dirs) == 1 and not files:
            cur = dirs[0]
            continue
        break
    return cur


def _unique_target(target: Path, existing: set[tuple[str, int]]) -> Path | None:
    """同名同大小 → 跳过；同名不同大小 → 加序号。"""
    if not target.exists():
        return target
    try:
        key = (target.name.lower(), target.stat().st_size)
    except OSError:
        key = (target.name.lower(), 0)
    if key in existing:
        return None
    stem, suffix = target.stem, target.suffix
    for i in range(2, 100):
        cand = target.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return None


def _move_file(src: Path, dest: Path, result: OrganizeResult,
               existing: set[tuple[str, int]]) -> None:
    util.ensure_dir(dest.parent)
    name = util.safe_filename(dest.name, 120)
    if name != dest.name:
        dest = dest.with_name(name)
        result.renamed += 1
    chosen = _unique_target(dest, existing)
    if chosen is None:
        result.skipped += 1
        return
    try:
        size = src.stat().st_size
        shutil.move(str(src), str(chosen))
        result.moved += 1
        result.size += size
        existing.add((chosen.name.lower(), size))
    except (OSError, shutil.Error) as exc:
        result.notes.append(f"移动失败 {src.name}: {exc}")
        result.skipped += 1


def organize(
    sources: Iterable[str | Path],
    intent: Intent,
    library_dir: str | Path,
    media_ext: Iterable[str] | None = None,
    flatten: bool = True,
    series_name: str = "",
) -> OrganizeResult:
    """把若干解压产物整理进书库，返回结果。"""
    result = OrganizeResult()
    library = util.ensure_dir(library_dir)
    allow = {e.lower() for e in (media_ext or ALWAYS_KEEP)}
    series = util.safe_filename(series_name or intent.title or "未命名资源", 80)
    target_root = library / series
    util.ensure_dir(target_root)

    existing: set[tuple[str, int]] = set()
    for cur, _dirs, files in os.walk(target_root):
        for f in files:
            try:
                existing.add((f.lower(), (Path(cur) / f).stat().st_size))
            except OSError:
                pass

    for src in sources:
        src = Path(src)
        if not src.exists():
            continue
        root = _content_root(src) if flatten else src
        entries = [e for e in root.iterdir() if not e.name.startswith(".")] if root.is_dir() else []
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]

        # 情况 A：子目录（按书目/按卷）——每个目录整体搬到同名子目录下
        for d in dirs:
            dest_dir = target_root / util.safe_filename(d.name, 100)
            for cur, _ds, fs in os.walk(d):
                rel = Path(cur).relative_to(d)
                for f in fs:
                    p = Path(cur) / f
                    if p.suffix.lower() not in allow:
                        result.skipped += 1
                        continue
                    _move_file(p, dest_dir / rel / f, result, existing)

        # 情况 B：根目录下直接摆着的文件——搬进系列目录
        for f in files:
            p = f  # iterdir 已经是完整（相对 root 拼接过的）路径，不要再拼一次
            if p.suffix.lower() not in allow:
                result.skipped += 1
                continue
            _move_file(p, target_root / p.name, result, existing)

    result.target = str(target_root)
    result.files = util.count_files(target_root)
    result.size = util.dir_size(target_root)
    return result


def library_manifest(library_dir: str | Path) -> list[dict]:
    """书库里有什么，给首页/报告用。"""
    library = Path(library_dir)
    if not library.is_dir():
        return []
    out: list[dict] = []
    from datetime import datetime

    for entry in sorted(library.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            files = util.count_files(entry)
            size = util.dir_size(entry)
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, util.CST).strftime("%Y-%m-%d %H:%M")
            out.append({
                "name": entry.name, "path": str(entry), "files": files,
                "size": size, "mtime": mtime,
            })
    return out
