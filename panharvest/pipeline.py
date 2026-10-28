"""流水线编排：一句话 → 网盘链接 → 校验 → 转存 → 下载 → 解压 → 入库 → 报告。

阶段（每一步都会写进 job 记录，失败可从中断处续跑）：
    intent → search → check → rank → select → transfer → inspect
           → download → extract → organize → report

两种执行模式：
- auto   ：有 BaiduPCS-Go 且已登录时全自动
- manual ：只看链接和提取码，人工转存后把文件放进 inbox，再 resume
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import baidu, extract, intent as intent_mod, organize, pansou, pcs, rank, report, util
from .config import Config
from .state import State

EventSink = Callable[[str, dict], None]


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, cfg: Config, state: State | None = None,
                 on_event: EventSink | None = None):
        self.cfg = cfg
        self.paths = cfg.ensure_dirs()
        self.state = state or State(self.paths["data"] / "state.db")
        self.on_event = on_event
        self.stages: list[dict] = []
        self.warnings: list[str] = []
        self._pcs: pcs.BaiduPCS | None = None
        self._backend_cache: str | None = None

    # ------------------------------------------------------------ 基础设施
    def emit(self, kind: str, payload: dict | None = None) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload or {})
            except Exception:
                pass

    def record(self, stage: str, ok: bool, message: str, **extra: Any) -> None:
        entry = {"stage": stage, "ok": ok, "message": message, "ts": util.stamp()}
        entry.update(extra)
        self.stages.append(entry)
        self.emit("stage", entry)
        util.log(f"[{stage}] {'OK' if ok else 'FAIL'} {message}", "STAGE" if ok else "WARN")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        util.log(message, "WARN")

    # ------------------------------------------------------------ 后端
    def backend(self) -> str:
        """返回 pcs / manual。"""
        if self._backend_cache:
            return self._backend_cache
        mode = str(self.cfg.get("backends.transfer", "auto")).lower()
        if mode == "manual":
            self._backend_cache = "manual"
            return "manual"
        exe = self.cfg.resolve_pcs_exe()
        if not exe:
            if mode == "pcs":
                raise PipelineError(
                    "backends.transfer=pcs 但没找到 BaiduPCS-Go。先运行 `harvest setup` 下载，"
                    "或在 config.json 里填 baidupcs.exe 的路径。")
            self._backend_cache = "manual"
            return "manual"
        client = self.pcs_client(exe)
        info = client.whoami()
        if not info.get("logged_in"):
            if mode == "pcs":
                raise PipelineError("BaiduPCS-Go 未登录，先运行 `harvest login` 保存 Cookie。")
            util.log("BaiduPCS-Go 已安装但未登录，本次走人工转存模式", "WARN")
            self._backend_cache = "manual"
            return "manual"
        util.log(f"BaiduPCS-Go 已登录：uid={info['uid']} {info.get('name', '')}")
        self._backend_cache = "pcs"
        return "pcs"

    def pcs_client(self, exe: str | None = None) -> pcs.BaiduPCS:
        if self._pcs is None:
            exe = exe or self.cfg.resolve_pcs_exe()
            if not exe:
                raise PipelineError("未找到 BaiduPCS-Go 可执行文件")
            self._pcs = pcs.BaiduPCS(
                exe=exe,
                config_dir=self.cfg.path_of("baidupcs.config_dir"),
                remote_root=self.cfg.get("baidupcs.remote_root") or "/资源收割",
                threads=int(self.cfg.get("baidupcs.threads") or 0),
                max_files=int(self.cfg.get("baidupcs.max_files") or 2),
                mode=str(self.cfg.get("baidupcs.download_mode") or "locate"),
                timeout=float(self.cfg.get("extract.timeout") or 3600),
            )
        return self._pcs

    # ------------------------------------------------------------ 主流程
    def run(self, prompt: str, mode: str = "auto", job_id: str | None = None,
            open_when_done: bool | None = None) -> dict:
        started = time.time()
        intent = intent_mod.parse(prompt, int(self.cfg.get("limits.search_variants", 3)))
        util.log("=" * 60)
        util.log(f"收到需求：{prompt}")
        util.log(f"解析结果：{intent.summary()}")
        util.log(f"搜索词：{' | '.join(intent.keywords)}")

        job = self.state.get_job(job_id) if job_id else None
        if job is None:
            job = self.state.create_job(prompt, intent.to_dict(),
                                        {k: str(v) for k, v in self.paths.items()})
        else:
            intent = intent_mod.Intent.from_dict(job.get("intent") or intent.to_dict())
        job_id = job["id"]
        self.state.update_job(job_id, status="running", stage="intent")
        slug = util.slugify(intent.title or prompt, 40, "resource")
        job_slug = f"{slug}-{job_id[-4:]}"
        self.record("intent", True, intent.summary())

        result: dict[str, Any] = {
            "job_id": job_id, "prompt": prompt, "intent": intent.to_dict(),
            "mode": mode, "slug": job_slug, "candidates": [], "chosen": None,
            "library": "", "files": 0, "size": 0, "warnings": self.warnings,
            "stages": self.stages, "ok": False, "message": "",
        }

        # ---- 只要链接（人工模式立即返回）
        cands = self._search(intent, result)
        if mode == "search":
            result["ok"] = bool(cands)
            result["message"] = f"搜到 {len(cands)} 条候选"
            self._finish(job_id, result, job, intent)
            return result

        backend = self.backend()
        util.log(f"执行后端：{'BaiduPCS-Go 自动' if backend == 'pcs' else '人工转存 + inbox 续跑'}")

        if mode == "inbox" or backend == "manual":
            inbox = self.paths["inbox"]
            util.ensure_dir(inbox)
            handoff = report.write_handoff(
                self.paths["reports"] / job_slug / "handoff.html", job, intent, cands,
                str(inbox), str(self.cfg.get("baidupcs.remote_root") or "/资源收割"))
            self.record("handoff", True, f"已生成人工转存引导页 {handoff}")
            local_dir = self.paths["downloads"] / job_slug
            util.ensure_dir(local_dir)
            moved = self._pull_from_inbox(inbox, local_dir)
            if moved == 0:
                result["handoff"] = str(handoff)
                result["message"] = ("没有可自动转存的账号，已生成引导页；把文件放进 inbox 后运行 "
                                     f"`harvest resume {job_id}`")
                result["ok"] = False
                self._finish(job_id, result, job, intent)
                return result
            util.log(f"inbox 里取到 {moved} 个文件，继续解压入库")
        else:
            chosen = self._acquire(intent, cands, job_slug, result)
            if not chosen:
                result["message"] = "没有资源通过校验，未下载。候选清单见报告。"
                self._finish(job_id, result, job, intent)
                return result
            local_dir = Path(chosen["local_dir"])

        # ---- 解压
        extract_res = self._extract(local_dir, result)
        # ---- 入库
        if extract_res.get("ok"):
            self._organize(extract_res, intent, result)

        result["ok"] = bool(result.get("library"))
        result["message"] = ("完成，成品已入库" if result["ok"] else "入库失败，请看日志")
        self._finish(job_id, result, job, intent)
        if open_when_done is None:
            open_when_done = bool(self.cfg.get("open_when_done", True))
        if result["ok"] and open_when_done:
            util.open_path(result["library"])
        util.log(f"总耗时 {time.time() - started:.1f}s")
        return result

    # ------------------------------------------------------------ 主题批量扫描
    def pansou_client(self) -> pansou.PanSou:
        return pansou.PanSou(
            base=str(self.cfg.get("pansou.base") or ""),
            token=str(self.cfg.get("pansou.token") or ""),
            timeout=float(self.cfg.get("pansou.timeout") or 90),
            refresh=bool(self.cfg.get("pansou.refresh", False)),
        )

    def batch(self, topic: str, queries: list[str] | None = None,
              type_hint: str = "book", max_candidates: int = 60) -> dict:
        """一个主题、多个关键词 → 分类清单（不下载，只出结果和引导页）。"""
        from . import topic as topic_mod

        intent = intent_mod.parse(topic)
        if type_hint in intent_mod.TYPE_EXPECT and type_hint != "unknown":
            intent.type = type_hint
            intent.expect_ext = list(intent_mod.TYPE_EXPECT[type_hint])
        queries = [q for q in (queries or []) if q] or [topic]

        job = self.state.create_job(
            topic, intent.to_dict(), {k: str(v) for k, v in self.paths.items()})
        job_id = job["id"]
        slug = util.slugify(topic, 40, "topic")
        self.state.update_job(job_id, status="running", stage="batch")
        util.log("=" * 60)
        util.log(f"主题批量扫描：{topic}")
        util.log(f"关键词 {len(queries)} 个：{' / '.join(queries)}")

        ps = self.pansou_client()
        if not ps.reachable():
            raise PipelineError(
                f"连不上 PanSou（{ps.base}）。先起服务：\n"
                "    docker run -d --name pansou -p 8888:8888 ghcr.io/fish2018/pansou:latest")
        cands, notes = topic_mod.batch_search(
            ps, queries, self.cfg.get("clouds"), max_candidates=max_candidates)
        self.record("search", bool(cands), f"候选 {len(cands)} 条")

        states = ps.check_links(cands)
        for cand in cands:
            info = states.get(pansou.url_key(cand["url"])) or {}
            cand["alive"] = info.get("state", "unknown")
        alive = sum(1 for c in cands if c.get("alive") in ("ok", "locked"))
        self.record("check", True, f"存活 {alive}/{len(cands)}")

        ranked = rank.rank(cands, intent, dict(self.cfg.get("limits") or {}))
        groups = topic_mod.group_candidates(ranked)
        self.record("rank", True,
                    f"{len(groups)} 个分类，accept {sum(1 for c in ranked if c['verdict'] == 'accept')} 条")

        self.state.add_candidates(job_id, ranked)
        for cand in ranked:
            self.state.update_candidate(
                job_id, cand["url"], score=cand.get("score", 0),
                verdict=cand.get("verdict", "pending"), alive=cand.get("alive", "unknown"),
                reasons=cand.get("reasons", []), size_bytes=cand.get("size", 0),
                file_count=cand.get("file_count", 0))

        reports = self.paths["reports"] / f"{util.now():%Y%m%d}-{slug}"
        rp = report.write_topic_report(reports / "清单.md", topic, queries, groups, notes)
        report.write_candidates_csv(ranked, reports / "candidates.csv")
        handoff = report.write_handoff(
            reports / "handoff.html", job, intent, ranked[:40],
            str(self.paths["inbox"]), str(self.cfg.get("baidupcs.remote_root") or "/资源收割"))
        self.state.update_job(job_id, status="done", stage="batch")
        util.log(f"清单：{rp}")
        util.log(f"引导页：{handoff}")
        return {
            "job_id": job_id, "topic": topic, "queries": queries,
            "total": len(ranked), "alive": alive, "groups": groups,
            "report": str(rp), "handoff": str(handoff),
            "candidates": [_slim(c) for c in ranked],
        }

    # ------------------------------------------------------------ 搜索 + 校验 + 排序
    def _search(self, intent: intent_mod.Intent, result: dict) -> list[dict]:
        ps = self.pansou_client()
        if not ps.reachable():
            raise PipelineError(
                f"连不上 PanSou（{ps.base}）。启动方式：\n"
                "    docker run -d --name pansou -p 8888:8888 ghcr.io/fish2018/pansou:latest\n"
                "然后在 config.json 里确认 pansou.base，或先跑 `harvest doctor`。")

        max_c = int(self.cfg.get("limits.max_candidates", 20))
        cands, notes = ps.collect(intent.keywords, self.cfg.get("clouds"), max_c)
        self.record("search", bool(cands), f"候选 {len(cands)} 条｜" + "；".join(notes))
        if not cands:
            result["candidates"] = []
            return []

        # ---- 存活检测：先问 PanSou 官方接口
        states = ps.check_links(cands)
        for cand in cands:
            key = pansou.url_key(cand["url"])
            info = states.get(key)
            cand["alive"] = (info or {}).get("state", "unknown")
            cand["alive_summary"] = (info or {}).get("summary", "")
            if cand["alive"] in ("bad", "dead"):
                self.state.remember_link(cand["url"], "bad")
        ok_n = sum(1 for c in cands if c.get("alive") in ("ok", "locked"))
        self.record("check", True, f"存活 {ok_n}/{len(cands)}（另 "
                                   f"{len(cands) - ok_n} 条未确认或失效）")

        # ---- 百度链接做深度探测（拿文件清单）
        verify_top = int(self.cfg.get("limits.verify_top", 12))
        baidu_cands = [c for c in cands if c.get("cloud") == "baidu"
                       and c.get("alive") not in ("bad", "dead")][:verify_top]
        if baidu_cands:
            util.log(f"深入探测 {len(baidu_cands)} 条百度分享（只读，不登录）")
        for cand in baidu_cands:
            probe = baidu.probe(cand["url"], cand.get("password", ""),
                               f"{cand.get('title', '')} {cand.get('note', '')}")
            cand["probe"] = probe.to_dict()
            if probe.files:
                cand["files"] = probe.files
            if probe.alive is True:
                cand["alive"] = "ok"
            elif probe.alive is False:
                cand["alive"] = "bad"
            if probe.needs_pwd and not cand.get("password"):
                cand["password"] = find_password(cand) or ""
        probed = sum(1 for c in baidu_cands if c.get("files"))
        if baidu_cands:
            self.record("probe", True, f"{probed}/{len(baidu_cands)} 条拿到文件清单")

        # ---- 打分排序
        limits = dict(self.cfg.get("limits") or {})
        ranked = rank.rank(cands, intent, limits)
        got = sum(1 for c in ranked if c.get("verdict") == "accept")
        self.record("rank", True, f"可下载 {got} 条，待确认 "
                                  f"{sum(1 for c in ranked if c.get('verdict') == 'review')} 条")

        # ---- 写库
        self.state.add_candidates(result["job_id"], ranked)
        for cand in ranked:
            self.state.update_candidate(
                result["job_id"], cand["url"],
                score=cand.get("score", 0), verdict=cand.get("verdict", "pending"),
                alive=cand.get("alive", "unknown"), reasons=cand.get("reasons", []),
                probe=cand.get("probe", {}), size_bytes=cand.get("size", 0),
                file_count=cand.get("file_count", 0),
            )
        result["candidates"] = [_slim(c) for c in ranked]
        return ranked

    # ------------------------------------------------------------ 转存 → 复核 → 下载
    def _acquire(self, intent: intent_mod.Intent, cands: list[dict],
                 job_slug: str, result: dict) -> dict | None:
        client = self.pcs_client()
        remote_dir = client.job_dir(job_slug)
        local_dir = self.paths["downloads"] / job_slug
        util.ensure_dir(local_dir)
        limits = dict(self.cfg.get("limits") or {})

        candidates = [c for c in cands if c.get("cloud") == "baidu"
                      and c.get("verdict") in ("accept", "review")]
        if not candidates:
            others = [c for c in cands if c.get("cloud") != "baidu"
                      and c.get("verdict") in ("accept", "review")]
            if others:
                result["message"] = ("有匹配的非百度资源，但自动下载只支持百度网盘："
                                     f"{others[0].get('url')} 提取码 {others[0].get('password')}")
                self.warn(result["message"])
            return None

        for index, cand in enumerate(candidates[:3], 1):
            util.log(f"尝试第 {index} 个候选：{cand.get('title', '')[:40]} "
                     f"（分数 {cand.get('score')}，判定 {cand.get('verdict')}）")
            tr = client.transfer(cand["url"], cand.get("password", ""), remote_dir, collect=True)
            if not tr.get("ok"):
                self.record("transfer", False, f"转存失败：{tr.get('message')}")
                self.state.remember_link(cand["url"], "bad")
                continue
            saved = tr.get("saved") or ""
            self.record("transfer", True, f"转存成功：{saved or '（未解析到文件名）'}")
            self.state.update_candidate(result["job_id"], cand["url"], state="transferred")

            remote_paths = [f"{remote_dir}/{saved}"] if saved else [remote_dir]
            listing = client.ls(remote_paths[0], max_depth=2)
            files = listing.get("entries") or []
            if files:
                deep = rank.deep_verdict(files, intent, limits, cand)
                util.log(f"真实文件清单：{len(files)} 项，{util.human_size(listing.get('total_size'))}，"
                         f"复核 {deep['score']} 分 / {deep['verdict']}")
                for reason in deep["reasons"][:4]:
                    util.log(f"  · {reason}")
                cand.update({
                    "score": deep["score"], "verdict": deep["verdict"],
                    "reasons": deep["reasons"], "size": deep["size"],
                    "file_count": deep["file_count"],
                })
                self.state.update_candidate(
                    result["job_id"], cand["url"], score=deep["score"],
                    verdict=deep["verdict"], reasons=deep["reasons"],
                    size_bytes=deep["size"], file_count=deep["file_count"], state="checked")
                if deep["verdict"] == "reject":
                    self.record("inspect", False, "文件清单与需求不符，换下一个候选")
                    self._cleanup_remote(client, remote_paths)
                    continue
            else:
                self.record("inspect", True, "拿不到文件清单，按标题判定继续")

            dl = client.download(remote_paths, str(local_dir))
            got = len(dl.get("saved") or []) + len(dl.get("skipped") or [])
            self.record("download", bool(dl.get("ok")),
                        f"下载 {got} 个文件" + (f"｜{dl.get('message', '')}" if not dl.get("ok") else ""))
            if not dl.get("ok"):
                self._cleanup_remote(client, remote_paths)
                continue
            cand["local_dir"] = str(local_dir)
            result["chosen"] = _slim(cand)
            self.state.update_candidate(result["job_id"], cand["url"], state="chosen")
            self.state.update_job(result["job_id"], chosen_url=cand["url"])
            if not self.cfg.get("baidupcs.keep_remote", True):
                self._cleanup_remote(client, remote_paths)
            return cand
        return None

    def _cleanup_remote(self, client: pcs.BaiduPCS, paths: list[str]) -> None:
        try:
            for path in paths:
                client.rm(path)
            util.log("已清理网盘内的临时转存副本")
        except Exception as exc:  # 清理失败不影响主流程
            util.log(f"清理网盘副本失败：{exc}", "WARN")

    def _pull_from_inbox(self, inbox: Path, local_dir: Path) -> int:
        """人工模式下：把 inbox 里的东西搬进本次任务的下载目录。"""
        inbox = Path(inbox)
        if not inbox.is_dir():
            return 0
        count = 0
        for entry in list(inbox.iterdir()):
            if entry.name.startswith("."):
                continue
            dest = local_dir / entry.name
            try:
                if entry.is_dir():
                    if dest.exists():
                        continue
                    shutil.move(str(entry), str(dest))
                    count += 1
                else:
                    if dest.exists():
                        dest.unlink()
                    shutil.move(str(entry), str(dest))
                    count += 1
            except (OSError, shutil.Error) as exc:
                self.warn(f"从 inbox 搬运 {entry.name} 失败：{exc}")
        if count:
            util.log(f"inbox → 下载目录：{count} 项")
        return count

    # ------------------------------------------------------------ 解压
    def _extract(self, local_dir: Path, result: dict) -> dict:
        local_dir = Path(local_dir)
        files = sum(1 for _ in local_dir.rglob("*") if _.is_file()) if local_dir.is_dir() else 0
        if not files:
            self.record("extract", False, f"下载目录是空的：{local_dir}")
            result["message"] = "没有下载到文件"
            return {"ok": False}
        chosen = result.get("chosen") or {}
        passwords = extract.gather_passwords(
            chosen, local_dir, self.cfg.get("extract.passwords") or [])
        if passwords:
            util.log(f"密码候选 {len(passwords)} 个：{', '.join(passwords[:5])}"
                     + (" ..." if len(passwords) > 5 else ""))
        ex = extract.extract_all(
            src=local_dir,
            passwords=passwords,
            cfg=dict(self.cfg.get("extract") or {}),
            work_root=local_dir,
            max_depth=int(self.cfg.get("extract.max_depth", 3)),
            timeout=float(self.cfg.get("extract.timeout", 3600)),
            delete_archives=bool(self.cfg.get("extract.delete_archives", False)),
        )
        data = ex.to_dict()
        ok = ex.ok or bool(ex.output_dirs)
        detail = (f"{len(ex.archives)} 个压缩包，成功 "
                  f"{sum(1 for a in ex.archives if a['ok'])} 个，输出 "
                  f"{util.human_size(util.dir_size(local_dir))}")
        self.record("extract", ok and not ex.errors,
                    detail + (f"｜失败 {len(ex.errors)}" if ex.errors else ""))
        for err in ex.errors[:5]:
            self.warn(f"解压问题：{err}")
        data["dirs"] = [str(p) for p in (ex.output_dirs or [local_dir])]
        return data

    # ------------------------------------------------------------ 入库
    def _organize(self, extract_res: dict, intent: intent_mod.Intent, result: dict) -> None:
        dirs = [Path(d) for d in extract_res.get("dirs") or []]
        if not dirs:
            self.record("organize", False, "没有可入库的目录")
            return
        for d in dirs:
            removed, _notes = organize.clean_junk(d, intent)
            if removed:
                util.log(f"清掉 {removed} 个垃圾文件/目录：{d.name}")
        org = organize.organize(
            dirs, intent, self.paths["library"],
            media_ext=self.cfg.get("organize.media_ext"),
            flatten=bool(self.cfg.get("organize.flatten", True)),
        )
        self.record("organize", bool(org.files),
                    f"入库 {org.files} 个文件 / {util.human_size(org.size)} → {org.target}")
        for note in org.notes[:5]:
            self.warn(note)
        if not org.files:
            result["message"] = "解压出来的文件都不是可阅读内容（检查是否解压失败）"
            return
        result["library"] = org.target
        result["files"] = org.files
        result["size"] = org.size
        result["extracted"] = len(dirs)
        result["passwords"] = sorted(set((extract_res.get("passwords_used") or {}).values()))
        self.state.add_asset(result["job_id"], "library", org.target, org.size,
                             f"{org.files} 个文件")

    # ------------------------------------------------------------ 收尾
    def _finish(self, job_id: str, result: dict, job: dict, intent: intent_mod.Intent) -> None:
        reports = self.paths["reports"] / str(result.get("slug") or job_id)
        # 用数据库里的最终状态重建候选表：深度复核/下载之后分数会变，报告要和 DB 一致
        rows = self._rows_for_report(job_id)
        if rows:
            result["candidates"] = rows
        report_path = report.write_report(
            reports / "report.md", job, intent, rows,
            result.get("chosen"), self.stages, {
                "library": result.get("library"), "files": result.get("files", 0),
                "size": result.get("size", 0), "extracted": result.get("extracted", 0),
                "passwords": result.get("passwords") or [],
                "warnings": self.warnings,
            })
        report.write_candidates_csv(rows, reports / "candidates.csv")
        report.write_json({"result": result, "stages": self.stages},
                          reports / "result.json")
        report.write_index(self.paths["reports"] / "library.html", self.paths["library"],
                           organize.library_manifest(self.paths["library"]))
        result["report"] = str(report_path)
        result["reports_dir"] = str(reports)
        result["warnings"] = self.warnings
        self.state.update_job(
            job_id, status="done" if result.get("ok") else "stopped",
            stage=(self.stages[-1]["stage"] if self.stages else ""),
            error="" if result.get("ok") else result.get("message", ""),
        )
        self.emit("done", result)

    def _rows_for_report(self, job_id: str) -> list[dict]:
        """从数据库读最终候选表（含深度复核后的分数），按分数排序。"""
        order = {"accept": 0, "review": 1, "reject": 2, "pending": 3}
        rows = []
        for row in self.state.list_candidates(job_id):
            rows.append({
                "score": row.get("score", 0), "verdict": row.get("verdict", ""),
                "cloud": row.get("cloud", ""), "title": row.get("title", ""),
                "password": row.get("password", ""), "url": row.get("url", ""),
                "size": row.get("size_bytes", 0), "file_count": row.get("file_count", 0),
                "reasons": row.get("reasons", []), "alive": row.get("alive", ""),
                "note": row.get("note", ""), "source": row.get("source", ""),
            })
        rows.sort(key=lambda r: (order.get(r["verdict"], 3), -float(r["score"] or 0)))
        return rows


def find_password(cand: dict) -> str:
    """从标题/备注里找提取码。"""
    text = f"{cand.get('title', '')} {cand.get('note', '')}"
    return pansou.extract_password_from_text(text)


def _slim(cand: dict) -> dict:
    keep = ("url", "cloud", "password", "title", "note", "source", "published",
            "alive", "verdict", "score", "reasons", "size", "file_count")
    return {k: cand.get(k) for k in keep if k in cand}
