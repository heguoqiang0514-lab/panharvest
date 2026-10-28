"""主题批量扫片：一个主题（如「考研数学一」）打到多个关键词上，汇总成一张分类清单。

普通 `run` 是"一句话 → 一个资源"；`batch` 是"一个主题 → 一批资源"。
两个关键优化：
1. 先把所有关键词的请求一次性打出去（PanSou 异步模式会立刻返回并在后台跑），
   等一会儿再统一取缓存，比"一个词一个词等"快好几倍；
2. 按书名/品牌分组，让「张宇1000题」「李永乐660题」各自成行，而不是一锅乱炖。
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from . import pansou, util

# 常见考研数学教辅的归类（按顺序匹配，命中即停）；不命中就是「其他」
GROUPS: list[tuple[str, str]] = [
    (r"张宇.*(1000|题源|千题)", "张宇 1000 题"),
    (r"(1000题|1000 题).*张宇|张宇1000", "张宇 1000 题"),
    (r"张宇.*(30讲|30 讲)", "张宇 基础30讲"),
    (r"张宇.*(36讲|36 讲)", "张宇 强化36讲"),
    (r"张宇.*(8套|八套)", "张宇 8 套卷"),
    (r"张宇.*(4套|四套)", "张宇 4 套卷"),
    (r"张宇", "张宇 其他"),
    (r"(复习全书|李永乐.*全书)", "李永乐 复习全书"),
    (r"660", "李永乐 660 题"),
    (r"李永乐.*(真题|历年)", "李永乐 历年真题"),
    (r"李永乐", "李永乐 其他"),
    (r"汤家凤.*1800|1800", "汤家凤 1800 题"),
    (r"汤家凤", "汤家凤 其他"),
    (r"武忠祥.*330|330", "武忠祥 330 题"),
    (r"武忠祥", "武忠祥 辅导讲义"),
    (r"李林.*(880|四套|六套|4套|6套)", "李林 880 / 6+4"),
    (r"李林", "李林 其他"),
    (r"(合工大|超越)", "合工大超越 5 套卷"),
    (r"(历年真题|真题解析|真题20|真题19)", "历年真题"),
    (r"(大纲|考试分析)", "大纲 / 考试分析"),
    (r"(基础|强化|冲刺).*(讲义|班|课)", "讲义 / 网课"),
    (r"(660|1800|1000|880|330|题源|习题)", "习题集（其他）"),
]


def classify(title: str) -> str:
    text = title or ""
    for pattern, label in GROUPS:
        if re.search(pattern, text, re.I):
            return label
    return "其他"


def group_candidates(cands: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    order = [label for _p, label in GROUPS] + ["其他"]
    for cand in cands:
        label = classify(cand.get("title", "") or cand.get("note", ""))
        groups.setdefault(label, []).append(cand)
    for items in groups.values():
        items.sort(key=lambda c: (-float(c.get("score") or 0), c.get("title", "")))
    return {label: groups[label] for label in order if label in groups}


def batch_search(
    ps: pansou.PanSou,
    queries: Iterable[str],
    clouds: Iterable[str] | None = None,
    max_candidates: int = 60,
    warm_wait: float = 12.0,
    rounds: int = 3,
) -> tuple[list[dict], list[str]]:
    """多关键词批量搜索（先并行预热，再统一收割）。"""
    wanted = {c for c in (clouds or []) if c in pansou.CLOUD_LABEL} or None
    queries = [q.strip() for q in queries if q and q.strip()]
    merged: dict[str, dict] = {}
    notes: list[str] = []

    # 第一轮：全部打出去，让 PanSou 在后台把插件跑起来
    for kw in queries:
        try:
            ps.search(kw, wanted)
        except Exception as exc:  # 预热失败不影响后面
            util.log(f"预热「{kw}」失败：{exc}", "WARN")
    util.log(f"已发出 {len(queries)} 个关键词，等 {warm_wait:.0f}s 让 PanSou 后台抓取")
    time.sleep(warm_wait)

    # 第二轮起：命中缓存
    pending = list(queries)
    for attempt in range(rounds):
        still: list[str] = []
        for kw in pending:
            try:
                data = ps.search(kw, wanted)
            except Exception as exc:
                notes.append(f"「{kw}」失败：{exc}")
                continue
            found = ps._extract(data, kw, wanted)
            new = 0
            for cand in found:
                key = pansou.url_key(cand["url"])
                if key in merged:
                    merged[key]["hits"] += 1
                    if not merged[key].get("password") and cand.get("password"):
                        merged[key]["password"] = cand["password"]
                    continue
                merged[key] = cand
                new += 1
            util.log(f"「{kw}」抓到 {len(found)} 条（新增 {new}，累计 {len(merged)}）")
            if not found:
                still.append(kw)
        if not still or len(merged) >= max_candidates:
            break
        pending = still
        util.log(f"还有 {len(still)} 个关键词没结果，再等一轮（{attempt + 2}/{rounds + 1}）")
        time.sleep(warm_wait)

    cands = list(merged.values())
    util.log(f"批量搜索完成：{len(cands)} 条候选，来自 {len(queries)} 个关键词")
    return cands[: max(max_candidates, 1)], notes
