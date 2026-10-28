"""把一句人话 prompt 变成结构化意图 + 一组搜索关键词变体。

「葬送的芙莉莲 漫画 第1-10卷 高清 帮我下载解压」
  -> type=comic, title=葬送的芙莉莲, volumes=1-10,
     keywords=['葬送的芙莉莲', '葬送的芙莉莲 漫画', '葬送的芙莉莲 第1-10卷']

这里只做确定性启发式，不引入任何模型依赖：可预测、可测试、可解释。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 类型词表

TYPE_WORDS: dict[str, list[str]] = {
    "comic": ["漫画", "单行本", "画集", "设定集", "同人", "本子", "全彩", "汉化",
              "manga", "comic", "彩漫", "条漫", "日漫", "韩漫"],
    "book": ["小说", "电子书", "书", "文集", "作品集", "epub", "pdf", "mobi",
             "azw3", "txt", "轻小说", "文学", "专著", "教材", "古籍"],
    "video": ["电影", "电视剧", "剧集", "番剧", "动画", "动漫", "纪录片", "综艺",
              "1080p", "2160p", "4k", "蓝光", "hd", "bluray", "美剧", "日剧", "韩剧"],
    "audio": ["音乐", "无损", "flac", "mp3", "有声", "广播剧", "音频"],
}

# 期望的文件扩展名（用于判断"内容是否对得上"）
TYPE_EXPECT: dict[str, list[str]] = {
    "comic": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif",
              ".cbz", ".cbr", ".pdf", ".zip", ".7z", ".rar"],
    "book": [".pdf", ".epub", ".mobi", ".azw3", ".azw", ".txt", ".doc", ".docx",
             ".zip", ".7z", ".rar", ".cbz"],
    "video": [".mp4", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".rmvb", ".m2ts"],
    "audio": [".flac", ".mp3", ".m4a", ".wav", ".ape", ".aac", ".ogg"],
    "unknown": [".pdf", ".epub", ".jpg", ".png", ".zip", ".7z", ".rar",
                ".mp4", ".mkv", ".txt", ".cbz", ".cbr", ".mobi", ".azw3"],
}

# 说话时的噪音，去掉后剩下来的才是片名
NOISE_PATTERNS = [
    r"帮我", r"请帮我", r"麻烦", r"我想看", r"我要看", r"想看", r"给我", r"替我",
    r"找一下", r"找找", r"搜一下", r"搜索", r"检索", r"下载", r"转存", r"解压",
    r"自动", r"一键", r"整理", r"入库", r"的资源", r"资源", r"谢谢", r"感谢",
    r"有没有", r"求", r"来个", r"一份", r"全套", r"高清版", r"高清", r"超清",
    r"无水印", r"全集吗", r"可以吗", r"吗", r"呀", r"吧", r"呢",
]

# 与片名无关的标签词：算进搜索词，但绝不能当成片名
TAG_WORDS = [
    "全集", "合集", "全本", "完本", "打包", "整包", "珍藏", "精校", "完整版",
    "典藏", "中文", "简体", "繁体", "无删减", "未删减", "全彩版", "单行本",
    "完结", "连载", "精编", "重制", "修复版", "蓝光版", "重压", "收藏版",
    "1080p", "2160p", "720p", "4k", "8k", "hd", "hdr", "bluray", "web-dl",
    "中字", "双语", "国语", "日语", "粤语", "内嵌", "外挂",
]

VOLUME_UNITS = "卷册集话期季"

# 顺序即优先级：区间 > 全N卷 > 第N卷 > Vol > Sxx
_P_RANGE = re.compile(rf"第?\s*(\d{{1,4}})\s*[-~—到至]\s*(\d{{1,4}})\s*([{VOLUME_UNITS}])")
_P_ALL = re.compile(rf"全\s*(\d{{1,3}})\s*([卷册集])")
_P_SINGLE = re.compile(rf"第\s*(\d{{1,4}})\s*([{VOLUME_UNITS}])")
_P_VOL = re.compile(r"(?:vol\.?|v)\s*(\d{1,4})\s*[-~]\s*(\d{1,4})", re.I)
_P_SEASON = re.compile(r"\bs(\d{1,2})\s*(?:[-~]\s*s?(\d{1,2}))?\b", re.I)

# extract_title 里用来"从标题中抹掉卷号"的表达式
VOLUME_STRIP = [_P_RANGE, _P_ALL, _P_SINGLE, _P_VOL, _P_SEASON]


def _unit_kind(unit: str) -> str:
    if unit in "卷册集":
        return "卷"
    if unit in "话期":
        return "话"
    return "季"


@dataclass
class Intent:
    prompt: str
    title: str = ""
    type: str = "unknown"
    volumes: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    expect_ext: list[str] = field(default_factory=list)
    must_words: list[str] = field(default_factory=list)
    avoid_words: list[str] = field(default_factory=list)
    year: str = ""

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "title": self.title,
            "type": self.type,
            "volumes": self.volumes,
            "keywords": self.keywords,
            "expect_ext": self.expect_ext,
            "must_words": self.must_words,
            "avoid_words": self.avoid_words,
            "year": self.year,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Intent":
        obj = cls(prompt=data.get("prompt", ""))
        for key in ("title", "type", "volumes", "keywords", "expect_ext",
                    "must_words", "avoid_words", "year"):
            if key in data:
                setattr(obj, key, data[key])
        return obj

    def summary(self) -> str:
        bits = [f"类型={self.type}", f"标题={self.title or '?'}"]
        if self.volumes:
            bits.append(f"卷={self.volumes.get('label', '')}")
        bits.append(f"关键词={len(self.keywords)} 组")
        return " / ".join(bits)


def detect_type(text: str) -> str:
    low = text.lower()
    scores = {t: 0 for t in TYPE_WORDS}
    for typ, words in TYPE_WORDS.items():
        for w in words:
            if w.lower() in low:
                scores[typ] += 1
    # 漫画里常出现"全集"，别让"书"把漫画盖掉：按命中数取最大，平票时按固定优先级
    best = max(scores.items(), key=lambda kv: (kv[1], -list(TYPE_WORDS).index(kv[0])))
    return best[0] if best[1] > 0 else "unknown"


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = r"[一二三四五六七八九十]{1,3}"


def _cn_to_int(text: str) -> int | None:
    """一 / 十二 / 二十 / 二十三 -> 1 / 12 / 20 / 23。"""
    if not text:
        return None
    if all(ch in _CN_DIGITS or ch == "十" for ch in text):
        if len(text) == 1:
            return 10 if text == "十" else _CN_DIGITS.get(text)
        if text[0] == "十":
            return 10 + (_CN_DIGITS.get(text[1]) or 0 if len(text) > 1 else 0)
        if len(text) == 2 and text[1] == "十":
            return _CN_DIGITS[text[0]] * 10
        if len(text) == 3 and text[1] == "十":
            return _CN_DIGITS[text[0]] * 10 + (_CN_DIGITS.get(text[2]) or 0)
    return None


def _normalize_cn_numbers(text: str) -> str:
    """把"第十卷""全十二卷""一至二十卷"里的中文数字换成阿拉伯数字。"""
    unit = r"[卷册集话期季]"

    def sub(pattern: str, repl) -> None:
        nonlocal text
        text = re.sub(pattern, repl, text)

    sub(rf"[第]\s*({_CN_UNIT})\s*(?={unit})",
        lambda m: f"第{_cn_to_int(m.group(1)) if _cn_to_int(m.group(1)) is not None else m.group(1)}")
    sub(rf"全\s*({_CN_UNIT})\s*(?={unit})",
        lambda m: f"全{_cn_to_int(m.group(1)) if _cn_to_int(m.group(1)) is not None else m.group(1)}")
    sub(rf"({_CN_UNIT})\s*[-~—到至]\s*({_CN_UNIT})\s*(?={unit})",
        lambda m: (f"{_cn_to_int(m.group(1)) if _cn_to_int(m.group(1)) is not None else m.group(1)}"
                   f"-{_cn_to_int(m.group(2)) if _cn_to_int(m.group(2)) is not None else m.group(2)}"))
    return text


def parse_volumes(text: str) -> dict[str, Any]:
    text = _normalize_cn_numbers(text)

    def build(kind: str, start: int, end: int, raw: str) -> dict[str, Any]:
        if end < start:
            start, end = end, start
        if kind == "季":
            label = f"第{start}季" if end == start else f"第{start}-{end}季"
        else:
            label = f"{kind}{start}" if end == start else f"{kind}{start}-{end}"
        return {
            "kind": kind, "start": start, "end": end, "count": end - start + 1,
            "raw": raw, "label": label,
        }

    m = _P_RANGE.search(text)
    if m:
        return build(_unit_kind(m.group(3)), int(m.group(1)), int(m.group(2)), m.group(0))
    m = _P_ALL.search(text)
    if m:
        return build(_unit_kind(m.group(2)), 1, int(m.group(1)), m.group(0))
    m = _P_SINGLE.search(text)
    if m:
        return build(_unit_kind(m.group(2)), int(m.group(1)), int(m.group(1)), m.group(0))
    m = _P_VOL.search(text)
    if m:
        return build("卷", int(m.group(1)), int(m.group(2)), m.group(0))
    m = _P_SEASON.search(text)
    if m:
        end = int(m.group(2)) if m.group(2) else int(m.group(1))
        return build("季", int(m.group(1)), end, m.group(0))
    return {}


def _tag_vocabulary() -> set[str]:
    words: set[str] = set(TAG_WORDS)
    for group in TYPE_WORDS.values():
        words.update(group)
    return {w.lower() for w in words}


_TAGS = _tag_vocabulary()


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def extract_title(prompt: str, volumes: dict[str, Any]) -> str:
    text = _normalize_cn_numbers(prompt.strip())
    for pattern in VOLUME_STRIP:
        text = pattern.sub(" ", text)
    for pat in NOISE_PATTERNS:
        text = re.sub(pat, " ", text)
    # 去掉书名号/引号/括号等装饰，但保留括号里的正规信息（如作者）
    text = re.sub(r"[《》〈〉「」『』【】\[\]\(\)（）]", " ", text)
    text = re.sub(r"[，,。.、;；:：!！?？~～|/\\+*#@$%^&_=]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = [p for p in text.split(" ") if p]
    if not parts:
        return ""
    # 1) 先剔除纯数字/纯符号的碎片
    cleaned = [p for p in parts if not re.fullmatch(r"[\d\s.\-]+", p)] or parts
    # 2) 剔掉类型词与标签词（epub / 漫画 / 1080p / 全集 …），它们只是搜索标签
    core = [p for p in cleaned if p.lower() not in _TAGS]
    if not core:
        core = cleaned
    # 汉字多的优先（"考研数学一" 要赢过 "GitHub"），再比长度
    title = max(core, key=lambda p: (_cjk_count(p), len(p))) if len(core) > 1 else core[0]
    # 若最长段过短，退回整体
    if len(title) <= 2 and "".join(core) != title:
        title = "".join(core)
    return title.strip()


def build_keywords(title: str, typ: str, volumes: dict[str, Any], limit: int = 3) -> list[str]:
    out: list[str] = []
    if title:
        out.append(title)
    type_word = {"comic": "漫画", "book": "小说", "video": "", "audio": "", "unknown": ""}[typ]
    if title and type_word:
        out.append(f"{title} {type_word}")
    if title and volumes.get("label"):
        out.append(f"{title} {volumes['label']}")
    if title and not volumes and typ == "comic":
        out.append(f"{title} 全集")
    seen: set[str] = set()
    uniq = []
    for kw in out:
        if kw and kw not in seen:
            seen.add(kw)
            uniq.append(kw)
    return uniq[: max(1, limit)]


def parse(prompt: str, variants: int = 3) -> Intent:
    prompt = (prompt or "").strip()
    typ = detect_type(prompt)
    volumes = parse_volumes(prompt)
    # 只说了"第 N 卷/全 N 卷"也能判断是漫画，说了"第 N 季"就是影视
    if typ == "unknown" and volumes:
        typ = "comic" if volumes.get("kind") == "卷" else "video"
    title = extract_title(prompt, volumes)
    year_match = re.search(r"(19|20)\d{2}", prompt)
    expect = list(TYPE_EXPECT.get(typ, TYPE_EXPECT["unknown"]))

    must: list[str] = []
    avoid: list[str] = []
    if typ == "comic":
        must = ["第", "卷", "话", "册"]  # 弱提示，仅参与打分
        avoid = ["预告", "花絮", "解说", "reaction"]
    elif typ == "video":
        avoid = ["预告", "花絮", "解说", "reaction", "枪版", "tc版"]
    elif typ == "book":
        avoid = ["漫画版", "影视"]

    intent = Intent(
        prompt=prompt,
        title=title,
        type=typ,
        volumes=volumes,
        keywords=build_keywords(title, typ, volumes, variants),
        expect_ext=expect,
        must_words=must,
        avoid_words=avoid,
        year=year_match.group(0) if year_match else "",
    )
    return intent


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    for sample in sys.argv[1:] or [
        "葬送的芙莉莲 漫画 第1-10卷 高清 帮我下载解压",
        "三体 全集 epub 电子书",
        "《间谍过家家》第9卷 汉化",
        "想看电影 沙丘2 1080p",
    ]:
        print(sample, "->", json.dumps(parse(sample).to_dict(), ensure_ascii=False))
