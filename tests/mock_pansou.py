"""假的 PanSou 服务：把 /api/search 和 /api/check/links 的响应固定下来。

只用于本地端到端测试，不参与正式运行。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LINKS = {
    "葬送的芙莉莲": [
        {
            "message_id": "1", "unique_id": "tg-1", "channel": "tgsearchers3",
            "datetime": "2026-06-01T10:00:00Z",
            "title": "葬送的芙莉莲 漫画 1-10卷 全彩汉化",
            "content": "葬送的芙莉莲 漫画 1-10卷 全彩汉化 高清 附赠设定集",
            "links": [
                {"type": "baidu", "url": "https://pan.baidu.com/s/1goodshare",
                 "password": "abcd", "work_title": "葬送的芙莉莲 漫画 1-10卷 全彩汉化"},
                {"type": "baidu", "url": "https://pan.baidu.com/s/1deadshare",
                 "password": "efgh", "work_title": "葬送的芙莉莲 剧场版 预告片"},
                {"type": "quark", "url": "https://pan.quark.cn/s/quark123",
                 "password": "", "work_title": "葬送的芙莉莲 漫画 全集"},
            ],
            "images": [],
        }
    ]
}

CHECK = {
    "1goodshare": ("ok", "链接有效"),
    "deadshare": ("bad", "链接失效"),
}


def _merged(payload: dict) -> dict:
    by_type: dict[str, list] = {}
    for res in payload.get("results") or []:
        for link in res.get("links") or []:
            item = {
                "url": link["url"],
                "password": link.get("password", ""),
                "note": link.get("work_title") or res["title"],
                "datetime": res.get("datetime", ""),
                "source": f"tg:{res.get('channel', '')}",
                "images": res.get("images") or [],
            }
            by_type.setdefault(link["type"], []).append(item)
    out = dict(payload)
    out["merged_by_type"] = by_type
    out["total"] = sum(len(v) for v in by_type.values())
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        return

    def _send(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            self._send({"status": "ok", "auth_enabled": False, "plugins_enabled": True,
                        "plugin_count": 3, "plugins": ["mock"], "channels_count": 2,
                        "channels": ["tgsearchers3", "mock"]})
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = {}
        if path == "/api/search":
            kw = data.get("kw", "")
            results = []
            for title, items in LINKS.items():
                if title in kw or kw in title:
                    results = items
                    break
            self._send(_merged({"results": results}))
            return
        if path == "/api/check/links":
            out = []
            for item in data.get("items") or []:
                url = item.get("url", "")
                key = next((k for k in CHECK if k in url), "")
                state, summary = CHECK.get(key, ("uncertain", "未确认"))
                out.append({"disk_type": item.get("disk_type"), "url": url,
                            "normalized_url": url, "state": state, "cache_hit": False,
                            "summary": summary})
            self._send({"results": out})
            return
        self._send({"error": "not found"}, 404)


class MockPanSou:
    def __init__(self, port: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockPanSou":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # 手动起一个来玩
    with MockPanSou(8899) as mock:
        print("mock pansou at", mock.base)
        threading.Event().wait()
