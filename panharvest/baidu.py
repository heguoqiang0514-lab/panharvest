"""百度网盘分享链接的深度探测（不需要登录、不消耗账号）。

两个公开探针，谁成功用谁：
1. /api/shorturlinfo?shorturl=xxx  —— 拿 shareid / uk / 文件清单
2. /s/1xxxx 页面里的 window.yunData —— 网页版同源数据

拿到 file_list 之后才能真正回答"这个资源是不是我要的"：
文件数、总大小、扩展名分布、是否压缩包、是否分卷、目录名里有没有密码提示。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import util

SURL_RE = re.compile(r"pan\.baidu\.com/s/([0-9A-Za-z_\-]+)")
PWD_IN_TEXT = re.compile(r"(?:提取码|访问码|密码|口令|pwd)\s*[:：=]?\s*([0-9a-zA-Z]{4,8})", re.I)

ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".tar.gz", ".bz2", ".xz", ".001", ".z01",
               ".cbz", ".cbr", ".part1.rar", ".part01.rar"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
BOOK_EXT = {".pdf", ".epub", ".mobi", ".azw3", ".azw", ".txt", ".djvu", ".cbz", ".cbr"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".rmvb", ".m2ts", ".mov"}


@dataclass
class Probe:
    alive: bool | None = None            # True/False/None(未知)
    errno: int | None = None
    message: str = ""
    method: str = ""
    title: str = ""
    shareid: str = ""
    uk: str = ""
    needs_pwd: bool = False
    files: list[dict] = field(default_factory=list)   # [{name,size,isdir,path}]
    total_size: int = 0
    total_count: int = 0

    # ---------------------------------------------------------- 派生指标
    def names(self, limit: int = 40) -> list[str]:
        return [f.get("name", "") for f in self.files[:limit]]

    def ext_hist(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for f in self.files:
            if f.get("isdir"):
                continue
            ext = PurePosixPath(f.get("name", "")).suffix.lower()
            hist[ext] = hist.get(ext, 0) + 1
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def image_ratio(self) -> float:
        files = [f for f in self.files if not f.get("isdir")]
        if not files:
            return 0.0
        imgs = sum(1 for f in files if PurePosixPath(f.get("name", "")).suffix.lower() in IMAGE_EXT)
        return imgs / len(files)

    def has_archives(self) -> bool:
        return any(PurePosixPath(f.get("name", "")).suffix.lower() in ARCHIVE_EXT
                   or f.get("name", "").lower().endswith(".part1.rar")
                   for f in self.files if not f.get("isdir"))

    def archive_names(self) -> list[str]:
        return [f["name"] for f in self.files
                if not f.get("isdir") and (
                    PurePosixPath(f.get("name", "")).suffix.lower() in ARCHIVE_EXT
                    or f.get("name", "").lower().endswith(".part1.rar"))]

    def volume_numbers(self) -> set[int]:
        """从文件名里抽出卷号：第01卷 / Vol.3 / 001. 之类。"""
        nums: set[int] = set()
        for f in self.files:
            name = f.get("name", "")
            for pat in (r"第\s*(\d{1,3})\s*[卷册话集]", r"[Vv]ol\.?\s*(\d{1,3})",
                        r"(?:^|[\s_\-(])(\d{2,3})(?=[\s_\-.)]|$)"):
                m = re.search(pat, name)
                if m:
                    nums.add(int(m.group(1)))
        return nums

    def to_dict(self) -> dict:
        return {
            "alive": self.alive, "errno": self.errno, "message": self.message,
            "method": self.method, "title": self.title, "needs_pwd": self.needs_pwd,
            "total_size": self.total_size, "total_count": self.total_count,
            "ext_hist": self.ext_hist(), "image_ratio": round(self.image_ratio(), 3),
            "has_archives": self.has_archives(), "names": self.names(12),
        }


def parse_share(url: str, extra_text: str = "") -> tuple[str, str]:
    """从链接（或整段分享文案）里取出 surl 与提取码。"""
    text = f"{url} {extra_text}".strip()
    m = SURL_RE.search(text) or re.search(r"/s/([0-9A-Za-z_\-]{6,})", text)
    surl = m.group(1) if m else ""
    pwd = ""
    qm = re.search(r"[?&](?:pwd|password)=([0-9a-zA-Z]{4,8})", text)
    if qm:
        pwd = qm.group(1)
    if not pwd:
        pm = PWD_IN_TEXT.search(text)
        if pm:
            pwd = pm.group(1)
    return surl, pwd


def _headers() -> dict[str, str]:
    return {
        "User-Agent": util.UA_BROWSER,
        "Referer": "https://pan.baidu.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _errno_hint(errno: int, errmsg: str) -> str:
    table = {
        0: "正常",
        2: "分享链接不存在或已被取消",
        -9: "需要提取码或提取码错误",
        -62: "分享链接已失效",
        105: "链接地址错误",
        115: "该分享已过期",
        118: "分享已取消",
    }
    return table.get(errno) or errmsg or f"errno={errno}"


def probe_shorturlinfo(surl: str, timeout: float = 12) -> Probe:
    url = (
        "https://pan.baidu.com/api/shorturlinfo?shorturl=" + urllib.parse.quote(surl) +
        "&web=1&app_id=250528&channel=chunlei&clienttype=0&root=1"
    )
    probe = Probe(method="shorturlinfo")
    try:
        data = util.http_json(url, headers=_headers(), timeout=timeout, retries=1)
    except util.HttpError as exc:
        probe.message = f"shorturlinfo 请求失败：{exc}"
        return probe
    if not isinstance(data, dict):
        probe.message = "shorturlinfo 返回格式异常"
        return probe
    errno = int(data.get("errno") or 0)
    probe.errno = errno
    probe.shareid = str(data.get("shareid") or "")
    probe.uk = str(data.get("uk") or "")
    probe.title = (data.get("title") or "").strip()
    file_list = data.get("file_list") or []
    if isinstance(file_list, list):
        for item in file_list:
            if not isinstance(item, dict):
                continue
            probe.files.append({
                "name": item.get("server_filename") or item.get("filename") or "",
                "size": int(item.get("size") or 0),
                "isdir": bool(item.get("isdir")),
                "path": item.get("path") or "",
            })
    probe.total_size = sum(f["size"] for f in probe.files)
    probe.total_count = len(probe.files)

    if errno == 0:
        probe.alive = True
        probe.message = "链接有效"
    elif errno in (2, -62, 105, 115, 118):
        probe.alive = False
        probe.message = _errno_hint(errno, data.get("errmsg") or "")
    elif errno == -9:
        probe.alive = True
        probe.needs_pwd = True
        probe.message = "需要提取码（链接仍存活）"
    else:
        probe.alive = None
        probe.message = _errno_hint(errno, data.get("errmsg") or "")
    return probe


YUNDATA_RE = re.compile(r"(?:yunData|locals)\.setData\((\{.*?\})\);", re.S)
YUNDATA_RE2 = re.compile(r"window\.yunData\s*=\s*(\{.*?\});", re.S)


def _extract_json_object(text: str, start: int) -> dict | None:
    """从 start 处的 '{' 开始做括号配对，取出完整 JSON 对象。"""
    depth, in_str, esc, i = 0, False, False, start
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def probe_share_page(surl: str, pwd: str = "", timeout: float = 15) -> Probe:
    probe = Probe(method="share_page")
    url = f"https://pan.baidu.com/s/{surl}"
    if pwd:
        url += f"?pwd={urllib.parse.quote(pwd)}"
    try:
        status, raw = util.http_request(url, headers=_headers(), timeout=timeout, retries=1)
    except util.HttpError as exc:
        probe.message = f"分享页请求失败：{exc}"
        return probe
    html = util.decode_console(raw)
    probe.errno = status
    if "输入提取码" in html or "share/init" in html:
        probe.alive = True
        probe.needs_pwd = True
        probe.message = "分享页要求输入提取码"
    data = None
    for pattern in (YUNDATA_RE, YUNDATA_RE2):
        m = pattern.search(html)
        if m:
            data = _extract_json_object(html, m.start(1))
            if data:
                break
    if not data:
        if probe.alive is None:
            probe.alive = False if ("分享的文件已经被取消" in html or "链接不存在" in html) else None
            probe.message = probe.message or "分享页未包含文件数据（可能需要提取码，或百度返回验证码页）"
        return probe

    probe.title = str(data.get("title") or data.get("shareTitle") or "").strip()
    probe.shareid = str(data.get("shareid") or "")
    probe.uk = str(data.get("share_uk") or data.get("uk") or "")
    for item in data.get("file_list") or []:
        if not isinstance(item, dict):
            continue
        probe.files.append({
            "name": item.get("server_filename") or "",
            "size": int(item.get("size") or 0),
            "isdir": bool(item.get("isdir")),
            "path": item.get("path") or "",
        })
    probe.total_size = sum(f["size"] for f in probe.files)
    probe.total_count = len(probe.files)
    if probe.files:
        probe.alive = True
        probe.message = "分享页解析成功"
    return probe


def probe(url: str, pwd: str = "", extra_text: str = "") -> Probe:
    """对一条百度分享链接做深度探测：先 API，再网页。"""
    surl, parsed_pwd = parse_share(url, extra_text)
    pwd = pwd or parsed_pwd
    if not surl:
        return Probe(alive=False, message="不是有效的百度网盘分享链接", method="parse")

    first = probe_shorturlinfo(surl)
    if first.files or (first.alive is True and not first.needs_pwd):
        return first
    second = probe_share_page(surl, pwd)
    # 合并：谁有文件清单用谁，存活判定以更明确的那个为准
    if second.files:
        return second
    if first.alive is not None:
        if not first.needs_pwd or not second.alive:
            return first
    return second if second.alive is not None else first
