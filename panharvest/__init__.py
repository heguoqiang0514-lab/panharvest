"""网盘资源收割机（panharvest）。

一句话 → 找网盘链接 → 校验是否对得上 → 转存下载 → 解压 → 清洗入库 → 出报告。

模块分工：
    intent     把 prompt 解析成结构化需求（类型 / 片名 / 卷号 / 关键词变体）
    pansou     PanSou 搜索 + 官方链接存活检测
    baidu      百度分享页深度探测（文件清单、是否需提取码）
    rank       相关性打分与 accept/review/reject 判定
    pcs        BaiduPCS-Go 封装（转存 → ls → 下载）
    extract    解压引擎（zip/7z/rar/tar、密码字典、嵌套、分卷）
    organize   清洗广告文件、扁平化、去重、入库
    report     markdown 报告 / 候选 CSV / 人工转存引导页 / 书库索引
    pipeline   串起全部阶段，可中断续跑
    server     本地网页界面
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
