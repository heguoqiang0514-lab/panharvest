"""候选判定：这堆链接里哪一条真的对得上用户要的东西。

打分只用两类输入，任何一类缺失都能降级工作：
- 文本（标题 / 备注 / 分享来源）
- 文件清单（转让后的真实目录，或探测到的 file_list）

输出 verdict：
- accept  可以自动下载
- review  边界情况，列出理由交给用户点确认
- reject  明显不对/失效/超限，不再尝试
"""
from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath
from typing import Any

from .intent import Intent

ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".001", ".cbz", ".cbr"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".tif", ".tiff"}
BOOK_EXT = {".pdf", ".epub", ".mobi", ".azw3", ".azw", ".txt", ".djvu", ".cbz", ".cbr"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".rmvb", ".m2ts", ".mov"}

NEGATIVE_WORDS = ["预告", "花絮", "解说", "reaction", "枪版", "tc版", "试看", "片段",
                  "广告", "招聘", "兼职", "教程", "课程", "直播回放"]
NEGATIVE_PENALTY = 30


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\s\-_·\.\,\:\;\(\)\[\]【】（）《》]+", "", text)
    return text


def similarity(a: str, b: str) -> float:
    """0~1 的文本相似度，优先判包含关系。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        return min(0.98, 0.72 + 0.28 * len(short) / max(len(long_), 1))
    return difflib.SequenceMatcher(None, na, nb).ratio()


def analyze_files(items: list[dict] | None) -> dict:
    """对文件清单做统计，给打分和报告共用。"""
    items = items or []
    files = [f for f in items if not f.get("isdir")]
    total = sum(int(f.get("size") or 0) for f in files)
    hist: dict[str, int] = {}
    for f in files:
        ext = PurePosixPath(f.get("name", "")).suffix.lower()
        hist[ext] = hist.get(ext, 0) + 1
    vols: set[int] = set()
    for f in items:
        name = f.get("name", "")
        for pat in (r"第\s*(\d{1,3})\s*[卷册话集]", r"[Vv]ol\.?\s*(\d{1,3})",
                    r"(?:^|[\s_\-(])(\d{2,3})(?=[\s_\-.)]|$)"):
            m = re.search(pat, name)
            if m:
                vols.add(int(m.group(1)))
                break
    imgs = sum(1 for f in files if PurePosixPath(f.get("name", "")).suffix.lower() in IMAGE_EXT)
    return {
        "file_count": len(files),
        "dir_count": sum(1 for f in items if f.get("isdir")),
        "total_size": total,
        "ext_hist": dict(sorted(hist.items(), key=lambda kv: -kv[1])),
        "image_ratio": (imgs / len(files)) if files else 0.0,
        "volumes": sorted(vols),
        "has_archive": any(e in ARCHIVE_EXT for e in hist),
        "top_names": [f.get("name", "") for f in items[:15]],
    }


def _type_fit(intent: Intent, analysis: dict) -> tuple[float, str]:
    """扩展名分布是否与期望类型一致。返回 (0~1, 说明)。"""
    hist = analysis.get("ext_hist") or {}
    total = sum(hist.values())
    if not total:
        return 0.5, "无文件清单，类型无法核对"
    expect = set(intent.expect_ext)
    hit = sum(n for ext, n in hist.items() if ext in expect)
    ratio = hit / total
    top = max(hist, key=hist.get) if hist else ""
    return ratio, f"文件类型匹配 {hit}/{total}（主要是 {top or '未知'}）"


def _volume_fit(intent: Intent, analysis: dict) -> tuple[float, str]:
    want = intent.volumes
    found = analysis.get("volumes") or []
    if not want:
        return 0.5, ""
    if not found:
        return 0.5, f"未见卷号（目标 {want.get('label')}）"
    start, end = int(want.get("start", 0)), int(want.get("end", 0))
    covered = [v for v in found if start <= v <= end]
    need = max(1, end - start + 1)
    ratio = min(1.0, len(set(covered)) / need)
    if ratio >= 0.9:
        return 1.0, f"卷号覆盖 {len(set(covered))}/{need}"
    if ratio > 0:
        return 0.6, f"只覆盖 {len(set(covered))}/{need} 卷"
    return 0.0, f"卷号不匹配（资源里是 {found[:5]}，要的是 {want.get('label')}）"


def judge(
    cand: dict,
    intent: Intent,
    files: list[dict] | None = None,
    alive: str | None = None,
    limits: dict | None = None,
) -> dict:
    """给单个候选打分并给出 verdict。"""
    limits = limits or {}
    max_size = float(limits.get("max_total_gb", 60)) * (1024 ** 3)
    max_files = int(limits.get("max_files", 5000))
    min_score = float(limits.get("min_score", 45))

    text = " ".join(str(cand.get(k, "")) for k in ("title", "note", "keyword"))
    analysis = analyze_files(files)
    reasons: list[str] = []
    score = 0.0
    fatal: list[str] = []

    # ---- 1) 标题相似度（最重要，45 分）
    # 分别跟标题/备注/原搜索词比，取最高分，避免"标题+备注"拼接后互相稀释
    fields = [str(cand.get(k) or "") for k in ("title", "note", "keyword")]
    sim = 0.5
    if intent.title:
        sim = max([similarity(intent.title, t) for t in fields if t] or [0.0])
    score += 45 * min(1.0, sim / 0.85)
    if sim >= 0.85:
        reasons.append(f"标题高度吻合（{sim:.0%}）：{cand.get('title', '')[:40]}")
    elif sim >= 0.5:
        reasons.append(f"标题部分吻合（{sim:.0%}）：{cand.get('title', '')[:40]}")
    else:
        reasons.append(f"标题不像（{sim:.0%}）：{cand.get('title', '')[:40]}")

    # ---- 2) 类型匹配（15 分）
    type_fit, type_note = _type_fit(intent, analysis)
    score += 15 * type_fit
    if type_note:
        reasons.append(type_note)

    # ---- 3) 卷号覆盖（15 分）
    vol_fit, vol_note = _volume_fit(intent, analysis)
    score += 15 * vol_fit
    if vol_note:
        reasons.append(vol_note)

    # ---- 4) 存活状态（15 分）
    state = (alive or "unknown").lower()
    if state in ("ok", "alive", "true"):
        score += 15
        reasons.append("链接检测：有效")
    elif state in ("locked",):
        score += 8
        reasons.append("链接检测：需要提取码")
    elif state in ("bad", "dead", "false"):
        reasons.append("链接检测：已失效")
        fatal.append("链接已失效")
    else:
        score += 7
        reasons.append("链接检测：未确认（跳过该信号）")

    # ---- 5) 体积/文件数（10 分）
    total_size = analysis.get("total_size") or 0
    file_count = analysis.get("file_count") or 0
    if total_size:
        if total_size > max_size:
            fatal.append(f"体积 {total_size / 1024 ** 3:.1f}GB 超过上限 {max_size / 1024 ** 3:.0f}GB")
            reasons.append(f"体积超限：{total_size / 1024 ** 3:.1f}GB")
        else:
            score += 10
            reasons.append(f"体积 {total_size / 1024 ** 3:.2f}GB，在限额内")
    else:
        score += 5
    if file_count and file_count > max_files:
        fatal.append(f"文件数 {file_count} 超过上限 {max_files}")
    if analysis.get("has_archive"):
        reasons.append("含压缩包，可直接解压")

    # ---- 6) 负向词（扣分）
    hits = [w for w in NEGATIVE_WORDS if w in text.lower()]
    if hits:
        score -= NEGATIVE_PENALTY
        reasons.append(f"标题含可疑词：{'/'.join(hits[:3])}")

    # ---- 7) 提取码缺失（能不能转存的硬条件）
    if cand.get("cloud") == "baidu" and not cand.get("password"):
        reasons.append("没有提取码，可能无法转存")
        score -= 8

    score = max(0.0, min(100.0, score))
    if fatal:
        verdict = "reject"
    elif score >= max(70.0, min_score):
        verdict = "accept"
    elif score >= min_score:
        verdict = "review"
    else:
        verdict = "reject"
    reasons = fatal + reasons
    return {
        "score": round(score, 1),
        "verdict": verdict,
        "reasons": reasons,
        "size": total_size,
        "file_count": file_count,
        "analysis": analysis,
    }


def rank(cands: list[dict], intent: Intent, limits: dict) -> list[dict]:
    """对候选排序，写回 score/verdict/reasons 字段。"""
    for cand in cands:
        result = judge(
            cand, intent,
            files=cand.get("files"),
            alive=cand.get("alive"),
            limits=limits,
        )
        cand.update({
            "score": result["score"],
            "verdict": result["verdict"],
            "reasons": result["reasons"],
            "size": result["size"],
            "file_count": result["file_count"],
            "analysis": result["analysis"],
        })
    order = {"accept": 0, "review": 1, "reject": 2}
    cands.sort(key=lambda c: (order.get(c.get("verdict", "reject"), 3), -c.get("score", 0)))
    return cands


def deep_verdict(files: list[dict], intent: Intent, limits: dict,
                 cand: dict) -> dict:
    """拿到真实文件清单之后的复核（转存后、下载前）。"""
    return judge(cand, intent, files=files, alive=cand.get("alive") or "ok", limits=limits)
