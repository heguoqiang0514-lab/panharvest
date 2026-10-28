"""命令行入口：harvest <一句话> / run / search / resume / setup / login / doctor / serve。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import config as config_mod
from . import intent as intent_mod
from . import organize, pansou, pcs, report, setup, util
from .config import Config
from .pipeline import Pipeline, PipelineError
from .state import State

KNOWN = {"run", "search", "batch", "resume", "setup", "login", "doctor", "selftest",
         "quark-dl", "serve", "extract", "library", "jobs", "help"}


def _say(title: str) -> None:
    util.log("")
    util.log(f"──── {title} " + "─" * max(0, 50 - len(title)))


def _print_result(result: dict) -> None:
    _say("结果")
    if result.get("ok"):
        util.log(f"✅ 已完成：{result.get('library')}")
        util.log(f"   文件 {result.get('files', 0)} 个，{util.human_size(result.get('size') or 0)}")
    else:
        util.log(f"⏹ 未完成：{result.get('message', '')}")
    chosen = result.get("chosen") or {}
    if chosen:
        util.log(f"   资源：{chosen.get('title', '')}")
        util.log(f"   链接：{chosen.get('url', '')} 提取码 {chosen.get('password') or '无'}")
        util.log(f"   匹配：{chosen.get('score')} 分 / {chosen.get('verdict')}")
    if result.get("report"):
        util.log(f"   报告：{result['report']}")
    if result.get("handoff"):
        util.log(f"   引导页：{result['handoff']}")
        util.log(f"   下一步：把文件放进 inbox 后执行  harvest resume {result.get('job_id')}")
    warn = result.get("warnings") or []
    if warn:
        util.log("   注意：")
        for w in warn[:5]:
            util.log(f"     · {w}")


# ---------------------------------------------------------------- 子命令

def cmd_run(args, cfg: Config, state: State) -> int:
    pipe = Pipeline(cfg, state)
    result = pipe.run(args.prompt, mode=args.mode, open_when_done=not args.no_open)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_result(result)
    return 0 if result.get("ok") else 2


def cmd_search(args, cfg: Config, state: State) -> int:
    pipe = Pipeline(cfg, state)
    result = pipe.run(args.prompt, mode="search", open_when_done=False)
    cands = result.get("candidates") or []
    _say(f"候选 {len(cands)} 条")
    for i, c in enumerate(cands, 1):
        flag = {"accept": "✅", "review": "❓", "reject": "✖"}.get(c.get("verdict", ""), "·")
        util.log(f"{i:>2}. {flag} {c.get('score', 0):>5} 分  [{c.get('cloud')}] "
                 f"{c.get('title', '')[:52]}")
        util.log(f"     {c.get('url')}  提取码 {c.get('password') or '无'}")
        if c.get("reasons"):
            util.log(f"     {c['reasons'][0][:80]}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("report"):
        util.log(f"\n报告：{result['report']}")
    return 0


def cmd_batch(args, cfg: Config, state: State) -> int:
    queries = [q for q in re.split(r"[,，\n]+", args.queries or "") if q.strip()]
    pipe = Pipeline(cfg, state)
    result = pipe.batch(args.topic, queries or None, type_hint=args.type,
                        max_candidates=args.max)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    _say(f"{result['topic']}：{result['total']} 条候选 / {len(result['groups'])} 个分类")
    for label, items in result["groups"].items():
        util.log(f"\n■ {label}（{len(items)} 条）")
        for c in items[:6]:
            flag = {"accept": "✅", "review": "❓", "reject": "✖"}.get(c.get("verdict", ""), "·")
            util.log(f"  {flag} {c.get('score', 0):>5}  [{c.get('cloud')}] "
                     f"{(c.get('title') or '')[:52]}")
            util.log(f"        {c.get('url')}  提取码 {c.get('password') or '无'}")
        if len(items) > 6:
            util.log(f"        …… 还有 {len(items) - 6} 条见清单")
    util.log(f"\n清单：{result['report']}")
    util.log(f"引导页：{result['handoff']}")
    return 0


def _quark_cookie_file(cfg: Config) -> Path:
    return cfg.path_of("data") / "quark_cookie.txt"


def cmd_quark_dl(args, cfg: Config, state: State) -> int:
    from . import quark_dl

    cookie_file = _quark_cookie_file(cfg)
    cookie = args.cookie or (cookie_file.read_text(encoding="utf-8").strip()
                             if cookie_file.is_file() else "")
    if not cookie:
        util.log('还没有夸克 Cookie。先执行： harvest login --quark "__puus=...; ..."', "ERROR")
        return 2
    out = Path(args.out) if args.out else cfg.path_of("paths.downloads") / util.slugify(args.url[-10:], 20)
    include = tuple(x.strip() for x in re.split(r"[,，]+", args.include or "") if x.strip())
    exclude = tuple(x.strip() for x in re.split(r"[,，]+", args.exclude or "") if x.strip())
    try:
        result = quark_dl.download_share(
            cookie, args.url, out,
            passcode=args.pwd or "",
            max_files=args.max,
            include=include, exclude=exclude,
            max_bytes=int(args.max_gb * 1024 ** 3) if args.max_gb else 0,
        )
    except quark_dl.QuarkError as exc:
        util.log(f"下载失败：{exc}", "ERROR")
        return 2
    util.log(f"完成：{len(result['files'])} 个文件 → {result['dir']}")
    for item in result["files"]:
        flag = "跳过" if item.get("skipped") else "下载"
        util.log(f"  {flag} {Path(item['path']).name}")
    return 0


def cmd_resume(args, cfg: Config, state: State) -> int:
    job = state.get_job(args.job_id)
    if not job:
        for recent in state.recent_jobs(20):
            if recent["id"].endswith(args.job_id):
                job = recent
                break
    if not job:
        util.log(f"找不到任务 {args.job_id}", "ERROR")
        return 2
    pipe = Pipeline(cfg, state)
    result = pipe.run(job["prompt"], mode="inbox", job_id=job["id"],
                      open_when_done=not args.no_open)
    _print_result(result)
    return 0 if result.get("ok") else 2


def cmd_setup(args, cfg: Config, state: State) -> int:
    out = setup.run_setup(cfg, want_baidupcs=not args.skip_baidupcs)
    if not out.get("sevenzip"):
        util.log("7-Zip 没装成功：.7z / AES 加密 zip 会解不开，可手动装 7-Zip 后重试。", "WARN")
    return 0


def cmd_login(args, cfg: Config, state: State) -> int:
    exe = cfg.resolve_pcs_exe()
    if not exe:
        util.log("还没装 BaiduPCS-Go，先跑：harvest setup", "ERROR")
        return 2
    client = pcs.BaiduPCS(exe, cfg.path_of("baidupcs.config_dir"),
                          remote_root=str(cfg.get("baidupcs.remote_root") or "/资源收割"))
    if args.show:
        info = client.whoami()
        util.log(f"当前账号：uid={info['uid']} {info.get('name', '')}"
                 + ("（已登录）" if info["logged_in"] else "（未登录）"))
        return 0 if info["logged_in"] else 2
    if args.logout:
        util.log(json.dumps(client.logout(), ensure_ascii=False))
        return 0
    if args.quark:
        from . import quark_dl

        cookie = args.quark.strip()
        if "=" not in cookie:
            util.log('Cookie 格式不对，应该是 "__puus=xxx; __pus=yyy; ..." 这样', "ERROR")
            return 2
        try:
            info = quark_dl.QuarkSession(cookie).whoami()
            util.log(f"✅ 夸克 Cookie 可用：{info.get('nickname') or '(未取到昵称)'}")
        except Exception as exc:
            util.log(f"⚠️ 夸克 Cookie 校验没通过（仍然保存，可稍后重试）：{exc}", "WARN")
        path = _quark_cookie_file(cfg)
        util.ensure_dir(path.parent)
        path.write_text(cookie, encoding="utf-8")
        util.log(f"已保存到 {path}（只在本机，不会上传）")
        return 0
    if args.cookies:
        info = client.login_cookies(args.cookies)
    elif args.bduss:
        info = client.login_bduss(args.bduss, args.stoken or "")
    else:
        util.log("用法：harvest login --cookies \"BDUSS=...; STOKEN=...\"  "
                 "或  harvest login --bduss <BDUSS> --stoken <STOKEN>", "ERROR")
        return 2
    if info.get("ok"):
        who = client.whoami()
        util.log(f"✅ 登录成功：uid={who['uid']} {who.get('name', '')}")
        util.log("Cookie 只存在本地配置文件里（tools 同级的 data/baidupcs-config），不会上传。")
        return 0
    util.log(f"❌ 登录失败：{info.get('message')}", "ERROR")
    return 2


def cmd_doctor(args, cfg: Config, state: State) -> int:
    _say("环境检查")
    rows = cfg.summary()
    for name, value, note in rows:
        util.log(f"{name:<14} {value}")
        if note:
            util.log(f"{'':<14} {note}")
    _say("PanSou")
    ps = pansou.PanSou(str(cfg.get("pansou.base") or ""),
                       str(cfg.get("pansou.token") or ""), timeout=10)
    health = ps.health()
    util.log(f"地址 {ps.base}")
    util.log(f"状态 {health.get('status')}"
             + (f"｜插件 {health.get('plugin_count')} 个｜频道 {health.get('channels_count')} 个"
                if health.get("status") == "ok" else f"｜{health.get('error', '')}"))
    if health.get("status") != "ok":
        util.log("启动方式： docker run -d --name pansou -p 8888:8888 ghcr.io/fish2018/pansou:latest")
    _say("BaiduPCS-Go")
    exe = cfg.resolve_pcs_exe()
    if exe:
        client = pcs.BaiduPCS(exe, cfg.path_of("baidupcs.config_dir"))
        info = client.whoami()
        util.log(f"程序 {exe}")
        util.log(f"账号 uid={info['uid']} {info.get('name', '')}"
                 + ("（已登录，可全自动）" if info["logged_in"] else "（未登录，只能人工转存）"))
    else:
        util.log("未安装（自动转存/下载不可用，将走人工转存 + inbox 模式）")
        util.log("安装： harvest setup")
    _say("解压能力")
    ex = cfg.get("extract") or {}
    for key, label in (("sevenzip", "7-Zip/7za"), ("sevenzr", "7zr"), ("unrar", "UnRAR")):
        path = ex.get(key) or ""
        util.log(f"{label:<10} {path or '未找到'}")
    util.log("zip/tar 由 Python 标准库处理，始终可用。")
    _say("任务统计")
    stats = state.stats()
    util.log(f"历史任务 {stats['jobs']} 个，候选 {stats['candidates']} 条")
    return 0


def cmd_selftest(args, cfg: Config, state: State) -> int:
    from . import selftest as selftest_mod

    return selftest_mod.run_all()


def cmd_serve(args, cfg: Config, state: State) -> int:
    from . import server

    return server.serve(cfg, state, port=args.port, open_browser=not args.no_open)


def cmd_extract(args, cfg: Config, state: State) -> int:
    src = Path(args.path)
    if not src.is_dir():
        util.log(f"目录不存在：{src}", "ERROR")
        return 2
    from . import extract

    it = intent_mod.parse(args.into or src.name)
    if args.into:            # --into 就是系列名，不要再让解析器改写它
        it.title = args.into
    for d in [src]:
        removed, _notes = organize.clean_junk(d, it)
        if removed:
            util.log(f"清理 {removed} 个垃圾项")
    passwords = extract.gather_passwords({}, src, cfg.get("extract.passwords") or [])
    res = extract.extract_all(src, passwords, dict(cfg.get("extract") or {}), work_root=src)
    util.log(f"解压：成功 {sum(1 for a in res.archives if a['ok'])}/{len(res.archives)}")
    if res.errors:
        for err in res.errors[:5]:
            util.log(f"  {err}", "WARN")
    if args.organize:
        # 解压完再清一次垃圾（广告文件通常在压缩包里面）
        for out in res.output_dirs:
            organize.clean_junk(out, it)
        org = organize.organize(res.output_dirs, it, cfg.path_of("paths.library"),
                                media_ext=cfg.get("organize.media_ext"))
        util.log(f"入库：{org.files} 个文件 → {org.target}")
        for note in org.notes[:5]:
            util.log(f"  {note}", "WARN")
    return 0


def cmd_library(args, cfg: Config, state: State) -> int:
    items = organize.library_manifest(cfg.path_of("paths.library"))
    _say(f"书库 {cfg.path_of('paths.library')}")
    if not items:
        util.log("（空的）")
        return 0
    for i, item in enumerate(items, 1):
        util.log(f"{i:>2}. {item['name']:<36} {item['files']:>5} 个文件  "
                 f"{util.human_size(item['size']):>9}  {item['mtime']}")
    total = sum(i["size"] for i in items)
    util.log(f"合计 {len(items)} 个系列，{util.human_size(total)}")
    return 0


def cmd_jobs(args, cfg: Config, state: State) -> int:
    _say("最近任务")
    for job in state.recent_jobs(args.limit):
        util.log(f"{job['id']}  {job['status']:<8} {job['stage']:<10} {job['prompt'][:40]}")
    return 0


# ---------------------------------------------------------------- 解析器

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harvest",
        description="网盘资源收割机：一句话 → 找链接 → 校验 → 转存下载 → 解压 → 入库")
    p.add_argument("-c", "--config", help="配置文件路径（默认 config.json）")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="完整流水线")
    r.add_argument("prompt", help="你想要什么，例如：葬送的芙莉莲 漫画 第1-10卷")
    r.add_argument("--mode", default="auto", choices=["auto", "search", "inbox"],
                   help="auto=能自动就自动；search=只找链接；inbox=从 inbox 取文件继续")
    r.add_argument("--no-open", action="store_true", help="完成后不要自动打开目录")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("search", help="只搜链接，出候选表")
    s.add_argument("prompt")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    b = sub.add_parser("batch", help="主题批量扫描：一个主题、多个关键词 → 分类清单")
    b.add_argument("topic", help="主题，如 考研数学一")
    b.add_argument("-q", "--queries", help="逗号分隔的搜索词；不填就用主题本身")
    b.add_argument("--type", default="book",
                   choices=["book", "comic", "video", "audio", "unknown"])
    b.add_argument("--max", type=int, default=60)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_batch)

    rs = sub.add_parser("resume", help="继续某个任务（人工转存后）")
    rs.add_argument("job_id")
    rs.add_argument("--no-open", action="store_true")
    rs.set_defaults(func=cmd_resume)

    st = sub.add_parser("setup", help="下载 7-Zip 控制台与 BaiduPCS-Go")
    st.add_argument("--skip-baidupcs", action="store_true")
    st.set_defaults(func=cmd_setup)

    lg = sub.add_parser("login", help="登录百度网盘（BaiduPCS-Go）")
    lg.add_argument("--cookies", help='百度整段 Cookie，如 "BDUSS=xxx; STOKEN=yyy"')
    lg.add_argument("--quark", help='夸克整段 Cookie，如 "__puus=xxx; ..."')
    lg.add_argument("--bduss")
    lg.add_argument("--stoken")
    lg.add_argument("--show", action="store_true", help="查看当前登录状态")
    lg.add_argument("--logout", action="store_true")
    lg.set_defaults(func=cmd_login)

    d = sub.add_parser("doctor", help="环境自检")
    d.set_defaults(func=cmd_doctor)

    stf = sub.add_parser("selftest", help="本地逻辑自检（不联网）")
    stf.set_defaults(func=cmd_selftest)

    sv = sub.add_parser("serve", help="打开本地网页界面")
    sv.add_argument("--port", type=int, default=0)
    sv.add_argument("--no-open", action="store_true")
    sv.set_defaults(func=cmd_serve)

    ex = sub.add_parser("extract", help="只对某个目录做解压/入库")
    ex.add_argument("path")
    ex.add_argument("--into", help="系列名（决定入库目录名）")
    ex.add_argument("--organize", action="store_true", help="顺手入库")
    ex.set_defaults(func=cmd_extract)

    q = sub.add_parser("quark-dl", help="下载夸克分享到本地（需要夸克 Cookie）")
    q.add_argument("url")
    q.add_argument("--pwd", help="提取码")
    q.add_argument("--out", help="保存目录")
    q.add_argument("--max", type=int, default=40, help="最多下载几个文件")
    q.add_argument("--include", help="只下路径含这些词的（逗号分隔）")
    q.add_argument("--exclude", help="跳过路径含这些词的（逗号分隔，如 数二,数三）")
    q.add_argument("--max-gb", dest="max_gb", type=float, default=0, help="总体积上限 GB")
    q.add_argument("--cookie", help="临时用这段 Cookie，而不是已保存的那份")
    q.set_defaults(func=cmd_quark_dl)

    lib = sub.add_parser("library", help="看书库里有什么")
    lib.set_defaults(func=cmd_library)

    jb = sub.add_parser("jobs", help="最近的任务")
    jb.add_argument("--limit", type=int, default=15)
    jb.set_defaults(func=cmd_jobs)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in KNOWN and not argv[0].startswith("-"):
        argv = ["run", *argv]
    if not argv:
        print(build_parser().format_help())
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    cfg.ensure_dirs()
    state = State(cfg.path_of("paths.data") / "state.db")
    try:
        return int(args.func(args, cfg, state) or 0)
    except PipelineError as exc:
        util.log(str(exc), "ERROR")
        return 2
    except KeyboardInterrupt:
        util.log("已中断", "WARN")
        return 130
    finally:
        state.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
