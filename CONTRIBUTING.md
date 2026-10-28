# 参与开发

先说三条硬规矩，剩下的都好说。

1. **零第三方依赖**：只用 Python 标准库。加一个 `requirements.txt` 就失去了这个项目最好的属性。
2. **隐私零容忍**：`data/`、`library/`、`reports/`、`config.json`、`.private-vault/` 一律不进仓库。
   提交前跑一次 `git status --porcelain | Select-String 'data/|library/|\.db|profile|config\.json'`，输出必须为空。
3. **判断要能解释**：任何打分/筛选逻辑都必须产出人能读的理由（`reasons`），因为报告要把理由给用户看。

---

## 本地跑起来

```powershell
harvest.bat selftest          # 37 项本地逻辑自检，不联网、不需要账号
python tests/test_e2e.py      # 端到端：假 PanSou + 假 PCS + 真压缩包
python tests/test_web.py 8902 # 网页界面（自带假 PanSou）
```

三个用例都**不依赖网络和账号**，这是本项目测试策略的核心：外部服务永远是假的，压缩包永远是真的。

## 代码地图

| 文件 | 职责 | 改动风险 |
|---|---|---|
| `intent.py` | 一句话 → 类型/标题/卷号/搜索词（纯启发式） | 改完必须更新 `selftest.test_intent` |
| `pansou.py` | 聚合搜索 + 链接存活检测 | 字段容错要保留 |
| `rank.py` | 六个信号加权打分 → accept/review/reject | **最值钱的逻辑**，改权重请说明理由 |
| `pcs.py` / `quark_dl.py` | 转存 + 下载后端 | 见下「加网盘」 |
| `extract.py` | 解压引擎（标准库优先，7-Zip 兜底） | 新增格式要加自检 |
| `organize.py` | 清洗、去重、入库 | — |
| `pipeline.py` | 编排 + 中断续跑 | 每个阶段必须写 job 记录 |
| `server.py` | 本地 Web 面板（单文件内嵌 HTML） | 不引入前端构建链 |

## 加一个网盘后端

照 `quark_dl.py` 的形状写四步即可挂上去：

```
save   把分享里的文件转存到自己的网盘
task   轮询转存结果，拿到 fid
link   换出带签名的直链
get    带 Cookie + Referer 下载（支持断点）
```

然后在 `pipeline.Pipeline.backend()` 里加分支。要求：失败要抛可读的错误，不要静默降级。

## 改判定阈值

`rank.judge()` 里六个信号：标题 45 / 类型 15 / 卷号 15 / 存活 15 / 体积 10，负向词 −30，无提取码 −8。
阈值在 `limits.min_score`（默认 45）与 `accept ≥ 70`。影视类的争议最大，欢迎带数据来讨论。

## 提交前

```powershell
harvest.bat selftest && python tests/test_e2e.py
git status --porcelain          # 确认没有 data/ library/ config.json
```

PR 里请写：**你观察到的现象 → 你怎么判断的 → 你怎么验证的**。三句话就够。
