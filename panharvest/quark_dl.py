"""夸克网盘下载（需要登录 Cookie）：分享 → 转存到自己网盘 → 取直链 → 下载。

为什么需要 Cookie：夸克只允许登录用户下载分享文件，匿名只能读目录（见 quark.py）。

流程：
    save   把分享里的 file 转存到自己网盘（默认根目录）
    task   轮询转存任务，拿到新文件的 fid
    link   POST /file/download 换出带签名的直链
    get    带 Cookie + Referer 下载（支持断点）

Cookie 从哪来：浏览器打开 pan.quark.cn 并登录 → F12 → Network → 任一请求的
Cookie 里找 `__puus=...`。把整段 Cookie 粘给 `harvest login --quark "..."` 即可。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import util

API = "https://drive-pc.quark.cn/1/clouddrive"
Q = "?pr=ucpro&fr=pc&uc_param_str="
# 必须用夸克 PC 客户端的 UA：网页/手机 UA 会被 code 23018「download file size limit」拦大文件
UA_QUARK_CLIENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
    "Safari/537.36 Channel/pckk_other_ch"
)


class QuarkError(RuntimeError):
    pass


@dataclass
class Saved:
    fids: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    task_id: str = ""
    raw: dict = field(default_factory=dict)


class QuarkSession:
    """一个带 Cookie 的夸克会话。"""

    def __init__(self, cookie: str, timeout: float = 60, save_dir_fid: str = "0"):
        self.cookie = (cookie or "").strip()
        if not self.cookie:
            raise QuarkError("没有夸克 Cookie：先执行 harvest login --quark \"<cookie>\"")
        self.timeout = timeout
        self.save_dir_fid = save_dir_fid

    # ------------------------------------------------------------ 基础
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA_QUARK_CLIENT,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Content-Type": "application/json",
            "Cookie": self.cookie,
        }

    def _post(self, path: str, payload: dict) -> dict:
        try:
            data = util.http_json(f"{API}{path}{Q}", "POST", payload, self._headers(),
                                  self.timeout, retries=2, json_body=True)
        except util.HttpError as exc:
            raise QuarkError(f"{path} 请求失败（HTTP {exc.status or '网络'}）：{exc}") from exc
        return self._check(data, path)

    def _get(self, path: str, query: str = "") -> dict:
        try:
            data = util.http_json(f"{API}{path}{Q}{query}", headers=self._headers(),
                                  timeout=self.timeout, retries=2)
        except util.HttpError as exc:
            raise QuarkError(f"{path} 请求失败（HTTP {exc.status or '网络'}）：{exc}") from exc
        return self._check(data, path)

    @staticmethod
    def _check(data: dict, where: str) -> dict:
        if not isinstance(data, dict):
            raise QuarkError(f"{where}: 返回格式异常")
        code = data.get("code")
        if code not in (0, None):
            msg = data.get("message") or ""
            if code == 31001 or "登录" in str(msg):
                raise QuarkError(f"Cookie 失效或未登录（{msg}）")
            raise QuarkError(f"{where}: {code} {msg}")
        return data

    # ------------------------------------------------------------ 账号
    def whoami(self) -> dict:
        data = self._get("/member")
        body = data.get("data") or {}
        return {"nickname": body.get("nickname") or "", "member_type": body.get("member_type")}

    # ------------------------------------------------------------ 转存
    def ensure_folder(self, name: str) -> str:
        """在我的网盘根目录下建一个文件夹，返回它的 fid（已存在就复用）。"""
        try:
            items = self.list_own("0")
            for item in items:
                if item["isdir"] and item["name"] == name:
                    return item["fid"]
        except QuarkError:
            pass
        data = self._post("/file", {"pdir_fid": "0", "file_name": name,
                                    "dir_path": "", "dir_init_lock": False})
        fid = str(((data.get("data") or {}).get("fid")) or "")
        if not fid:
            raise QuarkError("建文件夹没有返回 fid")
        return fid

    def save_share(self, pwd_id: str, stoken: str, fid_list: list[str],
                   fid_token_list: list[str]) -> Saved:
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": self.save_dir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        data = self._post("/share/sharepage/save", payload)
        body = data.get("data") or {}
        task_id = str(body.get("task_id") or "")
        saved = Saved(task_id=task_id, raw=body)
        if not task_id:
            raise QuarkError(f"转存没有返回 task_id：{json.dumps(body, ensure_ascii=False)[:200]}")
        return self._wait_task(saved)

    def _wait_task(self, saved: Saved, timeout: float = 60) -> Saved:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self._get("/task", f"&task_id={saved.task_id}&retry_index=0")
            body = data.get("data") or {}
            status = body.get("status")
            if status == 2:
                save_as = body.get("save_as") or {}
                fids = save_as.get("save_as_top_fids") or []
                saved.fids = [str(f) for f in fids]
                return saved
            if status in (3, 4):  # 失败
                raise QuarkError(f"转存任务失败：{body.get('message') or status}")
            time.sleep(1.0)
        raise QuarkError("转存任务超时")

    # ------------------------------------------------------------ 直链
    def download_url(self, fid: str) -> str:
        data = self._post("/file/download", {"fids": [fid]})
        rows = data.get("data") or []
        if not rows:
            raise QuarkError("没有拿到下载链接")
        url = rows[0].get("download_url") or ""
        if not url:
            raise QuarkError(f"下载链接为空：{json.dumps(rows[0], ensure_ascii=False)[:200]}")
        return url

    # ------------------------------------------------------------ 列自己的目录
    def list_own(self, pdir_fid: str = "0", page_size: int = 100) -> list[dict]:
        data = self._get("/file/sort", f"&pdir_fid={pdir_fid}&_page=1&_size={page_size}"
                                       "&_fetch_total=1&_sort=file_type:asc,updated_at:desc")
        body = data.get("data") or {}
        return [{"name": r.get("file_name") or "", "size": int(r.get("size") or 0),
                 "isdir": bool(r.get("dir")), "fid": r.get("fid") or ""}
                for r in body.get("list") or []]

    # ------------------------------------------------------------ 下载
    def fetch(self, url: str, dest: Path, on_progress=None) -> Path:
        util.ensure_dir(dest.parent)
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {"User-Agent": UA_QUARK_CLIENT, "Referer": "https://pan.quark.cn/",
                   "Cookie": self.cookie, "Accept": "*/*"}
        import urllib.request

        req = urllib.request.Request(url, headers=headers)
        got = 0
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
        tmp.replace(dest)
        return dest


def download_share(
    cookie: str,
    share_url: str,
    out_dir: str | Path,
    passcode: str = "",
    extra_text: str = "",
    max_files: int = 40,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    max_bytes: int = 0,
    max_depth: int = 6,
    chunk: int = 30,
    on_log=None,
) -> dict:
    """把整个夸克分享下载到本地目录。

    include/exclude 按「分享内完整路径」的子串过滤，例如 include=("02.数学",)
    exclude=("数二","数三")；max_bytes 是体积上限（字节，0 表示不限）。
    """
    from . import quark

    log = on_log or (lambda m: util.log(m))
    out_dir = Path(out_dir)
    util.ensure_dir(out_dir)

    pwd_id, parsed_pwd = quark.parse_share(share_url, extra_text)
    if not pwd_id:
        raise QuarkError("不是有效的夸克分享链接")
    probe = quark.probe(share_url, passcode or parsed_pwd, extra_text, max_depth=2)
    if not probe.ok:
        raise QuarkError(f"分享不可用：{probe.message}")
    log(f"分享：{probe.title or '(未知)'}｜探测到 {probe.file_count} 个文件 / "
        f"{util.human_size(probe.total_size)}")

    stoken, _t, err = quark.get_token(pwd_id, passcode or parsed_pwd)
    if not stoken:
        raise QuarkError(f"拿不到分享凭证：{err}")

    # ---- 收集文件：include 里的路径按“逐级下降”走，不再全盘遍历
    terms = [[c for c in t.strip("/").split("/") if c] for t in include] if include else []
    all_files: list[dict] = []
    # 队列元素：(目录 fid, 当前路径, 深度, 每个 include 词已匹配到第几段)
    queue: list[tuple[str, str, int, tuple[int, ...]]] = [
        ("0", "", 0, tuple(0 for _ in terms))]
    seen_dirs = 0

    def next_state(name: str, state: tuple[int, ...]) -> tuple[int, ...]:
        out = []
        for comps, matched in zip(terms, state):
            if matched < len(comps) and comps[matched] == name:
                out.append(matched + 1)
            else:
                out.append(matched)
        return tuple(out)

    def complete(state: tuple[int, ...]) -> bool:
        return bool(terms) and all(
            m >= len(c) for c, m in zip(terms, state))

    while queue:
        fid, prefix, depth, state = queue.pop(0)
        rows, _trunc, err = quark.list_dir_with_tokens(pwd_id, stoken, fid)
        if err:
            raise QuarkError(f"列目录失败：{err}")
        seen_dirs += 1
        for row in rows:
            path = f"{prefix}{row['name']}"
            if row.get("isdir"):
                if depth >= max_depth or _hit(exclude, path):
                    continue
                nxt = next_state(row["name"], state)
                # 只有“还能匹配上”或“已经整段命中”的目录才继续往下走
                if terms and not complete(nxt) and not any(
                        m < len(c) for c, m in zip(terms, nxt)):
                    continue
                queue.append((row["fid"], path + "/", depth + 1, nxt))
            else:
                if terms and not complete(state) and not _hit(include, path):
                    continue
                row = dict(row)
                row["path"] = path
                all_files.append(row)
    log(f"遍历 {seen_dirs} 个目录，命中 {len(all_files)} 个文件")

    picked: list[dict] = []
    total = 0
    for row in all_files:
        path = row["path"]
        if include and not _hit(include, path):
            continue
        if exclude and _hit(exclude, path):
            continue
        if max_bytes and total + row["size"] > max_bytes:
            continue
        picked.append(row)
        total += row["size"]
        if len(picked) >= max_files:
            break
    if not picked:
        raise QuarkError("按当前筛选条件没有命中任何文件")
    log(f"选中 {len(picked)} 个文件 / {util.human_size(total)}")

    session = QuarkSession(cookie)
    try:
        session.save_dir_fid = session.ensure_folder("资源收割")
        log(f"转存目标：/资源收割（fid={session.save_dir_fid}）")
    except QuarkError as exc:
        log(f"建文件夹失败，改存根目录：{exc}")

    results: list[dict] = []
    done_bytes = 0
    for start in range(0, len(picked), chunk):
        batch = picked[start:start + chunk]
        log(f"转存第 {start // chunk + 1} 批（{len(batch)} 个文件）")
        saved = session.save_share(
            pwd_id, stoken,
            [f["fid"] for f in batch],
            [f.get("share_fid_token", "") for f in batch],
        )
        if len(saved.fids) != len(batch):
            log(f"注意：提交 {len(batch)} 个，返回 {len(saved.fids)} 个 fid")
        for i, fid in enumerate(saved.fids):
            item = batch[i] if i < len(batch) else {"path": f"file-{i}.bin", "size": 0}
            rel = Path(item["path"])
            dest = out_dir / Path(*[util.safe_filename(p, 90) for p in rel.parts])
            if dest.exists() and dest.stat().st_size == item.get("size", -1):
                log(f"已存在，跳过：{dest.name}")
                results.append({"path": str(dest), "skipped": True})
                continue
            url = session.download_url(fid)

            def progress(got: int, total_bytes: int, _n=dest.name) -> None:
                if total_bytes and got >= total_bytes or (total_bytes and got % (1 << 22) < (1 << 18)):
                    log(f"  {_n}: {got * 100 // max(total_bytes, 1)}% "
                        f"({util.human_size(got)}/{util.human_size(total_bytes)})")

            session.fetch(url, dest, on_progress=progress)
            done_bytes += dest.stat().st_size
            results.append({"path": str(dest), "skipped": False,
                            "size": dest.stat().st_size})
    log(f"下载完成：{len(results)} 个文件，{util.human_size(done_bytes)}")
    return {"ok": True, "files": results, "title": probe.title, "dir": str(out_dir),
            "bytes": done_bytes}


def _hit(words: tuple[str, ...], path: str) -> bool:
    return bool(words) and any(w and w in path for w in words)
