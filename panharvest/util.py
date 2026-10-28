"""通用工具：日志、HTTP、子进程、路径与文本清洗。

本模块不依赖任何第三方库，Windows / Linux 均可运行。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

CST = timezone(timedelta(hours=8))

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 百度网盘网页版必须带 Referer + 浏览器 UA，否则 shorturlinfo 直接返回错误
UA_PAN = "netdisk;2.2.51.6;netdisk;10.0.63;PC;android-android"

# ---------------------------------------------------------------- 时间 / 体积

def now() -> datetime:
    return datetime.now(CST)


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def human_size(n: float | int | None) -> str:
    if not n:
        return "0B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def parse_size(text: str) -> int:
    """把 '1.2GB' / '800M' / '1024' 解析成字节数，失败返回 0。"""
    if not text:
        return 0
    m = re.match(r"([\d.]+)\s*([KMGTP]?)I?B?", str(text).strip(), re.I)
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(val * mult.get(unit, 1))


# ---------------------------------------------------------------- 日志

_LOG_SINKS: list[Callable[[str], None]] = []


def add_log_sink(fn: Callable[[str], None]) -> None:
    if fn not in _LOG_SINKS:
        _LOG_SINKS.append(fn)


def remove_log_sink(fn: Callable[[str], None]) -> None:
    if fn in _LOG_SINKS:
        _LOG_SINKS.remove(fn)


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{stamp()}] {level:<5} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # 老式 GBK 控制台
        print(line.encode("gbk", "replace").decode("gbk"), flush=True)
    for sink in list(_LOG_SINKS):
        try:
            sink(line)
        except Exception:  # 日志通道绝不能影响主流程
            pass


# ---------------------------------------------------------------- 文本 / 路径

_ILLEGAL = r'<>:"/\|?*'
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}


def safe_filename(name: str, maxlen: int = 120, default: str = "untitled") -> str:
    """生成 Windows 上合法的文件名，保留中文。"""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = "".join("_" if c in _ILLEGAL else c for c in name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = default
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > maxlen:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: maxlen - len(ext) - 1] + "." + ext
        else:
            name = name[:maxlen]
    return name.rstrip(" .") or default


def slugify(text: str, maxlen: int = 48, default: str = "job") -> str:
    """给远程目录/状态用：只留中英文数字，其他变连字符。"""
    text = unicodedata.normalize("NFC", str(text or "")).strip()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text[:maxlen].strip("-") or default)


def decode_console(raw: bytes) -> str:
    """子进程输出解码：先 UTF-8，再 GBK（中文 Windows 控制台），最后替换。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "utf-16-le"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def dir_size(path: str | Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def count_files(path: str | Path) -> int:
    n = 0
    for _root, _dirs, files in os.walk(path):
        n += len(files)
    return n


# ---------------------------------------------------------------- 子进程

class CmdResult:
    __slots__ = ("rc", "out", "err", "cmd", "seconds")

    def __init__(self, rc: int, out: str, err: str, cmd: Sequence[str], seconds: float):
        self.rc, self.out, self.err, self.cmd, self.seconds = rc, out, err, list(cmd), seconds

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return (self.out or "") + (("\n" + self.err) if self.err else "")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CmdResult rc={self.rc} cmd={' '.join(self.cmd[:3])}>"


def run(
    args: Sequence[str],
    cwd: str | Path | None = None,
    timeout: float = 900,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    on_line: Callable[[str], None] | None = None,
    quiet: bool = False,
) -> CmdResult:
    """执行外部命令并捕获输出。on_line 可用于实时进度（逐行回调）。"""
    args = [str(a) for a in args]
    full_env = os.environ.copy()
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    if not quiet:
        log("$ " + " ".join(args[:6]) + (" ..." if len(args) > 6 else ""), "CMD")
    started = time.time()
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return CmdResult(127, "", f"找不到可执行文件: {args[0]}", args, 0.0)

    out_chunks: list[str] = []
    deadline = time.time() + timeout
    try:
        if stdin_text is not None and proc.stdin:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = decode_console(raw).rstrip("\r\n")
            out_chunks.append(line)
            if on_line and line.strip():
                try:
                    on_line(line)
                except Exception:
                    pass
            if time.time() > deadline:
                proc.kill()
                out_chunks.append("[TIMEOUT] 命令超时被终止")
                break
        rc = proc.wait(timeout=max(1, deadline - time.time()))
    except Exception as exc:  # pragma: no cover
        try:
            proc.kill()
        except Exception:
            pass
        rc = -1
        out_chunks.append(f"[ERROR] {exc}")
    text = "\n".join(out_chunks)
    return CmdResult(rc, text, "", args, time.time() - started)


def which(name: str, extra: Iterable[str] = ()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        if cand and Path(cand).is_file():
            return str(cand)
    return None


# ---------------------------------------------------------------- HTTP

class HttpError(Exception):
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status, self.body = status, body


def http_request(
    url: str,
    method: str = "GET",
    data: bytes | dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    retries: int = 2,
    json_body: bool = False,
) -> tuple[int, bytes]:
    hdrs = {"User-Agent": UA_BROWSER, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    body: bytes | None = None
    if isinstance(data, dict):
        if json_body:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        else:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)

    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read()
            except Exception:
                pass
            # 4xx 是确定性错误，不再重试
            if 400 <= exc.code < 500:
                raise HttpError(f"HTTP {exc.code}", exc.code, decode_console(payload)[:500]) from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(1.2 * (attempt + 1))
    raise HttpError(f"请求失败: {last}")


def http_json(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    retries: int = 2,
    json_body: bool = False,
) -> Any:
    status, raw = http_request(url, method, data, headers, timeout, retries, json_body=json_body)
    txt = decode_console(raw).strip()
    if not txt:
        raise HttpError(f"空响应 (HTTP {status})", status)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        raise HttpError(f"响应不是合法 JSON (HTTP {status})", status, txt[:500]) from None


def http_download(url: str, dest: Path, headers: dict[str, str] | None = None,
                  timeout: float = 120, progress: Callable[[int], None] | None = None) -> Path:
    hdrs = {"User-Agent": UA_BROWSER}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1 << 17)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
            if progress:
                progress(total)
    tmp.replace(dest)
    return dest


def retry_call(fn: Callable[[], Any], times: int = 3, delay: float = 1.5,
               desc: str = "操作") -> Any:
    last: Exception | None = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i < times - 1:
                log(f"{desc} 第 {i + 1} 次失败：{exc}", "WARN")
                time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- 其它

def tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def open_path(path: str | Path) -> None:
    """用系统默认程序打开文件/文件夹。"""
    p = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as exc:
        log(f"无法打开 {p}：{exc}", "WARN")
