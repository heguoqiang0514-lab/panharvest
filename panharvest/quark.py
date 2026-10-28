"""夸克网盘分享探测：不需要登录就能读到分享里的文件清单。

三步（都是夸克网页版自己用的公开接口）：
    token  → 用分享 id 换 stoken（带提取码就传 passcode）
    detail → 用 stoken 列目录，拿到文件名/大小/是否目录
    （下载需要登录态，本模块只做"这链接是不是我想的那个东西"的校验）

注意：stoken 里可能含 "+" "/" "="，放进 query 必须 urlencode，否则 400。
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from . import util

API = "https://drive-pc.quark.cn/1/clouddrive"
BASE_Q = "?pr=ucpro&fr=pc&uc_param_str="
PWD_ID_RE = re.compile(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)")
PASSCODE_RE = re.compile(r"(?:提取码|密码|pwd)\s*[:：=]?\s*([0-9a-zA-Z]{4})", re.I)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": util.UA_BROWSER,
        "Referer": "https://pan.quark.cn/",
        "Origin": "https://pan.quark.cn",
        "Content-Type": "application/json",
    }


@dataclass
class QuarkProbe:
    ok: bool = False
    message: str = ""
    title: str = ""
    stoken: str = ""
    files: list[dict] = field(default_factory=list)   # {name,size,isdir}
    truncated: bool = False

    @property
    def total_size(self) -> int:
        return sum(int(f.get("size") or 0) for f in self.files if not f.get("isdir"))

    @property
    def file_count(self) -> int:
        return sum(1 for f in self.files if not f.get("isdir"))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "message": self.message, "title": self.title,
            "file_count": self.file_count, "total_size": self.total_size,
            "truncated": self.truncated,
            "names": [f["name"] for f in self.files[:10]],
        }


def parse_share(url: str, extra_text: str = "") -> tuple[str, str]:
    text = f"{url} {extra_text}"
    m = PWD_ID_RE.search(text)
    pwd_id = m.group(1) if m else ""
    pwd = ""
    qm = re.search(r"[?&](?:pwd|password)=([0-9a-zA-Z]{4})", text)
    if qm:
        pwd = qm.group(1)
    else:
        pm = PASSCODE_RE.search(text)
        if pm:
            pwd = pm.group(1)
    return pwd_id, pwd


def get_token(pwd_id: str, passcode: str = "", timeout: float = 20) -> tuple[str, str, str]:
    """返回 (stoken, 标题, 错误信息)。"""
    try:
        data = util.http_json(f"{API}/share/sharepage/token{BASE_Q}", "POST",
                              {"pwd_id": pwd_id, "passcode": passcode or ""},
                              _headers(), timeout, retries=1, json_body=True)
    except util.HttpError as exc:
        return "", "", f"token 请求失败：{exc}"
    if data.get("code") not in (0, None):
        return "", "", f"分享不可用：{data.get('message')}"
    body = data.get("data") or {}
    return str(body.get("stoken") or ""), "", ""


def list_dir(pwd_id: str, stoken: str, pdir_fid: str = "0", page_size: int = 100,
             timeout: float = 25) -> tuple[list[dict], bool, str]:
    """列一层目录，返回 (文件列表, 是否被截断, 错误)。"""
    enc = urllib.parse.quote(stoken, safe="")
    url = (f"{API}/share/sharepage/detail{BASE_Q}&pwd_id={pwd_id}&stoken={enc}"
           f"&pdir_fid={pdir_fid}&force=0&_page=1&_size={page_size}"
           f"&_fetch_banner=0&_fetch_share=1&_fetch_total=1&_sort=file_type:asc,updated_at:desc")
    try:
        data = util.http_json(url, headers=_headers(), timeout=timeout, retries=1)
    except util.HttpError as exc:
        return [], False, f"detail 请求失败：{exc}"
    if data.get("code") not in (0, None):
        return [], False, f"读取目录失败：{data.get('message')}"
    body = data.get("data") or {}
    items = []
    for row in body.get("list") or []:
        items.append({
            "name": row.get("file_name") or "",
            "size": int(row.get("size") or 0),
            "isdir": bool(row.get("dir")),
            "fid": row.get("fid") or "",
            "share_fid_token": row.get("share_fid_token") or "",
        })
    total = int((body.get("total") or {}).get("total") or 0) if isinstance(body.get("total"), dict) else 0
    truncated = bool(total and total > len(items))
    return items, truncated, ""


def list_dir_with_tokens(pwd_id: str, stoken: str, pdir_fid: str = "0",
                         page_size: int = 100) -> tuple[list[dict], bool, str]:
    """和 list_dir 一样，但保证返回的条目带 share_fid_token（转存要用）。"""
    return list_dir(pwd_id, stoken, pdir_fid, page_size)


def probe(url: str, passcode: str = "", extra_text: str = "",
          max_depth: int = 2, max_entries: int = 400) -> QuarkProbe:
    """探测一个夸克分享：拿标题 + 文件清单（最多两层）。"""
    result = QuarkProbe()
    pwd_id, parsed_pwd = parse_share(url, extra_text)
    if not pwd_id:
        result.message = "不是有效的夸克分享链接"
        return result
    stoken, _title, err = get_token(pwd_id, passcode or parsed_pwd)
    if not stoken:
        result.message = err or "拿不到 stoken"
        return result
    result.stoken = stoken

    queue: list[tuple[str, int, str]] = [("0", 0, "")]
    while queue and len(result.files) < max_entries:
        fid, depth, prefix = queue.pop(0)
        items, truncated, err = list_dir(pwd_id, stoken, fid)
        if err:
            result.message = err if not result.files else result.message
            break
        result.truncated = result.truncated or truncated
        for item in items:
            name = f"{prefix}{item['name']}"
            result.files.append({"name": name, "size": item["size"], "isdir": item["isdir"]})
            if item["isdir"] and depth < max_depth:
                queue.append((item["fid"], depth + 1, f"{name}/"))
        if len(result.files) >= max_entries:
            result.truncated = True
            break
    result.ok = bool(result.files)
    if not result.ok and not result.message:
        result.message = "分享里没有文件（可能已失效）"
    if result.ok and not result.title:
        tops = [f["name"] for f in result.files[:1]]
        result.title = tops[0] if tops else ""
    return result
