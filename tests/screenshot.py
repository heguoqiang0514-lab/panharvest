"""用无头 Edge/Chrome 给界面拍一张"跑完之后"的截图，方便在文档里预览。

只用于开发/文档，不参与运行时。

    python tests/screenshot.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from panharvest import server  # noqa: E402

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
]

SAMPLE = {
    "ok": True,
    "message": "完成，成品已入库",
    "job_id": "20261025-095155-b8e4cb",
    "library": str(ROOT / "library" / "葬送的芙莉莲"),
    "report": str(ROOT / "reports" / "葬送的芙莉莲-b8e4" / "report.md"),
    "files": 30,
    "size": 314572800,
    "warnings": ["第三方客户端操作百度网盘存在限速风险，建议用小号"],
    "candidates": [
        {"score": 98.6, "verdict": "accept", "cloud": "baidu",
         "title": "葬送的芙莉莲 漫画 1-10卷 全彩汉化",
         "url": "https://pan.baidu.com/s/1goodshare", "password": "abcd",
         "reasons": ["标题高度吻合（100%）", "卷号覆盖 10/10", "含压缩包，可直接解压"]},
        {"score": 72.0, "verdict": "review", "cloud": "quark",
         "title": "葬送的芙莉莲 漫画 全集", "url": "https://pan.quark.cn/s/quark123",
         "password": "", "reasons": ["标题高度吻合（100%）", "未见卷号（目标 卷1-10）"]},
        {"score": 31.1, "verdict": "reject", "cloud": "baidu",
         "title": "葬送的芙莉莲 剧场版 预告片",
         "url": "https://pan.baidu.com/s/1deadshare", "password": "efgh",
         "reasons": ["链接已失效", "标题含可疑词：预告"]},
    ],
}

LOGS = [
    "[10:21:04] INFO  收到需求：葬送的芙莉莲 漫画 第1-10卷 高清",
    "[10:21:04] INFO  解析结果：类型=comic / 标题=葬送的芙莉莲 / 卷=卷1-10 / 关键词=3 组",
    "[10:21:05] STAGE [search] OK 候选 3 条｜关键词「葬送的芙莉莲」抓到 47 条，新增 47 条",
    "[10:21:06] STAGE [check]  OK 存活 31/47（另 16 条未确认或失效）",
    "[10:21:08] STAGE [probe]  OK 9/12 条拿到文件清单",
    "[10:21:08] STAGE [rank]   OK 可下载 2 条，待确认 4 条",
    "[10:21:09] INFO  尝试第 1 个候选：葬送的芙莉莲 漫画 1-10卷 全彩汉化（分数 80.0，判定 accept）",
    "[10:21:12] STAGE [transfer] OK 转存成功：葬送的芙莉莲 1-10卷",
    "[10:21:13] INFO  真实文件清单：11 项，300.0MB，复核 98.6 分 / accept",
    "[10:21:13] INFO    · 卷号覆盖 10/10",
    "[10:21:47] STAGE [download] OK 下载 10 个文件",
    "[10:21:49] STAGE [extract]  OK 10 个压缩包，成功 10 个，输出 1.2GB",
    "[10:21:50] INFO  清掉 24 个垃圾文件/目录：葬送的芙莉莲 第01卷",
    "[10:21:51] STAGE [organize] OK 入库 2140 个文件 / 1.2GB → …\\library\\葬送的芙莉莲",
    "[10:21:51] INFO  总耗时 47.3s",
]

LIBRARY = [
    {"name": "葬送的芙莉莲", "path": str(ROOT / "library" / "葬送的芙莉莲"),
     "files": 2140, "size": 1288490188, "mtime": "2026-10-25 10:21"},
    {"name": "电锯人", "path": str(ROOT / "library" / "电锯人"),
     "files": 1680, "size": 943718400, "mtime": "2026-10-24 22:02"},
]


def build_html() -> str:
    page = server.PAGE.replace("%%BACKEND%%", "BaiduPCS-Go 全自动")
    inject = f"""
<script>
document.querySelector('#prompt').value = "葬送的芙莉莲 漫画 第1-10卷 高清";
document.querySelector('#log').textContent = {json.dumps(chr(10).join(LOGS), ensure_ascii=False)};
setStatus('已完成', 'ok');
renderResult({json.dumps(SAMPLE, ensure_ascii=False)});
const lib = {json.dumps(LIBRARY, ensure_ascii=False)};
document.querySelector('#library').innerHTML = '<table><tbody>' + lib.map(i =>
  '<tr><td><a href="#">' + i.name + '</a></td><td class="n">' + i.files + ' 个</td>'
  + '<td class="n">' + (i.size/1048576).toFixed(0) + 'MB</td>'
  + '<td class="mut">' + i.mtime + '</td></tr>').join('') + '</tbody></table>';
</script>
"""
    return page.replace("</body>", inject + "</body>")


def main() -> int:
    html_path = ROOT / "reports" / "_ui_preview.html"
    png_path = ROOT / "reports" / "ui-preview.png"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_html(), encoding="utf-8")

    exe = next((b for b in BROWSERS if shutil.which(b) or Path(b).is_file()), None)
    if not exe:
        print("没有可用的浏览器，跳过截图")
        return 0
    url = html_path.resolve().as_uri()
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--allow-file-access-from-files", f"--screenshot={png_path}",
           "--window-size=1080,1560", "--virtual-time-budget=3000", url]
    res = subprocess.run(cmd, capture_output=True, timeout=90)
    if png_path.is_file():
        print(f"截图已生成：{png_path} ({png_path.stat().st_size} bytes)")
        return 0
    print("截图失败：", res.stderr.decode("utf-8", "replace")[-500:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
