"""BaiduPCS-Go 封装：转存 → 列目录 → 下载。

命令与输出格式均按 qjfoidnh/BaiduPCS-Go v4.0.2 源码核对：
- transfer 只能转存到「当前工作目录」，所以必须先 cd（状态存在配置文件里）
- 成功输出：转存成功, 保存了<文件名>到当前目录
- ls 输出是 tablewriter 表格，目录名带结尾 "/"，末行是 总计
- download 输出：[id] 下载完成, 保存位置: <path> / [id] 文件已经存在: <path>, 跳过...
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Iterable

from . import util

# "当前帐号 uid: 123456, 用户名: xxx"
_WHO_RE = re.compile(r"uid:\s*(\d+)")
_TRANSFER_OK = re.compile(r"转存成功.*?保存了(.+?)到当前目录")
_TRANSFER_FAIL = re.compile(r"转存失败[:：]\s*(.+)")
_SAVED = re.compile(r"保存位置[:：]\s*(.+)")
_EXISTS = re.compile(r"文件已经存在[:：]\s*(.+?),\s*跳过")
_DL_PREP = re.compile(r"准备下载[:：]\s*(.+)")


class PcsError(RuntimeError):
    pass


def parse_ls(text: str) -> dict:
    """解析 `BaiduPCS-Go ls` 的表格输出。"""
    entries: list[dict] = []
    total_size = 0
    file_total = 0
    dir_total = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0] == "#":
            continue
        size_cell, name_cell = cells[1], cells[-1]
        if "总:" in size_cell or "文件总数" in name_cell:
            m = re.search(r"总:\s*([\d.]+\s*[KMGT]?i?B?)", size_cell)
            if m:
                total_size = util.parse_size(m.group(1))
            m = re.search(r"文件总数:\s*(\d+)", name_cell)
            if m:
                file_total = int(m.group(1))
            m = re.search(r"目录总数:\s*(\d+)", name_cell)
            if m:
                dir_total = int(m.group(1))
            continue
        if cells[0].isdigit():
            is_dir = name_cell.endswith("/")
            entries.append({
                "name": name_cell.rstrip("/"),
                "size": 0 if size_cell in ("-", "") else util.parse_size(size_cell),
                "isdir": is_dir,
                "wrapped": False,
            })
        elif entries and name_cell:
            # tablewriter 把过长文件名换行，续行只有名字
            entries[-1]["name"] += name_cell
            entries[-1]["wrapped"] = True
    if not total_size:
        total_size = sum(e["size"] for e in entries)
    if not file_total:
        file_total = sum(1 for e in entries if not e["isdir"])
    return {"entries": entries, "total_size": total_size,
            "file_total": file_total, "dir_total": dir_total}


class BaiduPCS:
    """BaiduPCS-Go 的命令行外壳。所有方法失败都会抛 PcsError 或返回 ok=False。"""

    def __init__(
        self,
        exe: str,
        config_dir: str | Path,
        remote_root: str = "/资源收割",
        threads: int = 0,
        max_files: int = 2,
        mode: str = "locate",
        timeout: float = 900,
        on_line: Callable[[str], None] | None = None,
    ):
        self.exe = str(exe)
        self.config_dir = str(config_dir)
        self.remote_root = remote_root.rstrip("/") or "/资源收割"
        self.threads = int(threads or 0)
        self.max_files = int(max_files or 0)
        self.mode = mode or "locate"
        self.timeout = timeout
        self.on_line = on_line
        self._lock = threading.RLock()
        util.ensure_dir(self.config_dir)

    # ------------------------------------------------------------ 底层
    def _env(self) -> dict[str, str]:
        return {"BAIDUPCS_GO_CONFIG_DIR": self.config_dir}

    def run(self, args: Iterable[str], timeout: float | None = None,
            on_line: Callable[[str], None] | None = None) -> util.CmdResult:
        cmd = [self.exe, *[str(a) for a in args]]
        return util.run(cmd, timeout=timeout or self.timeout, env=self._env(),
                        on_line=on_line or self.on_line)

    # ------------------------------------------------------------ 账号
    def whoami(self) -> dict:
        res = self.run(["who"], timeout=60)
        m = _WHO_RE.search(res.text)
        uid = int(m.group(1)) if m else 0
        name = ""
        nm = re.search(r"用户名:\s*(\S*)", res.text or "")
        if nm:
            name = nm.group(1)
        return {"uid": uid, "name": name, "logged_in": uid > 0, "raw": res.text.strip()}

    def logged_in(self) -> bool:
        return self.whoami().get("logged_in", False)

    def login_cookies(self, cookies: str) -> dict:
        res = self.run(["login", f"-cookies={cookies}"], timeout=90)
        ok = ("登录成功" in res.text) or self.logged_in()
        return {"ok": ok, "message": util.tail(res.text, 3)}

    def login_bduss(self, bduss: str, stoken: str = "") -> dict:
        args = ["login", f"-bduss={bduss}"]
        if stoken:
            args.append(f"-stoken={stoken}")
        res = self.run(args, timeout=90)
        ok = ("登录成功" in res.text) or self.logged_in()
        return {"ok": ok, "message": util.tail(res.text, 3)}

    def logout(self) -> dict:
        res = self.run(["logout"], timeout=60)
        return {"ok": not self.logged_in(), "message": util.tail(res.text, 3)}

    def quota(self) -> dict:
        res = self.run(["quota"], timeout=60)
        m = re.search(r"([\d.]+\s*[KMGT]?i?B?)\s*/\s*([\d.]+\s*[KMGT]?i?B?)", res.text or "")
        if not m:
            return {"ok": False, "raw": util.tail(res.text, 4)}
        return {"ok": True, "used": util.parse_size(m.group(1)),
                "total": util.parse_size(m.group(2)), "raw": res.text}

    # ------------------------------------------------------------ 目录
    def pwd(self) -> str:
        res = self.run(["pwd"], timeout=60)
        lines = [ln.strip() for ln in (res.text or "").splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    def mkdir(self, path: str) -> bool:
        with self._lock:
            res = self.run(["mkdir", path], timeout=60)
        return "创建目录成功" in (res.text or "")

    def ensure_dir(self, path: str) -> bool:
        """逐级创建网盘目录（BaiduPCS-Go 的 mkdir 不保证递归）。"""
        path = "/" + path.strip("/")
        current = ""
        for part in [p for p in path.split("/") if p]:
            current = f"{current}/{part}"
            if not self.mkdir(current):
                # 已存在也会"失败"，用 meta/ls 复核
                check = self.run(["ls", current], timeout=60)
                if check.rc != 0 or "不存在" in (check.text or "") or "错误" in (check.text or ""):
                    raise PcsError(f"无法创建网盘目录 {current}：{util.tail(check.text, 2)}")
        return True

    def cd(self, path: str) -> bool:
        with self._lock:
            res = self.run(["cd", path], timeout=60)
            if "改变工作目录" not in (res.text or ""):
                raise PcsError(f"切换目录失败 {path}：{util.tail(res.text, 3)}")
            now = self.pwd()
        if now.rstrip("/") != path.rstrip("/"):
            raise PcsError(f"工作目录未生效：期望 {path}，实际 {now}")
        return True

    def ls(self, path: str = ".", max_depth: int = 2) -> dict:
        res = self.run(["ls", path], timeout=180)
        parsed = parse_ls(res.text)
        parsed["ok"] = bool(parsed["entries"]) or "文件总数" in (res.text or "")
        if not parsed["ok"]:
            parsed["message"] = util.tail(res.text, 3)
        if max_depth > 1:
            for entry in list(parsed["entries"]):
                if entry["isdir"]:
                    sub_path = path.rstrip("/") + "/" + entry["name"]
                    sub = self.ls(sub_path, max_depth - 1)
                    for child in sub["entries"]:
                        child["name"] = f"{entry['name']}/{child['name']}"
                        parsed["entries"].append(child)
            parsed["total_size"] = sum(e["size"] for e in parsed["entries"])
            parsed["file_total"] = sum(1 for e in parsed["entries"] if not e["isdir"])
        return parsed

    def rm(self, path: str) -> bool:
        res = self.run(["rm", path], timeout=300)
        return "操作成功" in (res.text or "")

    # ------------------------------------------------------------ 转存
    def transfer(self, share_url: str, password: str, dest_dir: str,
                 collect: bool = True) -> dict:
        """把分享链接转存到 dest_dir，返回保存下来的名字。"""
        self.ensure_dir(dest_dir)
        args = ["transfer", share_url]
        if password:
            args.append(password)
        if collect:
            args.append("--collect")
        with self._lock:
            self.cd(dest_dir)
            res = self.run(args, timeout=600)
            text = res.text or ""
            ok = bool(_TRANSFER_OK.search(text))
            saved = ""
            m = _TRANSFER_OK.search(text)
            if m:
                saved = m.group(1).strip().strip("'\"")
            fail = _TRANSFER_FAIL.search(text)
            # 转存后把工作目录复位，避免影响别的命令
            try:
                self.cd("/")
            except PcsError:
                pass
        return {
            "ok": ok,
            "saved": saved,
            "message": (fail.group(1) if fail else util.tail(text, 3)),
            "raw": text,
        }

    # ------------------------------------------------------------ 下载
    def download(self, remote_paths: list[str], local_dir: str) -> dict:
        util.ensure_dir(local_dir)
        args = ["download", *remote_paths, f"--saveto={Path(local_dir)}",
                f"--mode={self.mode}", "--retry=3", "--mtime"]
        if self.threads > 0:
            args.append(f"-p={self.threads}")
        if self.max_files > 0:
            args.append(f"-l={self.max_files}")
        saved: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        def tap(line: str) -> None:
            m = _SAVED.search(line)
            if m:
                saved.append(m.group(1).strip())
                return
            m = _EXISTS.search(line)
            if m:
                skipped.append(m.group(1).strip())
                return
            if "失败" in line and "重试" not in line:
                failed.append(line.strip())

        res = self.run(args, timeout=max(self.timeout, 3600), on_line=tap)
        text = res.text or ""
        ok = bool(saved) or bool(skipped)
        if "[TIMEOUT]" in text:
            ok = False
        return {
            "ok": ok,
            "saved": saved,
            "skipped": skipped,
            "failed": failed,
            "message": util.tail(text, 4),
            "raw": text,
        }

    # ------------------------------------------------------------ 便捷
    def job_dir(self, slug: str) -> str:
        return f"{self.remote_root}/{slug}"

    def local_files(self, base: str | Path) -> list[str]:
        out: list[str] = []
        for root, _dirs, files in os.walk(base):
            for f in files:
                out.append(str(Path(root) / f))
        return out
