"""极简本地 Web 界面：一个输入框，一条流水线。

只用标准库 http.server，没有前端构建。默认只监听 127.0.0.1。
"""
from __future__ import annotations

import html
import json
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import organize, util
from .config import Config
from .pipeline import Pipeline, PipelineError
from .state import State

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘资源收割机</title>
<style>
:root{--bg:#f7f7f8;--fg:#16181d;--mut:#6b7280;--card:#fff;--bd:#e5e7eb;--acc:#2563eb;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e5e7eb;--mut:#9aa2af;--card:#171a20;--bd:#252a33;--acc:#4f8cff;--ok:#22c55e;--warn:#f59e0b;--bad:#f87171}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 18px 64px}
h1{font-size:19px;margin:0 0 4px} .sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
textarea{width:100%;min-height:76px;resize:vertical;padding:12px;border-radius:10px;border:1px solid var(--bd);
 background:transparent;color:inherit;font:inherit}
.row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
button{font:inherit;padding:8px 16px;border-radius:10px;border:1px solid var(--bd);background:transparent;
 color:inherit;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
select{padding:7px 10px;border-radius:9px;border:1px solid var(--bd);background:transparent;color:inherit;font:inherit}
#log{font:12.5px/1.55 var(--mono);white-space:pre-wrap;word-break:break-all;max-height:340px;overflow:auto;
 background:#00000008;border-radius:10px;padding:12px;margin:0}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--bd)}
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)} .mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--bd);vertical-align:top}
td.n{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--acc)}
pre.path{font:12.5px var(--mono);background:#00000008;padding:8px 10px;border-radius:8px;overflow:auto;margin:6px 0 0}
.hidden{display:none}
</style></head><body><div class="wrap">
<h1>网盘资源收割机</h1>
<div class="sub">一句话 → 搜链接 → 校验匹配度 → 转存下载 → 解压 → 入库。当前后端：<b>%%BACKEND%%</b></div>

<div class="card">
  <textarea id="prompt" placeholder="例如：葬送的芙莉莲 漫画 第1-10卷 高清 / 三体 全集 epub / 鬼灭之刃 第一季 1080p"></textarea>
  <div class="row">
    <select id="mode">
      <option value="auto">全流程（能自动就自动）</option>
      <option value="search">只要链接和候选清单</option>
      <option value="inbox">从 inbox 取文件继续（人工转存后）</option>
    </select>
    <button class="primary" id="go">开始</button>
    <span id="hint" class="mut"></span>
  </div>
</div>

<div class="card" id="logcard">
  <div class="row" style="margin:0 0 10px">
    <b>运行日志</b><span id="status" class="tag">空闲</span>
    <span style="flex:1"></span>
    <button id="clear" style="padding:4px 10px">清空</button>
  </div>
  <pre id="log"></pre>
</div>

<div class="card hidden" id="resultcard">
  <div id="result"></div>
</div>

<div class="card">
  <div class="row" style="margin:0 0 10px"><b>书库里已有</b><span class="mut">（点一下用资源管理器打开）</span></div>
  <div id="library" class="mut">加载中…</div>
</div>

<p class="mut" style="font-size:12px">仅供个人学习与备份使用；网盘第三方客户端存在账号风控风险，建议用小号并调低并发。</p>
</div>
<script>
const $ = (s) => document.querySelector(s);
let source = null;

function addLog(line){
  const el = $('#log');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  el.textContent += (el.textContent ? '\\n' : '') + line;
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function setStatus(text, cls){ const s=$('#status'); s.textContent=text; s.className='tag '+(cls||''); }

$('#go').onclick = async () => {
  const prompt = $('#prompt').value.trim();
  if(!prompt){ $('#hint').textContent='先写一句你想要什么'; return; }
  $('#go').disabled = true; $('#hint').textContent='';
  $('#log').textContent=''; $('#resultcard').classList.add('hidden');
  setStatus('运行中…','warn');
  const res = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({prompt, mode: $('#mode').value})});
  const data = await res.json();
  if(!res.ok){ addLog('启动失败：'+(data.error||res.status)); $('#go').disabled=false; setStatus('失败','bad'); return; }
  if(source) source.close();
  source = new EventSource('/api/stream?job=' + encodeURIComponent(data.job_id));
  source.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if(msg.type === 'log') addLog(msg.line);
    else if(msg.type === 'done'){ renderResult(msg.result); source.close(); $('#go').disabled=false;
      setStatus('已完成','ok'); loadLibrary(); }
    else if(msg.type === 'error'){ addLog('❌ '+msg.message); source.close(); $('#go').disabled=false;
      setStatus('出错','bad'); }
  };
  source.onerror = () => { /* 断线后浏览器会自动重连，任务结束会主动关闭 */ };
};
$('#clear').onclick = () => { $('#log').textContent=''; };

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function renderResult(r){
  const card = $('#resultcard'); card.classList.remove('hidden');
  const cands = r.candidates||[];
  let h = '<div class="row" style="margin:0 0 12px"><b>结果</b>';
  h += r.ok ? '<span class="tag ok">已完成</span>' : '<span class="tag warn">未完成</span>';
  h += '<span class="mut">'+esc(r.message||'')+'</span></div>';
  if(r.library){
    h += '<div>入库位置：</div><pre class="path">'+esc(r.library)+'</pre>';
    h += '<div class="row"><button onclick="openPath(\\''+esc(r.library).replace(/'/g,'')+'\\')">打开目录</button>';
    if(r.report) h += '<button onclick="openPath(\\''+esc(r.report).replace(/'/g,'')+'\\')">打开报告</button>';
    h += '<span class="mut">'+ (r.files||0) +' 个文件 / '+ (r.size? (r.size/1073741824).toFixed(2)+'GB':'') +'</span></div>';
  }
  if(r.handoff) h += '<div class="row"><button class="primary" onclick="window.open(\\'/handoff?job='+esc(r.job_id)+'\\',\\'_blank\\')">打开人工转存引导页</button><span class="mut">转存后把文件放进 inbox，再点“从 inbox 继续”</span></div>';
  if(cands.length){
    h += '<h3 style="font-size:15px;margin:18px 0 8px">候选清单</h3><table><thead><tr><th>#</th><th>匹配</th><th>网盘</th><th>标题</th><th>提取码</th></tr></thead><tbody>';
    cands.forEach((c,i)=>{
      const cls = c.verdict==='accept'?'ok':(c.verdict==='review'?'warn':'mut');
      h += '<tr><td>'+(i+1)+'</td><td class="n '+cls+'">'+(c.score||0)+'</td><td>'+esc(c.cloud)+'</td>'
        + '<td><a href="'+esc(c.url)+'" target="_blank" rel="noreferrer">'+esc(c.title||c.url)+'</a>'
        + '<div class="mut" style="font-size:12px">'+esc((c.reasons||[])[0]||'')+'</div></td>'
        + '<td>'+esc(c.password||'无')+'</td></tr>';
    });
    h += '</tbody></table>';
  }
  if((r.warnings||[]).length){
    h += '<div class="mut" style="margin-top:12px">注意：'+r.warnings.map(esc).join('；')+'</div>';
  }
  $('#result').innerHTML = h;
}

async function openPath(p){
  await fetch('/api/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:p})});
}

async function loadLibrary(){
  const res = await fetch('/api/library');
  const items = await res.json();
  const box = $('#library');
  if(!items.length){ box.innerHTML = '<span class="mut">还是空的</span>'; return; }
  box.innerHTML = '<table><tbody>' + items.map(i =>
    '<tr><td><a href="#" onclick="openPath(\\''+i.path.replace(/'/g,'')+'\\');return false">'+esc(i.name)+'</a></td>'
    +'<td class="n">'+i.files+' 个</td><td class="n">'+(i.size/1048576).toFixed(0)+'MB</td>'
    +'<td class="mut">'+esc(i.mtime)+'</td></tr>').join('') + '</tbody></table>';
}
loadLibrary();
</script></body></html>
"""


class JobRunner:
    """一次只跑一个任务，把日志按 SSE 推给前端。"""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self.job_id = ""
        self.prompt = ""

    def push(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)
            self.lock.notify_all()

    def log(self, line: str) -> None:
        self.push({"type": "log", "line": line})

    def finish(self, result: dict | None = None) -> None:
        with self.lock:
            self.result = result
            self.done = True
            self.events.append({"type": "done", "result": result or {}})
            self.lock.notify_all()


class Handler(BaseHTTPRequestHandler):
    runner: JobRunner
    cfg: Config
    state: State
    server_version = "HarvestUI/1.0"

    # ---------------------------------------------------------- 工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    # ---------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            backend = "BaiduPCS-Go 全自动" if self.cfg.resolve_pcs_exe() else "人工转存 + inbox"
            page = PAGE.replace("%%BACKEND%%", html.escape(backend))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/library":
            self._json(organize.library_manifest(self.cfg.path_of("paths.library")))
            return
        if path == "/api/jobs":
            self._json(self.state.recent_jobs(20))
            return
        if path == "/api/stream":
            self._stream(query.get("job", [""])[0])
            return
        if path == "/handoff":
            job_id = (query.get("job") or [""])[0]
            for cand in [self.cfg.path_of("paths.reports")]:
                hits = list(cand.rglob("handoff.html")) if cand.is_dir() else []
                for hit in hits:
                    if job_id and job_id[-4:] in hit.parent.name:
                        self._send(200, hit.read_bytes(), "text/html; charset=utf-8")
                        return
            self._send(404, b"no handoff", "text/plain; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/run":
            data = self._body()
            prompt = (data.get("prompt") or "").strip()
            mode = (data.get("mode") or "auto").strip()
            if not prompt:
                self._json({"error": "prompt 不能为空"}, 400)
                return
            if self.runner and not self.runner.done:
                self._json({"error": "已有任务在跑，等它结束"}, 409)
                return
            runner = JobRunner()
            runner.prompt = prompt
            type(self).runner = runner
            threading.Thread(target=self._execute, args=(runner, prompt, mode), daemon=True).start()
            # 等 job_id 就绪（最多 3s）
            for _ in range(60):
                if runner.job_id:
                    break
                time.sleep(0.05)
            self._json({"job_id": runner.job_id or "pending"})
            return
        if path == "/api/open":
            target = (self._body().get("path") or "").strip()
            allowed = [self.cfg.path_of("paths.library"), self.cfg.path_of("paths.reports"),
                       self.cfg.path_of("paths.downloads")]
            try:
                resolved = Path(target).resolve()
                if any(str(resolved).startswith(str(p.resolve())) for p in allowed):
                    util.open_path(resolved)
                    self._json({"ok": True})
                else:
                    self._json({"error": "只允许打开书库/报告/下载目录"}, 403)
            except OSError as exc:
                self._json({"error": str(exc)}, 400)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    # ---------------------------------------------------------- 任务执行
    def _execute(self, runner: JobRunner, prompt: str, mode: str) -> None:
        sink = None
        try:
            def sink(line: str) -> None:  # type: ignore[misc]
                runner.log(line)

            util.add_log_sink(sink)
            pipe = Pipeline(self.cfg, self.state,
                            on_event=lambda kind, payload: runner.log(
                                f"[{payload.get('stage', kind)}] "
                                f"{'OK' if payload.get('ok') else 'FAIL'} {payload.get('message', '')}"))
            runner.job_id = f"pending-{int(time.time())}"
            result = pipe.run(prompt, mode=mode, open_when_done=False)
            runner.job_id = result.get("job_id") or runner.job_id
            runner.finish(result)
        except PipelineError as exc:
            runner.log(f"❌ {exc}")
            runner.finish({"ok": False, "message": str(exc)})
        except Exception as exc:  # 任何异常都要在页面上看得见
            util.log(f"任务异常：{exc}", "ERROR")
            import traceback

            runner.log(traceback.format_exc()[-2000:])
            runner.finish({"ok": False, "message": f"异常：{exc}"})
        finally:
            if sink:
                util.remove_log_sink(sink)

    def _stream(self, job_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        runner = type(self).runner
        if runner is None:
            self._sse({"type": "error", "message": "还没有任务"})
            return
        index = 0
        last_ping = time.time()
        while True:
            with runner.lock:
                pending = runner.events[index:]
                index = len(runner.events)
                finished = runner.done
            for event in pending:
                self._sse(event)
            if finished:
                return
            if time.time() - last_ping > 15:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                last_ping = time.time()
            with runner.lock:
                if not runner.done and len(runner.events) <= index:
                    runner.lock.wait(timeout=1.0)

    def _sse(self, event: dict) -> None:
        try:
            self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise SystemExit(0)


def serve(cfg: Config, state: State, port: int = 0, open_browser: bool = True) -> int:
    Handler.cfg = cfg
    Handler.state = state
    Handler.runner = None  # type: ignore[assignment]
    want = port or int(cfg.get("ui.port", 8765))
    httpd = None
    for candidate in range(want, want + 12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        util.log(f"端口 {want} 起不来，换一个：harvest serve --port 9000", "ERROR")
        return 2
    host, real_port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{real_port}/"
    util.log(f"界面已启动：{url}")
    util.log("按 Ctrl+C 结束")
    if open_browser and cfg.get("ui.open_browser", True):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        util.log("界面已关闭")
    finally:
        httpd.server_close()
    return 0
