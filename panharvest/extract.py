"""解压引擎：zip / 7z / rar / tar 全格式 + 密码字典 + 嵌套压缩包。

设计原则：
1. 不依赖 pip 包（zip/tar 用标准库，rar 用本机 UnRAR，7z 用 7z/7zr）；
2. 密码错误时**不留半成品**，先验证再解压；
3. 一个压缩包试出来的密码，会自动用于同一批的其它包（网盘资源几乎都是同一个密码）。
"""
from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import util

ZIP_EXT = {".zip", ".cbz"}
RAR_EXT = {".rar", ".cbr"}
SEVEN_EXT = {".7z"}
TAR_EXT = {".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz", ".gz", ".bz2", ".xz"}
ARCHIVE_EXT = ZIP_EXT | RAR_EXT | SEVEN_EXT | TAR_EXT | {".001", ".iso"}

# 分卷：只有第一份需要处理，其余由解压器自动带上
_VOLUME_LATER = re.compile(r"\.(z\d{2}|r\d{2}|0\d{2}|\d{3})$", re.I)
_PART_LATER = re.compile(r"\.part(\d+)\.rar$", re.I)


@dataclass
class ExtractResult:
    ok: bool = False
    archives: list[dict] = field(default_factory=list)
    output_dirs: list[str] = field(default_factory=list)
    files: int = 0
    passwords_used: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "files": self.files, "errors": self.errors,
            "archives": self.archives, "passwords_used": self.passwords_used,
            "output_dirs": self.output_dirs,
        }


def is_archive(path: str | Path) -> bool:
    name = Path(path).name.lower()
    if name.endswith((".part1.rar", ".part01.rar")):
        return True
    if name.endswith(".tar.gz") or name.endswith(".tar.bz2") or name.endswith(".tar.xz"):
        return True
    return Path(name).suffix.lower() in ARCHIVE_EXT


def is_volume_part(path: str | Path) -> bool:
    name = Path(path).name
    m = _PART_LATER.search(name)
    if m:
        return int(m.group(1)) > 1
    return bool(_VOLUME_LATER.search(name)) and not name.lower().endswith((".001",))


def find_archives(root: str | Path, skip: Iterable[str] = ()) -> list[Path]:
    root = Path(root)
    skip_set = {str(Path(s).resolve()).lower() for s in skip if s}
    out: list[Path] = []
    for cur, _dirs, files in os.walk(root):
        for f in files:
            p = Path(cur) / f
            if str(p.resolve()).lower() in skip_set:
                continue
            if is_archive(p) and not is_volume_part(p):
                out.append(p)
    return sorted(out, key=lambda p: str(p).lower())


# ---------------------------------------------------------------- 密码

_PWD_PATTERNS = [
    re.compile(r"(?:解压密码|解压缩密码|压缩密码|密码)\s*[:：=]?\s*([^\s，,。;；、）)】\]]{2,24})"),
    re.compile(r"(?:pwd|passwd|password)\s*[:：=]?\s*([^\s，,。;；、）)】\]]{2,24})", re.I),
]
_PWD_FILENAMES = ("密码", "解压密码", "password", "说明", "readme", "必看")


def gather_passwords(cand: dict | None, src: str | Path, config_password_list: Iterable[str] = ()) -> list[str]:
    """按可能性排序的密码候选。"""
    out: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip().strip("\"'")
        if value and value not in out and len(value) <= 64:
            out.append(value)

    cand = cand or {}
    add(cand.get("password", ""))          # 提取码复用为解压密码：最常见
    for field in ("title", "note"):
        text = str(cand.get(field, ""))
        for pat in _PWD_PATTERNS:
            for m in pat.finditer(text):
                add(m.group(1))
    # 压缩包旁边常有 密码.txt / 说明.txt
    try:
        for entry in Path(src).rglob("*"):
            if entry.is_file() and entry.suffix.lower() == ".txt" and any(
                k in entry.name.lower() for k in _PWD_FILENAMES
            ):
                try:
                    text = entry.read_text(encoding="utf-8", errors="ignore")[:2000]
                except OSError:
                    continue
                for pat in _PWD_PATTERNS:
                    for m in pat.finditer(text):
                        add(m.group(1))
                if not any(pat.search(text) for pat in _PWD_PATTERNS):
                    stripped = text.strip()
                    if 2 <= len(stripped) <= 32 and "\n" not in stripped:
                        add(stripped)
    except OSError:
        pass
    for pw in config_password_list or []:
        add(str(pw))
    return out


# ---------------------------------------------------------------- 单包解压

def _console_hint(cfg: dict) -> dict:
    return {
        "sevenzip": cfg.get("sevenzip") or "",
        "sevenzr": cfg.get("sevenzr") or "",
        "unrar": cfg.get("unrar") or "",
    }


def _zip_names_fixed(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """修补 Windows 下 GBK 文件名乱码：未设 UTF-8 标记时按 cp437→gbk 还原。"""
    infos = zf.infolist()
    for info in infos:
        if info.flag_bits & 0x800:
            continue
        try:
            raw = info.filename.encode("cp437")
        except UnicodeEncodeError:
            continue
        for enc in ("gbk", "utf-8"):
            try:
                info.filename = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    return infos


def _safe_member(name: str, dest: Path) -> Path | None:
    """防 Zip-Slip：成员路径不得逃出目标目录。"""
    clean = name.replace("\\", "/").lstrip("/")
    if not clean or clean.startswith(".."):
        return None
    target = (dest / clean).resolve()
    try:
        target.relative_to(dest.resolve())
    except ValueError:
        return None
    return target


def _zip_extract(path: Path, dest: Path, passwords: list[str]) -> tuple[bool, str, str]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return False, "", f"不是有效 zip：{exc}"
    except OSError as exc:
        return False, "", f"打开失败：{exc}"
    with zf:
        infos = [i for i in _zip_names_fixed(zf) if not i.is_dir()]
        if not infos:
            return False, "", "压缩包里没有文件"
        encrypted = any(i.flag_bits & 0x1 for i in infos)
        attempts: list[str | None] = list(passwords) if encrypted else [None]
        if encrypted and not attempts:
            attempts = [""]
        for pwd in attempts:
            try:
                # 先用第一个加密成员验证密码，避免留下半成品
                probe = next((i for i in infos if i.flag_bits & 0x1), infos[0])
                if encrypted and pwd is not None:
                    with zf.open(probe, pwd=pwd.encode("utf-8")) as fh:
                        fh.read(1)
                for info in infos:
                    target = _safe_member(info.filename, dest)
                    if target is None:
                        continue
                    if info.is_dir():
                        util.ensure_dir(target)
                        continue
                    util.ensure_dir(target.parent)
                    with zf.open(info, pwd=(pwd.encode("utf-8") if (encrypted and pwd) else None)) as src, \
                            open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                return True, (pwd or ""), ""
            except NotImplementedError:
                return False, "", "该 zip 使用 AES 加密，需要安装 7-Zip"
            except RuntimeError as exc:
                msg = str(exc)
                if "password" in msg.lower() or "encrypted" in msg.lower():
                    continue
                return False, "", f"解压异常：{msg}"
            except (zipfile.BadZipFile, OSError) as exc:
                return False, "", f"解压异常：{exc}"
    return False, "", "所有密码都试过了，解压失败"


def _run_external(tool: str, args: list[str], dest: Path, timeout: float) -> tuple[bool, str, str]:
    res = util.run([tool, *args], timeout=timeout)
    rc, text = res.rc, res.text or ""
    lowered = text.lower()
    if rc == 11 or "wrong password" in lowered or "密码错误" in text:
        return False, "", "密码错误"
    if rc in (0, 1) and "错误" not in text:
        return True, "", ""
    if rc == 0:
        return True, "", ""
    return False, "", util.tail(text, 4) or f"返回码 {rc}"


def _rar_extract(path: Path, dest: Path, passwords: list[str], cfg: dict, timeout: float) -> tuple[bool, str, str]:
    unrar = cfg.get("unrar") or ""
    sevenzip = cfg.get("sevenzip") or ""
    if unrar:
        attempts: list[str | None] = [None, *passwords]
        last_msg = ""
        for pwd in attempts:
            pw_arg = "-p-" if pwd is None else f"-p{pwd}"
            ok, _, msg = _run_external(
                unrar, ["x", "-y", "-o+", pw_arg, str(path), str(dest) + os.sep], dest, timeout
            )
            if ok:
                return True, pwd or "", ""
            last_msg = msg
            if pwd is None and "密码错误" not in msg:
                break  # 不是密码问题（比如文件损坏），换 7z 再试
        if sevenzip:
            ok, pwd, msg = _seven_extract(path, dest, passwords, cfg, timeout)
            if ok:
                return ok, pwd, msg
            return False, "", msg or last_msg
        return False, "", last_msg or "解压失败"
    if sevenzip:
        return _seven_extract(path, dest, passwords, cfg, timeout)
    return False, "", "未找到 UnRAR 或 7-Zip，无法解压 rar"


def _seven_extract(path: Path, dest: Path, passwords: list[str], cfg: dict, timeout: float) -> tuple[bool, str, str]:
    tool = cfg.get("sevenzip") or cfg.get("sevenzr") or ""
    if not tool:
        return False, "", "未找到 7-Zip（可运行 setup 自动下载 7zr.exe）"
    attempts: list[str | None] = [None, *passwords]
    last = ""
    for pwd in attempts:
        args = ["x", "-y", f"-o{dest}"]
        if pwd:                      # 空密码时不要传 -p，否则 7z 会转去交互式询问
            args.append(f"-p{pwd}")
        args.append(str(path))
        ok, _, msg = _run_external(tool, args, dest, timeout)
        if ok:
            return True, pwd or "", ""
        last = msg
        if "密码" not in msg and "password" not in msg.lower():
            break
    return False, "", last or "解压失败"


def _tar_extract(path: Path, dest: Path, timeout: float) -> tuple[bool, str, str]:
    try:
        with tarfile.open(path, "r:*", encoding="utf-8", errors="replace") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                target = _safe_member(member.name, dest)
                if target is None:
                    continue
                util.ensure_dir(target.parent)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return True, "", ""
    except (tarfile.TarError, OSError) as exc:
        return False, "", f"tar 解压失败：{exc}"


def extract_one(path: Path, dest: Path, passwords: list[str], cfg: dict,
                timeout: float = 3600) -> tuple[bool, str, str, str]:
    """解压单个文件。返回 (ok, 使用的密码, 错误信息, 使用的工具)。"""
    util.ensure_dir(dest)
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    if suffix in ZIP_EXT:
        ok, pwd, msg = _zip_extract(path, dest, passwords)
        if not ok and (cfg.get("sevenzip") or cfg.get("sevenzr")):
            # AES 加密 / 特殊压缩方法：python zipfile 搞不定，交给 7-Zip
            util.log(f"zipfile 解压失败（{msg}），改用 7-Zip 重试")
            ok2, pwd2, msg2 = _seven_extract(path, dest, passwords, cfg, timeout)
            if ok2:
                return True, pwd2, "", "7z"
            return False, "", msg2 or msg, "7z"
        return ok, pwd, msg, "zipfile"
    if suffix in RAR_EXT or lower_name.endswith(".part1.rar"):
        ok, pwd, msg = _rar_extract(path, dest, passwords, cfg, timeout)
        return ok, pwd, msg, "unrar" if cfg.get("unrar") else "7z"
    if suffix in SEVEN_EXT or suffix == ".001":
        ok, pwd, msg = _seven_extract(path, dest, passwords, cfg, timeout)
        return ok, pwd, msg, "7z"
    if suffix in {".tar", ".tgz", ".gz", ".bz2", ".xz"} or lower_name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        ok, pwd, msg = _tar_extract(path, dest, timeout)
        return ok, "", msg, "tarfile"
    if suffix == ".iso":
        ok, pwd, msg = _seven_extract(path, dest, passwords, cfg, timeout)
        return ok, pwd, msg, "7z"
    return False, "", f"不支持的压缩格式 {suffix or path.name}", ""


# ---------------------------------------------------------------- 批量 + 嵌套

def extract_all(
    src: str | Path,
    passwords: list[str],
    cfg: dict,
    work_root: str | Path | None = None,
    max_depth: int = 3,
    timeout: float = 3600,
    delete_archives: bool = False,
    on_event: Callable[[str], None] | None = None,
) -> ExtractResult:
    """把 src 下所有压缩包解开（含嵌套），输出目录与 src 平级。"""
    src = Path(src)
    work_root = Path(work_root) if work_root else src
    result = ExtractResult()
    log = on_event or (lambda m: util.log(m))

    pending: list[tuple[Path, int]] = [(p, 0) for p in find_archives(src)]
    if not pending:
        log("下载目录里没有压缩包，跳过解压")
        result.ok = True
        result.output_dirs = [str(src)]
        return result

    known_pwd: list[str] = list(dict.fromkeys(passwords))
    processed: set[str] = set()
    produced: list[Path] = []

    while pending:
        archive, depth = pending.pop(0)
        key = str(archive.resolve()).lower()
        if key in processed or not archive.exists():
            continue
        processed.add(key)
        if depth > max_depth:
            result.skipped.append(str(archive))
            log(f"嵌套层数超过 {max_depth}，跳过 {archive.name}")
            continue

        stem = archive.name
        for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        else:
            stem = archive.stem
        if stem.lower().endswith((".part1", ".part01")):
            stem = stem.rsplit(".", 1)[0]
        out_dir = work_root / util.safe_filename(stem, 100, "unpacked")
        # 避免覆盖同名输出
        n = 2
        base = out_dir
        while out_dir.exists() and any(out_dir.iterdir()):
            out_dir = base.with_name(f"{base.name}-{n}")
            n += 1

        log(f"解压 {archive.name} -> {out_dir.name}")
        ok, used_pwd, msg, tool = extract_one(archive, out_dir, known_pwd, cfg, timeout)
        entry = {
            "path": str(archive), "output": str(out_dir), "ok": ok, "tool": tool,
            "password": used_pwd, "message": msg, "depth": depth,
        }
        result.archives.append(entry)
        if ok:
            if used_pwd:
                result.passwords_used[str(archive)] = used_pwd
                if used_pwd not in known_pwd:
                    known_pwd.insert(0, used_pwd)
            produced.append(out_dir)
            if delete_archives:
                try:
                    archive.unlink()
                except OSError:
                    pass
            # 找嵌套压缩包
            for nested in find_archives(out_dir):
                pending.append((nested, depth + 1))
        else:
            err = f"{archive.name}: {msg}"
            result.errors.append(err)
            log(f"解压失败 {err}", "WARN")
            if out_dir.exists() and not any(out_dir.iterdir()):
                shutil.rmtree(out_dir, ignore_errors=True)

    result.output_dirs = [str(p) for p in produced] or [str(src)]
    result.files = sum(util.count_files(p) for p in produced)
    result.ok = bool(produced) or not result.errors
    return result


def media_files(root: str | Path, ext_allow: Iterable[str]) -> list[Path]:
    allow = {e.lower() for e in ext_allow}
    out: list[Path] = []
    for cur, _dirs, files in os.walk(root):
        for f in files:
            if Path(f).suffix.lower() in allow:
                out.append(Path(cur) / f)
    return out
