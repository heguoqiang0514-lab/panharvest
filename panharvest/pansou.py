"""PanSou 客户端：搜索网盘链接 + 官方链接存活检测。

PanSou: https://github.com/fish2018/pansou
- POST /api/search      {"kw": "...", "res": "all", "src": "all", "cloud_types": [...]}
- POST /api/check/links {"items": [{"disk_type": "baidu", "url": "...", "password": "..."}]}
- GET  /api/health
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Iterable

from . import util
from .util import HttpError

# PanSou 支持的网盘标识
CLOUD_LABEL = {
    "baidu": "百度网盘",
    "quark": "夸克网盘",
    "aliyun": "阿里云盘",
    "115": "115网盘",
    "uc": "UC网盘",
    "xunlei": "迅雷网盘",
    "123": "123网盘",
    "tianyi": "天翼云盘",
    "mobile": "移动云盘",
    "pikpak": "PikPak",
    "guangya": "光鸭云盘",
    "magnet": "磁力链接",
    "ed2k": "电驴链接",
    "others": "其他",
}
CHECKABLE = {"baidu", "aliyun", "quark", "tianyi", "uc", "mobile", "115", "xunlei", "123"}


def normalize_url(url: str) -> tuple[str, str]:
    """把链接里自带的 pwd 拆出来。返回 (干净链接, 提取码)。"""
    url = (url or "").strip()
    if not url:
        return "", ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url, ""
    if not parsed.scheme:
        parsed = urllib.parse.urlsplit("https://" + url)
    qs = urllib.parse.parse_qs(parsed.query)
    pwd = ""
    for key in ("pwd", "password", "passcode"):
        if qs.get(key):
            pwd = qs[key][0].strip()
            break
    query = {k: v for k, v in qs.items() if k not in ("pwd", "password", "passcode")}
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query, doseq=True), "")
    )
    return clean, pwd


def url_key(url: str) -> str:
    """用于去重的稳定键。"""
    clean, _ = normalize_url(url)
    return clean.rstrip("/").lower()


def unwrap(data: Any) -> dict:
    """兼容两种返回外壳。

    官方后端直接返回 {"total":..,"results":[..],"merged_by_type":{..}}；
    有些版本（含 pansou-web / 部分公开实例）会在外面套一层
    {"code":0,"message":"success","data":{...}}。剥掉再解析。
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict) and any(
        key in inner for key in ("results", "merged_by_type", "total", "list")
    ):
        return inner
    return data


class PanSou:
    def __init__(self, base: str, token: str = "", timeout: float = 90, refresh: bool = False):
        self.base = (base or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.refresh = refresh

    # ------------------------------------------------------------ 基础
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _post(self, path: str, payload: dict) -> Any:
        if not self.base:
            raise HttpError("未配置 PanSou 地址（config.json -> pansou.base）")
        return util.http_json(
            self.base + path, "POST", payload, self._headers(), self.timeout, retries=1,
            json_body=True,
        )

    def health(self) -> dict:
        try:
            status, raw = util.http_request(self.base + "/api/health", timeout=10, retries=0)
            data = json.loads(util.decode_console(raw)) if raw else {}
            data.setdefault("status", "ok" if status == 200 else f"http{status}")
            return data
        except (HttpError, json.JSONDecodeError) as exc:
            return {"status": "down", "error": str(exc)}

    def reachable(self) -> bool:
        return self.health().get("status") == "ok"

    # ------------------------------------------------------------ 搜索
    def search(self, kw: str, clouds: Iterable[str] | None = None,
               refresh: bool | None = None) -> dict:
        payload: dict[str, Any] = {
            "kw": kw,
            "res": "all",
            "src": "all",
            "refresh": self.refresh if refresh is None else bool(refresh),
        }
        cloud_list = [c for c in (clouds or []) if c in CLOUD_LABEL]
        if cloud_list:
            payload["cloud_types"] = cloud_list
        data = self._post("/api/search", payload)
        if isinstance(data, dict) and data.get("data") is not None:
            code = data.get("code")
            if code not in (None, 0, 200) and not isinstance(data.get("data"), dict):
                msg = data.get("message") or data.get("error")
                raise HttpError(f"PanSou 返回错误：{msg}")
            data = unwrap(data)
        if isinstance(data, dict) and (data.get("code") or data.get("error")):
            msg = data.get("message") or data.get("error")
            if not isinstance(data.get("results"), list):
                raise HttpError(f"PanSou 返回错误：{msg}")
        return data if isinstance(data, dict) else {}

    def collect(self, keywords: list[str], clouds: Iterable[str] | None = None,
                max_candidates: int = 20, wait_rounds: int = 3,
                wait_seconds: float = 6.0) -> tuple[list[dict], list[str]]:
        """逐个关键词搜索并合并去重。返回 (候选列表, 每个关键词的命中数说明)。

        PanSou 默认是"异步插件"模式：首次请求可能只返回 4 秒内就绪的部分，
        其余结果在后台继续跑并写入缓存。所以第一次空手而归时要隔几秒再问两次，
        第二次通常直接命中服务端缓存。
        """
        wanted = {c for c in (clouds or []) if c in CLOUD_LABEL} or None
        merged: dict[str, dict] = {}
        notes: list[str] = []

        for index, kw in enumerate(keywords):
            found: list[dict] = []
            for attempt in range(max(1, wait_rounds)):
                try:
                    data = self.search(kw, wanted, refresh=False)
                except HttpError as exc:
                    notes.append(f"关键词「{kw}」搜索失败：{exc}")
                    util.log(f"搜索失败「{kw}」：{exc}", "WARN")
                    break
                found = self._extract(data, kw, wanted)
                if found:
                    if attempt:
                        util.log(f"「{kw}」第 {attempt + 1} 次请求才拿到结果（PanSou 异步）")
                    break
                if attempt < wait_rounds - 1:
                    util.log(f"「{kw}」暂无结果，{wait_seconds:.0f}s 后再问一次"
                             f"（{attempt + 1}/{wait_rounds}）")
                    time.sleep(wait_seconds)

            fresh = 0
            for cand in found:
                key = url_key(cand["url"])
                if key in merged:
                    merged[key]["hits"] += 1
                    if not merged[key].get("password") and cand.get("password"):
                        merged[key]["password"] = cand["password"]
                    continue
                merged[key] = cand
                fresh += 1
            notes.append(f"关键词「{kw}」抓到 {len(found)} 条，新增 {fresh} 条")
            util.log(f"搜索「{kw}」-> {len(found)} 条（累计 {len(merged)}）")
            if len(merged) >= max_candidates * 3:
                break
            # 第一个关键词就够多时，不再浪费请求
            if index == 0 and len(merged) >= max_candidates:
                break

        cands = sorted(merged.values(), key=lambda c: (-c.get("hits", 0), c.get("published", "")),
                       reverse=False)
        return cands[: max(max_candidates, 1)], notes

    @staticmethod
    def _extract(data: dict, kw: str, wanted: set[str] | None) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()

        # 1) merged_by_type：已经按网盘归类去重，优先用
        merged_by_type = data.get("merged_by_type") or {}
        if isinstance(merged_by_type, dict):
            for cloud, items in merged_by_type.items():
                if wanted and cloud not in wanted:
                    continue
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url") or ""
                    if not url:
                        continue
                    clean, pwd = normalize_url(url)
                    key = url_key(clean)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "url": clean,
                        "cloud": cloud,
                        "password": (item.get("password") or pwd or "").strip(),
                        "title": (item.get("note") or "").strip(),
                        "note": (item.get("note") or "").strip(),
                        "source": (item.get("source") or "").strip(),
                        "published": (item.get("datetime") or "").strip(),
                        "images": item.get("images") or [],
                        "hits": 1,
                        "keyword": kw,
                    })

        # 2) results：补充 merged_by_type 里没有的（有些插件只出现在 results）
        for res in data.get("results") or []:
            if not isinstance(res, dict):
                continue
            title = (res.get("title") or "").strip()
            content = (res.get("content") or "").strip()
            published = (res.get("datetime") or "").strip()
            channel = (res.get("channel") or "").strip()
            for link in res.get("links") or []:
                if not isinstance(link, dict):
                    continue
                cloud = link.get("type") or ""
                if wanted and cloud not in wanted:
                    continue
                url = link.get("url") or ""
                if not url:
                    continue
                clean, pwd = normalize_url(url)
                key = url_key(clean)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "url": clean,
                    "cloud": cloud,
                    "password": (link.get("password") or pwd or "").strip(),
                    "title": (link.get("work_title") or title or content[:60]).strip(),
                    "note": content[:200],
                    "source": f"tg:{channel}" if channel else "unknown",
                    "published": published,
                    "images": res.get("images") or [],
                    "hits": 1,
                    "keyword": kw,
                })
        return out

    # ------------------------------------------------------------ 存活检测
    def check_links(self, cands: list[dict]) -> dict[str, dict]:
        """批量检测链接有效性。返回 {url_key: {state, summary}}。"""
        items, by_key = [], {}
        for cand in cands:
            cloud = cand.get("cloud", "")
            if cloud not in CHECKABLE:
                continue
            item = {"disk_type": cloud, "url": cand["url"]}
            if cand.get("password"):
                item["password"] = cand["password"]
            items.append(item)
            by_key[url_key(cand["url"])] = True
        if not items:
            return {}
        result: dict[str, dict] = {}
        try:
            data = self._post("/api/check/links", {"items": items})
        except HttpError as exc:
            util.log(f"链接检测接口不可用（不阻塞流程）：{exc}", "WARN")
            return {}
        if isinstance(data, dict):
            data = unwrap(data)
        for row in (data.get("results") or []) if isinstance(data, dict) else []:
            url = (row.get("url") or "").strip()
            normalized = (row.get("normalized_url") or url).strip()
            key = url_key(url) or url_key(normalized)
            if not key:
                continue
            result[key] = {
                "state": row.get("state", "uncertain"),
                "summary": row.get("summary", ""),
                "checked_at": row.get("checked_at"),
                "cache": bool(row.get("cache_hit")),
            }
        util.log(f"链接检测：送出 {len(items)} 条，返回 {len(result)} 条")
        return result


def detect_cloud_from_url(url: str) -> str:
    low = (url or "").lower()
    rules = [
        ("pan.baidu.com", "baidu"), ("yun.baidu.com", "baidu"), ("eyun.baidu.com", "baidu"),
        ("pan.quark.cn", "quark"), ("aliyundrive.com", "aliyun"), ("alipan.com", "aliyun"),
        ("115.com", "115"), ("115cdn.com", "115"), ("anxia.com", "115"),
        ("drive.uc.cn", "uc"), ("pan.xunlei.com", "xunlei"),
        ("pan.123pan.cn", "123"), ("123pan.com", "123"), ("123684.com", "123"),
        ("cloud.189.cn", "tianyi"), ("caiyun.139.com", "mobile"), ("mypikpak.com", "pikpak"),
        ("guangyapan.com", "guangya"),
    ]
    for token, cloud in rules:
        if token in low:
            return cloud
    if low.startswith("magnet:"):
        return "magnet"
    if low.startswith("ed2k:"):
        return "ed2k"
    return "others"


def extract_password_from_text(text: str) -> str:
    """从标题/正文里抠提取码：'提取码: abcd' / '密码 1234'. """
    if not text:
        return ""
    m = re.search(r"(?:提取码|访问码|密码|口令|pwd)\s*[:：=]?\s*([0-9a-zA-Z]{4,8})", text, re.I)
    return m.group(1) if m else ""
