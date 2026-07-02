---
name: ai-news-pipeline
description: |
  AI 资讯自动化流水线 skill。把"采集 → 信息处理 → 输出 article → 调用 wechat-publisher 推送微信公众号"四步串成一条全自动链路。从 HackerNews、RSS（含 arXiv / X）多源采集资讯，启发式 + LLM 三关评分筛选排序，生成卡片式日报或深度文章 brief，最终调用 wechat-publisher skill 推送到微信公众号草稿箱。

  本 skill 自包含全部 Python 源码（采集器、评分器、日报生成器、发布器），可独立于任何项目安装使用。

  ⚠️ 硬依赖：推送阶段（阶段四）必须先安装 wechat-publisher skill，否则 publish 命令不可用。采集/处理/输出（阶段一至三）可独立使用。

  触发场景（只要沾边就该使用本 skill）：
  - 用户提到"采集"、"抓取资讯"、"采集资讯"、"抓新闻"等关键词
  - 用户提到"资讯日报"、"每日日报"、"AI 日报"、"digest"、"卡片日报"
  - 用户要求筛选 / 评分 / 排序 / 去重资讯
  - 用户要求"跑流水线"、"跑日报"、"run daily"、"今日资讯"
  - 用户提到"推送到公众号"、"发草稿"、"发布日报"
  - 用户要求把采集到的资讯生成文章并发布
  - 用户要求规划选题 / 生成 brief
dependencies:
  - name: wechat-publisher
    required_for: publish (阶段四)
    install: npx skills add jiji262/wechat-publisher
    note: 仅推送阶段需要；采集/处理/输出可不装

  触发场景（只要沾边就该使用本 skill）：
  - 用户提到"采集"、"抓取资讯"、"采集资讯"、"抓新闻"等关键词
  - 用户提到"资讯日报"、"每日日报"、"AI 日报"、"digest"、"卡片日报"
  - 用户要求筛选 / 评分 / 排序 / 去重资讯
  - 用户要求"跑流水线"、"跑日报"、"run daily"、"今日资讯"
  - 用户提到"推送到公众号"、"发草稿"、"发布日报"
  - 用户要求把采集到的资讯生成文章并发布
  - 用户要求规划选题 / 生成 brief
---

# AI 资讯自动化流水线

一个自包含的 skill，把四步能力串成一条 agent 可驱动的全自动链路：

```
采集 (collect) → 处理 (process) → 输出 (digest/plan) → 推送 (publish)
```

本 skill 自带全部 Python 源码（`src/ai_news_pipeline/`），通过 CLI（`run.py`）驱动各阶段。推送阶段调用 wechat-publisher skill。

> **⚠️ 推送阶段不自建发布逻辑。** 它调用 `wechat-publisher` skill 的 `publish.py`，由本 skill 的 `publishers/wechat.py` 通过 subprocess 自动完成。不要绕过它。

## 与 wechat-publisher 的分工

| 本 skill 负责 | wechat-publisher 负责 |
|---|---|
| 采集资讯（HN / RSS / arXiv / X） | 深度文章写作（7 阶段） |
| 启发式 + LLM 三关评分筛选 | 配图生成（多风格） |
| 卡片式日报生成 | 反 AI 检测打分（ai_score） |
| 选题规划（brief / research） | Markdown → 微信 HTML 转换 |
| **调用** wechat-publisher 推草稿 | 多账号 / 多主题 / 多平台同步 |

---

## 安装

### 方式 A：作为 Codex / Claude Code Skill 使用

将本 skill 放入 skills 目录：

```bash
# Codex
cp -r ai-news-pipeline ~/.codex/skills/ai-news-pipeline

# Claude Code
cp -r ai-news-publisher ~/.claude/skills/ai-news-publisher
```

### 方式 B：独立命令行使用

```bash
cd ai-news-pipeline
pip install -r requirements.txt
python run.py daily
```

### 依赖说明

- **Python >= 3.10**，核心依赖见 `requirements.txt`（requests / feedparser / pyyaml / python-dateutil）
- **wechat-publisher skill** — 推送阶段（阶段四）的**硬依赖**。必须先安装，否则 publish / digest 自动推送不可用：
  `ash
  npx skills add jiji262/wechat-publisher
  `
  安装后放在同级 skills 目录即可自动发现。采集 / 处理 / 输出（阶段一至三）不需要它，可独立使用。
- **LLM API**：OpenAI 兼容接口（如 GLM、DeepSeek、OpenAI），不配也能跑（降级为纯启发式评分）

---

## 前置条件检查

### 1) 配置文件

首次使用复制模板（config.yaml 已 gitignore）：

```bash
cd ai-news-pipeline   # 进入 skill 根目录
cp config.yaml.example config.yaml
```

必须填写的字段：

```yaml
llm:
  base_url: https://...   # OpenAI 兼容 API 地址
  api_key: sk-...         # 必填，否则 LLM 评分/日报增强全部降级为启发式
  model: glm-4.5-flash    # 默认模型

models:                   # 分阶段模型（可选，覆盖 llm.model）
  scorer: glm-5.2         # 三关评分
  enhance: glm-4.5-flash  # 日报卡片增强
  headline: glm-4.5-flash # 今日大事件选取
  paper_review: glm-5.2   # 论文审阅

wechat_publisher:
  skill_path: wechat-publisher  # 同级 skill 自动解析；或写绝对路径
  scripts_dir: scripts
```

wechat-publisher skill 自身也需要配置（含 AppID / AppSecret），详见其 SKILL.md。

### 2) 验证

```bash
python run.py collect   # 能跑通采集即配置 OK
```

---

## 完整工作流程（4 个阶段）

### 阶段一：采集（collect）

目标：从 HackerNews + RSS（含 arXiv / X/Twitter）抓取当天资讯，落盘原始数据。

```bash
cd ai-news-pipeline
python run.py collect                    # 采集今天
python run.py collect --date 2026-07-02  # 采集指定日期
```

**发生了什么：**

- HackerNewsCollector 走 Algolia 搜索 API，按 `collectors.hn_queries` 逐个查询，用 `date_window()` 精确过滤当天 UTC 时间窗
- RssCollector 抓取 20+ RSS 源，每个源带 `weight` 做源头信任分级（一手源权重高，arXiv 故意压低）
- ArXiv 质量预过滤：只收 cross-list、作者 >=2、摘要 >=300 字、剔除概念炒作标题
- X/Twitter（nitter RSS）做转推折叠、链接噪声清洗、低信息量推文过滤
- ArXiv 论文可选 LLM 审阅（`collectors.arxiv_review.enabled`），输出 `{date}_review.json`

**产出：** `data/raw/{date}.json`

**阶段判断：** 采集量 <10 多半是网络或 RSS 源失效，应检查源配置。

### 阶段二：信息处理（process）

目标：去重、分类、评分、排序。这是整个系统的"大脑"。

```bash
python run.py process                    # 处理今天
python run.py process --date 2026-07-02
```

**三层智能：**

1. **启发式粗筛**（200+ → ~60）
   - 四因子打分：`feed_weight × quality_factor + recency_boost + keyword_boost + source_authority_bonus`
   - 分类为四板块：AI 资讯 / AI 文献 / 半导体资讯 / 半导体文献
   - 标题相似度去重（SequenceMatcher，阈值 0.85）+ 跨天去重（扫过去 7 天）

2. **LLM 三关精排**（~60 → ~25）
   - 每板块 top-N 候选过 LLM（新闻 top15 / 论文 top30）
   - 三关评分：importance（权重 2.5）+ actionability（0.6）+ timeliness（0.3）
   - 加法融合（非乘法）：让重要内容突破阈值，不被低 feed_weight 压顶

3. **min_score 过滤 + cap 截断 + backfill 补漏**

**双模式：** 同时生成 `publish`（可发布）和 `learning`（个人学习，不推送）两套结果。

**产出：** `data/processed/{date}.json`

### 阶段三：输出 article

#### 3a. 卡片式日报（digest）—— 推荐

```bash
python run.py digest                 # 生成今天的发布日报
python run.py learn                  # 生成今天的学习日报（本地，不推送）
python run.py digest --date 2026-07-02
```

- LLM 卡片增强：中文标题 / 星级 / 为什么值得看 / 核心要点
- 今日大事件：LLM 从 top 候选选"如果今天只发一条发哪条"
- 程序化生成封面图（PIL）

**产出：** `data/articles/{account}/{date}-daily/article.md` + `cover.jpg` + `meta.json`

`publish.auto_publish_digest: true` 时自动进入阶段四。

#### 3b. 深度文章选题（plan）+ LLM 写作（generate）

```bash
python run.py plan                   # 选 top-N 话题，生成 brief.md / research.md
python run.py generate               # LLM 写 article.md（需 generator.enabled）
python run.py generate --dir data/articles/main/2026-07-02-xxx
```

**写作交接：** plan 生成的 brief 目录，推荐交给 wechat-publisher skill 走完整 7 阶段写作流程。`run.py generate` 只是纯 LLM 直写，不带配图和反 AI 检测。

### 阶段四：推送到微信公众号（publish）

```bash
# 单篇发布
python run.py publish --dir data/articles/main/2026-07-02-daily
python run.py publish --dir data/articles/main/2026-07-02-daily --account tech
# 发布当天所有就绪文章（需 publish.auto_publish: true）
python run.py publish
```

`publishers/wechat.py` 调用 wechat-publisher skill 的 `publish.py`，自动完成：
- 从 `wechat-publisher.yaml` 读取账号配置
- 提取标题和摘要
- 图片处理 → HTML 转换 → 封面上传 → **AI 味 gate**（>=45 分拦截）→ 草稿创建

**发布成功后：** 草稿在 mp.weixin.qq.com 草稿箱，需人工确认群发。

**阶段判断：**
- `publish.py not found`：检查 `wechat_publisher.skill_path` 是否指向正确
- `ai_score FAIL`：回 wechat-publisher 阶段 3.5 重写，或 `--skip-ai-score`
- `40164`：IP 白名单未配
- `40001/40002`：wechat-publisher.yaml 的 AppID / AppSecret 错

---

## 一键全流程（daily）

```bash
python run.py daily                  # 今天
python run.py daily --date 2026-07-02
```

`daily` 依次执行：collect → process → plan。后续 generate / publish 由开关控制：

- `generator.enabled: true` → 自动 LLM 写作
- `publish.auto_publish: true` → 自动推草稿

---

## 常用任务模式

### 模式 A：每日日报自动推送（最常用）

```bash
python run.py collect
python run.py process
python run.py digest    # auto_publish_digest=true 时自动推草稿
```

### 模式 B：手动选材 + 深度文章 + 发布

```bash
python run.py collect && python run.py process && python run.py plan
# plan 输出的 brief 目录交给 wechat-publisher skill 写作配图
python run.py publish --dir data/articles/main/2026-07-02-xxx
```

### 模式 C：只采集不发布（个人学习）

```bash
python run.py collect && python run.py process
python run.py learn
```

---

## 定时任务

### Windows（任务计划程序）

```powershell
cd ai-news-pipeline\schedules
.\register-task.ps1              # 注册每天 08:00 的计划任务
# 自定义时间和名称
.\register-task.ps1 -RunAt "06:00" -TaskName "My-AI-News"
```

### Linux / macOS（cron）

```bash
# crontab -e
0 8 * * * /path/to/ai-news-pipeline/schedules/run-daily.sh
```

定时脚本会自动定位 skill 根目录（基于脚本自身路径），独立于安装位置。

---

## 数据目录约定

所有数据都在 skill 根目录的 `data/`（已 gitignore）：

```
data/
├─ raw/                  # 原始采集 JSON
│  ├─ 2026-07-02.json
│  └─ 2026-07-02_review.json   # ArXiv 审阅（可选）
├─ processed/            # 处理后排序（含 publish/learning 双模式）
│  └─ 2026-07-02.json
└─ articles/             # 文章工作目录
   └─ main/
      └─ 2026-07-02-daily/
         ├─ article.md   # 日报成品
         ├─ cover.jpg
         └─ meta.json
```

---

## 配置速查

完整配置见 `config.yaml.example`。最常调整的：

```yaml
collectors:
  rss_feeds:          # 加减 RSS 源
    - title: ...
      url: ...
      weight: 1.0
      purpose: publish    # publish | learning | both

processor:
  min_score_news: 3.0      # 各板块最低分
  cap_news: 5              # 各板块最大条数
  llm_scorer:
    enabled: true
    evaluate_top_n: 15

publish:
  auto_publish: false
  auto_publish_digest: true
  ai_score_threshold: 45
```

---

## 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| 采集量 <10 | RSS 源失效 / 网络 | 检查 `collectors.rss_feeds` |
| "using heuristic only" | LLM 未配置或失败 | 确认 `llm.api_key` 非 `sk-...` |
| 429 rate limit | LLM API 频率限制 | 降低 `llm_scorer.concurrency` |
| publish.py not found | skill 路径错 | 检查 `wechat_publisher.skill_path`；可设 `WECHAT_PUBLISHER_PATH` 环境变量 |
| ai_score FAIL | 文章 AI 味重 | 回 wechat-publisher 阶段 3.5 重写 |
| 40164 | IP 未加白名单 | 公众平台加白名单 |

---

## 目录结构

```
ai-news-pipeline/
├── SKILL.md                    # 本文件
├── README.md
├── run.py                      # CLI 入口
├── config.yaml.example         # 配置模板
├── requirements.txt
├── pyproject.toml              # 可选：pip install -e . 安装为包
├── .gitignore
├── src/
│   └── ai_news_pipeline/
│       ├── __init__.py
│       ├── config.py           # 配置加载 + 路径解析 + 时区
│       ├── models.py           # NewsItem 数据类
│       ├── pipeline.py         # 编排层（命令实现）
│       ├── collectors/         # 采集层（HN + RSS）
│       ├── processors/         # 筛选层（启发式 + LLM 三关评分）
│       ├── generators/         # 生成层（日报 + brief + LLM 文章）
│       ├── analyzers/          # 分析器（arXiv 论文审阅）
│       └── publishers/         # 发布层（调用 wechat-publisher）
└── schedules/                  # 定时任务脚本
    ├── register-task.ps1       # Windows 计划任务注册
    ├── run-daily.ps1           # Windows 每日执行
    └── run-daily.sh            # Linux/macOS 每日执行
```

---

## 注意事项

- 所有命令在 skill 根目录下执行（`cd ai-news-pipeline`）
- 推送阶段依赖 wechat-publisher skill（需另行安装，详见其 SKILL.md）
- 所有内容只进**草稿箱**，不自动群发
- LLM 调用全部内置优雅降级：失败回退启发式，不阻断日报
- 跨天去重默认回看 7 天（`processor.dedup_lookback_days`）