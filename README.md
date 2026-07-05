# AI 资讯自动化流水线

每日定时采集 AI 资讯 → 去重评分 → 生成日报/选题 brief → 知识卡片（可选）→ 发布微信公众号草稿箱。

与仓库内的 [wechat-publisher](../.agents/skills/wechat-publisher/) skill 配合使用：本流水线负责**采集、评分与编排**，wechat-publisher 负责**写作、配图、排版与发布**。知识卡片功能可独立于微信发布，直接输出到 Obsidian vault。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    ai-news-pipeline                          │
│  collect → process → digest → publish                        │
│                  ├─ plan → [generate] → publish              │
│                  └─ learn → [knowledge card] → Obsidian      │
└──────────────────────────┬──────────────────────────────────┘
                           │ brief.md / article.md
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              wechat-publisher skill                          │
│  调研 → 写作 → 人味化 → 配图 → HTML → AI gate → 草稿箱      │
└─────────────────────────────────────────────────────────────┘
```

## 安装

```powershell
cd ai-news-pipeline

# 1. 配置文件
copy config.yaml.example config.yaml

# 2. 虚拟环境
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. 微信公众号配置（在上级目录 skill 中）
cd ..\.agents\skills\wechat-publisher
copy wechat-publisher.yaml.example wechat-publisher.yaml
# 填入 app_id / app_secret / 生图配置
```

## 命令

```powershell
cd ai-news-pipeline
.venv\Scripts\activate

python run.py daily      # 完整流水线（collect → process → plan）
python run.py collect    # 仅采集
python run.py process    # 仅处理（去重/评分/分类）
python run.py digest     # 生成每日资讯日报（auto_publish_digest 时自动推送）
python run.py learn      # 生成个人学习日报 + 知识卡片（本地，不推送）
python run.py plan       # 生成 brief.md + research.md
python run.py generate   # LLM 生成 article.md（需 generator.enabled=true）
python run.py publish --dir data/articles/main/2026-06-19-xxx
```

## 推荐工作流

### 自动化部分（定时执行）

`daily` 命令会自动完成：

1. 从 Hacker News Algolia + RSS 源采集 AI 资讯
2. 启发式评分 + LLM 三关评分（importance/timeliness/actionability）去重排序
3. 按分类（AI 资讯/文献、半导体资讯/文献）分桶输出
4. 为 Top N 话题创建 `data/articles/<account>/<date>-<slug>/` 目录，内含 `brief.md` 和 `research.md`

### 知识卡片（可选）

`learn` 命令在生成学习日报的同时，如果 `knowledge_base.enabled=true`，会额外生成结构化知识卡片到 Obsidian vault：

- **AI 日报卡片**：每条资讯按 importance 分层（1-2 仅事实，3 加 why/how，4-5 完整分析）
- **文献日报卡片**：论文按 LLM 审阅生成创新点/局限/评级
- **新闻要点提取**：LLM 结构化提取事件摘要、涉及公司/产品、行业影响

输出路径由 `knowledge_base.vault_root` + `news_subpath` 控制。

### 人工/Agent 部分（写作与配图）

对 plan 输出的目录，在 Cursor 中运行 **wechat-publisher** skill：

```
使用 wechat-publisher skill，对 data/articles/main/2026-06-19-xxx 目录
完成文章写作、配图、AI 味检测，并发布到草稿箱
```

或参考 `prompts/agent-daily.md` 中的 Agent 提示词。

### 发布

article.md 和 cover.jpg 就绪后：

```powershell
python run.py publish --dir data/articles/main/2026-06-19-xxx
```

## 定时任务

### Windows（任务计划程序）

```powershell
cd schedules
.\register-task.ps1              # 注册每天 08:00 执行
Start-ScheduledTask -TaskName "AI-News-Daily-Pipeline"  # 手动测试
```

日志：`data/logs/YYYY-MM-DD.log`

### Linux/macOS（cron）

```bash
chmod +x schedules/run-daily.sh
# crontab -e
# 0 8 * * * /path/to/ai-news-pipeline/schedules/run-daily.sh
```

## 配置说明

| 配置项 | 说明 |
|--------|------|
| `collectors.rss_feeds` | RSS 源列表，可增减 |
| `collectors.hn_queries` | Hacker News 搜索关键词 |
| `processor.keyword_boost` | 关键词加权规则 |
| `processor.llm_scorer` | LLM 三关评分（importance/timeliness/actionability） |
| `models` | 分阶段模型覆盖（scorer/enhance/headline/paper_review） |
| `categories` | 动态分类配置（ai/semiconductor 等，控制分桶与标签） |
| `schedule.max_topics` | 每天生成几个 brief（默认 3） |
| `generator.enabled` | 是否用 LLM API 自动生成 article.md |
| `publish.auto_publish` | daily 是否自动发布（需 article.md 已存在） |
| `publish.auto_publish_digest` | digest 是否自动推送日报到草稿箱 |
| `knowledge_base.enabled` | learn 时是否生成知识卡片到 Obsidian |
| `knowledge_base.vault_root` | Obsidian vault 根路径 |
| `wechat_publisher.skill_path` | wechat-publisher 相对路径 |

## 数据目录

```
data/
├── raw/           # 原始采集 JSON（按日期）
├── processed/     # 处理后排序 JSON（含 publish/learning 双模式）
├── articles/      # 文章工作目录
│   ├── main/      # 发布用（digest/plan 输出）
│   │   └── 2026-06-19-openai-gpt5/
│   │       ├── brief.md
│   │       ├── research.md
│   │       ├── meta.json
│   │       ├── article.md      # Agent/LLM 生成
│   │       ├── images/
│   │       └── cover.jpg
│   └── personal/  # 学习用（learn 输出，不推送）
│       └── 2026-06-19-daily/
└── logs/          # 定时任务日志
```

知识卡片输出到 `knowledge_base.vault_root` 指定的 Obsidian vault，不在 data/ 下。

## 可选：纯 LLM 自动生成

在 `config.yaml` 中设置：

```yaml
generator:
  enabled: true

llm:
  base_url: https://api.openai.com/v1
  api_key: sk-...
  model: gpt-4o
```

然后 `python run.py daily` 会在 plan 之后自动调用 LLM 生成 article.md。

注意：LLM 生成的文章仍需配图和 AI 味审校，完整质量仍建议走 wechat-publisher skill。

## 开发

```powershell
pip install -e .
ai-news daily   # 安装后可使用 ai-news 命令（与 python run.py 等价）
```

## License

MIT