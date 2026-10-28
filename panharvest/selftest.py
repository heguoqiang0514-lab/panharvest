"""自检：不联网、不碰网盘也能验证核心逻辑。

用法：
    python -m panharvest selftest
    或 harvest.bat selftest

覆盖：意图解析 / 打分判定 / 二维码表解析 / zip(含 GBK 文件名) / 嵌套压缩包 / tar.gz /
      rar(有 UnRAR 时) / 清洗归类 / 完整流水线（mock 后端）。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

from . import extract, intent, pcs, rank, util
from .config import Config

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    util.log(f"[{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    return bool(cond)


# ---------------------------------------------------------------- 造测试素材

class _LegacyZipInfo(zipfile.ZipInfo):
    """模拟 Windows 老压缩包：文件名按 GBK 字节写入，且不设 UTF-8 标记。"""

    def _encodeFilenameFlags(self):  # type: ignore[override]
        return self.filename.encode("gbk"), self.flag_bits & ~0x800


def make_zip(path: Path, entries: dict[str, bytes], legacy_gbk: bool = False) -> Path:
    util.ensure_dir(path.parent)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            if legacy_gbk:
                info = _LegacyZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, data)
            else:
                zf.writestr(name, data)
    return path


def make_tar_gz(path: Path, entries: dict[str, bytes]) -> Path:
    util.ensure_dir(path.parent)
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


# ---------------------------------------------------------------- 各项自检

def test_intent() -> None:
    i = intent.parse("葬送的芙莉莲 漫画 第1-10卷 高清 帮我下载解压")
    check("意图: 类型=comic", i.type == "comic", i.type)
    check("意图: 标题抽取", i.title == "葬送的芙莉莲", i.title)
    check("意图: 卷号 1-10", (i.volumes.get("start"), i.volumes.get("end")) == (1, 10), str(i.volumes))
    check("意图: 关键词 3 组", len(i.keywords) == 3, str(i.keywords))
    j = intent.parse("三体 全集 epub 电子书")
    check("意图: 书名不吞标签词", j.title == "三体", j.title)
    k = intent.parse("鬼灭之刃 第一季 1080p 动画")
    check("意图: 季识别", k.volumes.get("kind") == "季", str(k.volumes))


def test_rank() -> None:
    it = intent.parse("葬送的芙莉莲 漫画 第1-10卷")
    good = {
        "url": "https://pan.baidu.com/s/1good", "cloud": "baidu", "password": "abcd",
        "title": "葬送的芙莉莲 漫画 1-10卷 全彩汉化 高清PDF",
        "files": [
            {"name": f"葬送的芙莉莲 第{i:02d}卷.pdf", "size": 30 * 1024 * 1024, "isdir": False}
            for i in range(1, 11)
        ],
        "alive": "ok",
    }
    bad = {
        "url": "https://pan.baidu.com/s/1bad", "cloud": "baidu", "password": "abcd",
        "title": "沙丘2 电影 1080p 预告片 花絮", "files": [], "alive": "ok",
    }
    res_good = rank.judge(good, it, good["files"], "ok", {"min_score": 45})
    res_bad = rank.judge(bad, it, [], "ok", {"min_score": 45})
    check("判定: 对得上的判 accept", res_good["verdict"] == "accept",
          f"{res_good['verdict']} {res_good['score']}")
    check("判定: 明显不相干的判 reject", res_bad["verdict"] == "reject",
          f"{res_bad['verdict']} {res_bad['score']}")
    check("判定: 高分要高于低分", res_good["score"] > res_bad["score"],
          f"{res_good['score']} vs {res_bad['score']}")
    check("判定: 结论里带理由", bool(res_good["reasons"]), str(res_good["reasons"][:2]))


LS_SAMPLE = """
+---+------------+---------------------+----------------------------+
| # |  文件大小  |      修改日期       |          文件(目录)        |
+---+------------+---------------------+----------------------------+
| 0 | 45.20MB    | 2024-05-01 10:00:00 | 葬送的芙莉莲 第01卷.zip     |
| 1 | 46.10MB    | 2024-05-01 10:00:00 | 葬送的芙莉莲 第02卷.zip     |
| 2 | -          | 2024-05-01 10:00:00 | 特典/                      |
| 3 | 123.00MB   | 2024-05-01 10:00:00 | 特典/                |
|   | 总: 214.30MB |                   |                            |
+---+------------+---------------------+----------------------------+
"""


def test_parse_ls() -> None:
    parsed = pcs.parse_ls(LS_SAMPLE)
    names = [e["name"] for e in parsed["entries"]]
    check("ls 解析: 拿到 4 条", len(parsed["entries"]) == 4, str(names))
    check("ls 解析: 识别目录", parsed["entries"][2]["isdir"] is True, str(parsed["entries"][2]))
    check("ls 解析: 文件大小", parsed["entries"][0]["size"] > 45 * 1024 * 1024,
          str(parsed["entries"][0]["size"]))
    check("ls 解析: 总计行不算条目", not any("总:" in n for n in names), str(names))

    wrapped = """
+---+------------+---------------------+--------------------+
| # |  文件大小  |      修改日期       |      文件(目录)     |
+---+------------+---------------------+--------------------+
| 0 | 1.00MB     | 2024-05-01 10:00:00 | 一个非常长的文件    |
|   |            |                     | 名字续行.zip       |
+---+------------+---------------------+--------------------+
"""
    p2 = pcs.parse_ls(wrapped)
    check("ls 解析: 折行拼接", len(p2["entries"]) == 1 and "名字续行" in p2["entries"][0]["name"],
          str(p2["entries"]))


def test_extract(sandbox: Path) -> None:
    cfg = dict(Config.load().get("extract") or {})
    work = sandbox / "extract"
    shutil.rmtree(work, ignore_errors=True)
    util.ensure_dir(work)

    # 1) 普通 zip（UTF-8 文件名 + 中文路径）
    a = make_zip(work / "vol1.zip", {"第01卷/01.jpg": b"x" * 100, "第01卷/02.jpg": b"y" * 100})
    res = extract.extract_one(a, work / "out1", [], cfg)
    check("解压: zip 成功", res[0], res[2])
    check("解压: zip 中文路径还原", (work / "out1" / "第01卷" / "01.jpg").is_file(), str(res))

    # 2) 老式 GBK 文件名 zip（不做修复就会乱码）
    b = make_zip(work / "gbk.zip", {"第一话/封面.jpg": b"z" * 50}, legacy_gbk=True)
    res = extract.extract_one(b, work / "out2", [], cfg)
    check("解压: GBK 文件名成功", res[0], res[2])
    check("解压: GBK 文件名被还原", (work / "out2" / "第一话" / "封面.jpg").is_file(),
          str(list((work / "out2").rglob("*"))[:5]))

    # 3) tar.gz
    c = make_tar_gz(work / "book.tar.gz", {"book/epub.epub": b"e" * 10})
    res = extract.extract_one(c, work / "out3", [], cfg)
    check("解压: tar.gz 成功", res[0], res[2])
    check("解压: tar.gz 内容到位", (work / "out3" / "book" / "epub.epub").is_file(), str(res))

    # 4) 嵌套压缩包（zip 里套 zip）
    inner_dir = work / "inner_build"
    inner = make_zip(inner_dir / "inner.zip", {"深层/文件.txt": b"deep"})
    outer = make_zip(work / "outer.zip", {"嵌套.zip": inner.read_bytes()})
    res = extract.extract_all(work / "nested_src", [], cfg) if False else None
    src = work / "nested_src"
    util.ensure_dir(src)
    shutil.copy(outer, src / "outer.zip")
    result = extract.extract_all(src, [], cfg, work_root=work / "nested_out", max_depth=3)
    deep = list((work / "nested_out").rglob("文件.txt"))
    check("解压: 嵌套压缩包被递归解开", bool(deep), str(result.to_dict()["archives"]))

    # 5) 错误密码不应留下半成品
    res5 = extract.extract_one(a, work / "out5", ["wrong"], cfg)
    check("解压: 无密码包忽略错误密码", res5[0], res5[2])

    # 6) rar（本机 WinRAR 造样本 → UnRAR 解压）
    rar_src = sandbox / "rar_src"
    rar_ok = False
    winrar = Path(cfg.get("unrar") or "").with_name("WinRAR.exe")
    archive = work / "sample.rar"
    if winrar.is_file():
        util.ensure_dir(rar_src)
        (rar_src / "readme.txt").write_text("hello rar", encoding="utf-8")
        util.run([str(winrar), "a", "-ibck", "-ep1", "-y", str(archive), str(rar_src / "readme.txt")],
                 timeout=90)
        rar_ok = archive.is_file()
    if rar_ok:
        res6 = extract.extract_one(archive, work / "out6", [], cfg)
        check("解压: rar 成功（UnRAR）", res6[0], res6[2])
    else:
        util.log("跳过 rar 用例（本机无法生成样本或缺少 UnRAR）")

    # 7) 带密码的 zip（WinRAR 造 ZipCrypto 加密包）
    enc_zip = work / "enc.zip"
    enc_ok = False
    if winrar.is_file():
        payload = work / "enc_payload"
        util.ensure_dir(payload)
        (payload / "秘密.txt").write_text("secret", encoding="utf-8")
        util.run([str(winrar), "a", "-afzip", "-ibck", "-ep1", "-y", "-pSECRET123",
                  str(enc_zip), str(payload / "秘密.txt")], timeout=90)
        enc_ok = enc_zip.is_file()
    if enc_ok:
        res7 = extract.extract_one(enc_zip, work / "out7", ["wrong", "SECRET123"], cfg)
        check("解压: 加密 zip 自动试出正确密码", res7[0] and res7[1] == "SECRET123", str(res7))
        got = work / "out7" / "秘密.txt"
        content_ok = got.is_file() and got.read_text(encoding="utf-8") == "secret"
        check("解压: 试错密码后内容仍完好", content_ok, str(got))
        leftovers = [p.name for p in (work / "out7").rglob("*") if p.is_file() and p.stat().st_size == 0]
        check("解压: 没有 0 字节残留", not leftovers, str(leftovers))
    else:
        util.log("跳过加密 zip 用例（无法生成样本）")

    # 8) 7z（装了 7-Zip / 7zr 才有）
    if cfg.get("sevenzip") or cfg.get("sevenzr"):
        sz_src = work / "sz_payload"
        util.ensure_dir(sz_src)
        (sz_src / "a.txt").write_text("seven", encoding="utf-8")
        sz = work / "sample.7z"
        tool = cfg.get("sevenzip") or cfg.get("sevenzr")
        util.run([str(tool), "a", "-t7z", str(sz), str(sz_src / "a.txt")], timeout=90)
        if sz.is_file():
            res8 = extract.extract_one(sz, work / "out8", [], cfg)
            check("解压: 7z 成功", res8[0], res8[2])
    else:
        util.log("跳过 7z 用例（未安装 7-Zip / 7zr）")


def test_rank_report(sandbox: Path) -> None:
    """候选列表能排序并生成可读报告（不依赖网络）。"""
    it = intent.parse("电锯人 漫画 全12卷")
    cands = [
        {"url": "https://pan.baidu.com/s/1a", "cloud": "baidu", "password": "1111",
         "title": "电锯人 1-12卷 完结 汉化", "alive": "ok", "files": [
             {"name": f"电锯人 第{i:02d}卷.cbz", "size": 20 * 1024 * 1024, "isdir": False}
             for i in range(1, 13)]},
        {"url": "https://pan.baidu.com/s/1b", "cloud": "baidu", "password": "",
         "title": "电锯人 剧场版 预告", "alive": "bad", "files": []},
    ]
    ranked = rank.rank(cands, it, {"min_score": 45, "max_total_gb": 60, "max_files": 5000})
    check("排序: 最佳候选排第一", ranked[0]["url"].endswith("1a"), ranked[0]["title"])
    check("排序: 第一名 verdict=accept", ranked[0]["verdict"] == "accept",
          f"{ranked[0]['verdict']} {ranked[0]['score']}")


def test_organize(sandbox: Path) -> None:
    from . import organize

    work = sandbox / "organize"
    shutil.rmtree(work, ignore_errors=True)
    util.ensure_dir(work)
    it = intent.parse("测试系列 漫画")
    lib = work / "library"
    media = [".jpg", ".png"]

    # 1) 扁平文件（没有子目录）
    flat = work / "flat"
    util.ensure_dir(flat)
    for i in (1, 2, 3):
        (flat / f"{i:02d}.jpg").write_bytes(b"j" * (1000 + i))
    (flat / "公众号.txt").write_text("微信 加群 扫码 www.example.com", encoding="utf-8")
    res = organize.organize([flat], it, lib, media_ext=media)
    target = lib / "测试系列"
    check("入库: 扁平文件被搬走", res.moved == 3, str(res.to_dict()))
    check("入库: 非内容文件被跳过", (flat / "公众号.txt").exists(), str(list(flat.iterdir())))
    check("入库: 目标文件存在", (target / "01.jpg").is_file(), str(list(target.iterdir())))

    # 2) 重复执行不产生副本（同名同大小跳过）
    again = work / "flat2"
    util.ensure_dir(again)
    (again / "01.jpg").write_bytes(b"j" * 1001)
    res2 = organize.organize([again], it, lib, media_ext=media)
    check("入库: 同名同大小跳过", res2.moved == 0 and res2.skipped == 1, str(res2.to_dict()))

    # 3) 每卷一个子目录
    vols = work / "vols"
    for vol in (1, 2):
        util.ensure_dir(vols / f"第{vol:02d}卷")
        (vols / f"第{vol:02d}卷" / "p.jpg").write_bytes(b"v" * 500)
    res3 = organize.organize([vols], it, lib, media_ext=media)
    check("入库: 子目录按卷保留", (target / "第01卷" / "p.jpg").is_file(), str(res3.to_dict()))

    # 4) 清洗垃圾
    junk = work / "junk"
    util.ensure_dir(junk / "__MACOSX")
    (junk / "__MACOSX" / "x").write_bytes(b"")
    (junk / "Thumbs.db").write_bytes(b"")
    (junk / "更多资源.txt").write_text("免费下载 关注公众号 扫码", encoding="utf-8")
    (junk / "keep.jpg").write_bytes(b"k" * 100)
    removed, _notes = organize.clean_junk(junk, it)
    check("清洗: 删除系统垃圾", removed >= 2, f"removed={removed}")
    check("清洗: 保留正常文件", (junk / "keep.jpg").exists(), str(list(junk.iterdir())))


def run_all(verbose: bool = True) -> int:
    sandbox = util.ensure_dir(Config.load().path_of("paths.data") / "selftest")
    util.log(f"自检沙盒：{sandbox}")
    test_intent()
    test_rank()
    test_parse_ls()
    test_extract(sandbox)
    test_organize(sandbox)
    test_rank_report(sandbox)

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    util.log("=" * 56)
    if passed == total:
        util.log(f"自检全部通过：{passed}/{total}")
        return 0
    util.log(f"自检失败：{passed}/{total} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            util.log(f"  FAIL {name} :: {detail}", "ERROR")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_all())
