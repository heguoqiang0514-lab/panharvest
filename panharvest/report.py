"""报告输出：markdown 复盘 + 候选表 csv + 人工转存引导页 + 书库索引页。"""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from . import util
from .intent import Intent

VERDICT_LABEL = {"accept": "可下载", "review": "待确认", "reject": "已排除", "pending": "未判定"}
CLOUD_LABEL = {"baidu": "百度", "quark": "夸克", "aliyun": "阿里", "115": "115",
               "uc": "UC", "xunlei": "迅雷", "123": "123", "tianyi": "天翼"}


def _md_escape(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def write_candidates_csv(cands: list[dict], path: Path) -> Path:
    util.ensure_dir(path.parent)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["分数", "判定", "网盘", "标题", "提取码", "链接", "体积GB", "文件数", "理由"])
        for c in cands:
            writer.writerow([
                c.get("score", 0), VERDICT_LABEL.get(c.get("verdict", ""), c.get("verdict", "")),
                CLOUD_LABEL.get(c.get("cloud", ""), c.get("cloud", "")),
                c.get("title", ""), c.get("password", ""), c.get("url", ""),
                round((c.get("size") or 0) / 1024 ** 3, 2), c.get("file_count", 0),
                "；".join(c.get("reasons") or [])[:500],
            ])
    return path


def write_report(
    path: Path,
    job: dict,
    intent: Intent,
    cands: list[dict],
    chosen: dict | None,
    stages: list[dict],
    result: dict,
) -> Path:
    util.ensure_dir(path.parent)
    lines: list[str] = []
    add = lines.append

    add(f"# 资源收割报告 · {job.get('prompt', '')}")
    add("")
    add(f"- 任务号：`{job.get('id', '')}`")
    add(f"- 生成时间：{util.stamp()}")
    add(f"- 解析结果：{intent.summary()}")
    add(f"- 搜索关键词：{' / '.join(intent.keywords)}")
    add("")

    add("## 一、结论")
    add("")
    if chosen:
        add(f"选定资源：**{chosen.get('title', '')}**")
        add("")
        add("| 项目 | 内容 |")
        add("|---|---|")
        add(f"| 网盘 | {CLOUD_LABEL.get(chosen.get('cloud', ''), chosen.get('cloud', ''))} |")
        add(f"| 链接 | {chosen.get('url', '')} |")
        add(f"| 提取码 | `{chosen.get('password', '') or '无'}` |")
        add(f"| 匹配分 | {chosen.get('score', 0)} / 100 |")
        add(f"| 判定 | {VERDICT_LABEL.get(chosen.get('verdict', ''), '')} |")
        add(f"| 体积 | {util.human_size(chosen.get('size') or 0)} |")
        add(f"| 文件数 | {chosen.get('file_count') or 0} |")
        add("")
        add("判定理由：")
        for reason in chosen.get("reasons") or []:
            add(f"- {reason}")
    else:
        add("没有资源通过校验，未执行下载。看看下面的候选表，或换更精确的说法再跑一次。")
    add("")

    add("## 二、候选清单")
    add("")
    add("| # | 分数 | 判定 | 网盘 | 标题 | 提取码 | 主要理由 |")
    add("|---|---|---|---|---|---|---|")
    for i, c in enumerate(cands, 1):
        reason = (c.get("reasons") or [""])[0]
        add(f"| {i} | {c.get('score', 0)} | {VERDICT_LABEL.get(c.get('verdict', ''), '')} | "
            f"{CLOUD_LABEL.get(c.get('cloud', ''), c.get('cloud', ''))} | "
            f"{_md_escape(c.get('title', ''))[:60]} | `{c.get('password', '')}` | "
            f"{_md_escape(reason)[:60]} |")
    add("")

    add("## 三、流水线执行记录")
    add("")
    add("| 阶段 | 结果 | 说明 |")
    add("|---|---|---|")
    for st in stages:
        add(f"| {st.get('stage', '')} | {'✅' if st.get('ok') else '❌'} | {_md_escape(st.get('message', ''))[:120]} |")
    add("")

    if result:
        add("## 四、成品")
        add("")
        add(f"- 书库位置：`{result.get('library', '')}`")
        add(f"- 入库文件：{result.get('files', 0)} 个，{util.human_size(result.get('size') or 0)}")
        add(f"- 解压出：{result.get('extracted', 0)} 个目录")
        if result.get("passwords"):
            add(f"- 使用过的解压密码：{', '.join(f'`{p}`' for p in result['passwords'])}")
        if result.get("warnings"):
            add("- 注意：")
            for w in result["warnings"]:
                add(f"  - {w}")
        add("")
        add("直接用资源管理器打开上面的书库目录即可阅读；也可以用 Komga / Koodo Reader / calibre 扫描这个目录。")
        add("")

    add("---")
    add("")
    add("提示：第三方客户端操作百度网盘存在账号被限速/风控的风险，建议用小号，并把并发调低"
        "（config.json → baidupcs.max_files / threads）。")
    util.ensure_dir(path.parent)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


HANDOFF_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>人工转存引导 · {title}</title>
<style>
:root{{color-scheme:light dark}}
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:960px;margin:32px auto;padding:0 16px}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border-bottom:1px solid #8884;padding:8px 6px;text-align:left;vertical-align:top}}
code{{background:#8882;padding:1px 5px;border-radius:4px}}
a{{color:#2b7cd3}}
.pill{{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#2b7cd322}}
button{{font:inherit;padding:2px 8px;border-radius:6px;border:1px solid #8886;background:transparent;cursor:pointer}}
.note{{background:#8881;padding:12px 14px;border-radius:8px;margin:16px 0}}
</style></head>
<body>
<h1>人工转存引导 · {title}</h1>
<div class="note">
<p><b>为什么是人工？</b>没有配置 BaiduPCS-Go 时，<b>转存</b>这一步必须由你在网页里点（第三方自动转存有账号风险）。</p>
<ol>
<li>点开下面的链接，输入提取码，把文件<b>转存到你自己的网盘</b>（建议转存到 <code>{remote_hint}</code>）；</li>
<li>用你习惯的方式把它们下载到 <code>{inbox}</code>（或者把下载目录软链过去）；</li>
<li>回到命令行执行：<code>harvest.bat resume {job_id}</code> —— 解压、清洗、入库、出报告全自动。</li>
</ol>
</div>
<h2>候选资源（按匹配度排序）</h2>
<table>
<thead><tr><th>#</th><th>匹配</th><th>标题</th><th>提取码</th><th>链接</th></tr></thead>
<tbody>
{rows}
</tbody></table>
<h2>另一个选择：全自动</h2>
<div class="note">
安装并登录 BaiduPCS-Go 之后，<code>transfer</code> + <code>download</code> 两步会由程序完成，不再需要人工。<br>
<code>harvest.bat setup</code> 会自动下载组件，然后 <code>harvest.bat login -cookies "BDUSS=...; STOKEN=..."</code>。
</div>
<p style="color:#888;font-size:12px">生成时间 {ts}</p>
<script>
document.querySelectorAll('[data-copy]').forEach(function(b){{
  b.addEventListener('click', function(){{
    navigator.clipboard.writeText(b.dataset.copy).then(function(){{
      var t=b.textContent; b.textContent='已复制'; setTimeout(function(){{b.textContent=t}},900);
    }});
  }});
}});
</script>
</body></html>
"""


def write_handoff(path: Path, job: dict, intent: Intent, cands: list[dict],
                  inbox: str, remote_hint: str) -> Path:
    rows: list[str] = []
    for i, c in enumerate(cands, 1):
        pwd = c.get("password", "") or ""
        pwd_html = (f'<code>{html.escape(pwd)}</code> '
                    f'<button data-copy="{html.escape(pwd)}">复制</button>') if pwd else "无"
        rows.append(
            f"<tr><td>{i}</td><td>{c.get('score', 0)} "
            f"<span class='pill'>{VERDICT_LABEL.get(c.get('verdict', ''), '')}</span></td>"
            f"<td>{html.escape(c.get('title', ''))}<br><span style='color:#888;font-size:12px'>"
            f"{html.escape((c.get('reasons') or [''])[0])}</span></td>"
            f"<td>{pwd_html}</td>"
            f"<td><a href='{html.escape(c.get('url', ''))}' target='_blank' rel='noreferrer'>"
            f"{html.escape(c.get('url', ''))}</a></td></tr>"
        )
    body = HANDOFF_TEMPLATE.format(
        title=html.escape(intent.title or job.get("prompt", "资源")),
        job_id=job.get("id", ""),
        remote_hint=html.escape(remote_hint),
        inbox=html.escape(inbox),
        rows="\n".join(rows) or "<tr><td colspan='5'>没有候选</td></tr>",
        ts=util.stamp(),
    )
    util.ensure_dir(path.parent)
    path.write_text(body, encoding="utf-8")
    return path


INDEX_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>我的书库</title>
<style>
body{{font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:880px;margin:36px auto;padding:0 16px}}
h1{{font-size:20px}} table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid #8883;padding:8px 6px;text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
code{{background:#8882;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body>
<h1>我的书库 · {count} 个系列</h1>
<p style="color:#888">目录：<code>{root}</code> —— 用 Komga / Koodo Reader / calibre 指向这里即可。</p>
<table><thead><tr><th>系列</th><th>文件数</th><th>体积</th><th>更新时间</th></tr></thead><tbody>
{rows}
</tbody></table>
</body></html>
"""


def write_index(path: Path, library_dir: str | Path, items: list[dict]) -> Path:
    rows = "\n".join(
        f"<tr><td>{html.escape(i['name'])}</td><td class='n'>{i['files']}</td>"
        f"<td class='n'>{util.human_size(i['size'])}</td><td>{i['mtime']}</td></tr>"
        for i in items
    ) or "<tr><td colspan='4'>书库还是空的</td></tr>"
    util.ensure_dir(path.parent)
    path.write_text(INDEX_TEMPLATE.format(count=len(items), root=html.escape(str(library_dir)),
                                          rows=rows), encoding="utf-8")
    return path


TOPIC_TEMPLATE_HEAD = """# 主题清单 · {topic}

- 生成时间：{ts}
- 搜索词：{queries}
- 候选总数：{total} 条（存活 {alive} 条）
- 网盘分布：{clouds}
- 可自动下载：{auto} 条（百度网盘）
"""


def write_topic_report(path: Path, topic: str, queries: list[str],
                       groups: dict[str, list[dict]], notes: list[str] | None = None) -> Path:
    """主题批量扫描的分组报告。"""
    util.ensure_dir(path.parent)
    all_cands = [c for items in groups.values() for c in items]
    alive = sum(1 for c in all_cands if (c.get("alive") or "") in ("ok", "locked"))
    auto = sum(1 for c in all_cands if c.get("cloud") == "baidu"
               and (c.get("alive") or "") in ("ok", "locked", "unknown"))
    cloud_count: dict[str, int] = {}
    for c in all_cands:
        cloud_count[c.get("cloud", "?")] = cloud_count.get(c.get("cloud", "?"), 0) + 1
    cloud_text = "、".join(f"{CLOUD_LABEL.get(k, k)} {v}" for k, v in
                          sorted(cloud_count.items(), key=lambda kv: -kv[1])) or "无"

    lines = [TOPIC_TEMPLATE_HEAD.format(
        topic=topic, ts=util.stamp(),
        queries=" / ".join(queries) or topic,
        total=len(all_cands), alive=alive, clouds=cloud_text, auto=auto,
    )]
    for label, items in groups.items():
        lines.append(f"\n## {label}（{len(items)} 条）\n")
        lines.append("| # | 匹配分 | 判定 | 网盘 | 标题 | 提取码 | 链接 |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, c in enumerate(items, 1):
            lines.append(
                f"| {i} | {c.get('score', 0)} | {VERDICT_LABEL.get(c.get('verdict', ''), '')} | "
                f"{CLOUD_LABEL.get(c.get('cloud', ''), c.get('cloud', ''))} | "
                f"{_md_escape(c.get('title', ''))[:70]} | `{c.get('password', '')}` | "
                f"{c.get('url', '')} |")
    if notes:
        lines.append("\n## 备注\n")
        for note in notes:
            lines.append(f"- {note}")
    lines.append("\n---\n")
    lines.append("提取码在表格里；链接失效的会被标成「已排除」。"
                 "想要自动下载，装好 BaiduPCS-Go 并 `harvest batch ... --download`。")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_json(data: Any, path: Path) -> Path:
    util.ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

