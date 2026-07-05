# AI 资讯自动化流水线

每日定时采集 AI 资讯 → 去重评分 → 生成选题 brief →（Cursor Agent 写作配图）→ 发布微信公众号草稿箱。

与仓库内的 [wechat-publisher](../.agents/skills/wechat-publisher/) skill 配合使用：本流水线负责**采集与编排**，wechat-publisher 负责**写作、配图、排版与发布**。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    ai-news-pipeline                          │
│  collect → process → plan → [generate] → publish            │
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

python run.py daily      # 完整日报流水线（推荐）
python run.py collect    # 仅采集
python run.py process    # 仅处理（去重/评分）
python run.py plan       # 生成 brief.md + research.md
python run.py generate   # LLM 生成 article.md（需 config 中 generator.enabled=true）
python run.py publish --dir data/articles/main/2026-06-19-xxx
```

## 推荐工作流

### 自动化部分（定时执行）

`daily` 命令会自动完成：

1. 从 Hacker News Algolia + RSS 源采集 AI 资讯
2. 去重、关键词加权、按评分排序
3. 为 Top N 话题创建 `data/articles/<account>/<date>-<slug>/` 目录，内含 `brief.md` 和 `research.md`

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
| `collectors.rss_feeds` | RSS 源列表，可增删 |
| `collectors.hn_queries` | Hacker News 搜索关键词 |
| `processor.keyword_boost` | 关键词加权规则 |
| `schedule.max_topics` | 每天生成几个 brief（默认 3） |
| `generator.enabled` | 是否用 LLM API 自动生成 article.md |
| `publish.auto_publish` | daily 是否自动发布（需 article.md 已存在） |
| `wechat_publisher.skill_path` | wechat-publisher 相对路径 |

## 数据目录

```
data/
├── raw/           # 原始采集 JSON（按日期）
├── processed/     # 处理后排序 JSON
├── articles/      # 文章工作目录
│   └── main/
│       └── 2026-06-19-openai-gpt5/
│           ├── brief.md
│           ├── research.md
│           ├── meta.json
│           ├── article.md      # Agent/LLM 生成
│           ├── images/
│           └── cover.jpg
└── logs/          # 定时任务日志
```

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
ai-news daily   # 安装后可使用 ai-news 命令
```

## License

MIT
