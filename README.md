# ai-news-pipeline

AI 资讯自动化流水线 skill。把"采集 → 信息处理 → 输出 article → 调用 wechat-publisher 推送微信公众号"四步打包成一条自包含、可独立安装的自动链路。

## 它是什么

一个自包含的 Codex / Claude Code skill，自带全部 Python 源码（采集器、启发式 + LLM 三关评分筛选器、日报生成器、发布器）。其他用户安装后无需依赖任何外部项目即可运行。推送阶段调用同级的 [wechat-publisher](../wechat-publisher) skill。

## 安装

```bash
# 作为 skill 使用（Codex / Claude Code）
cp -r ai-news-pipeline ~/.codex/skills/ai-news-pipeline

# 或独立命令行使用
cd ai-news-pipeline
pip install -r requirements.txt
```

## 快速开始

```bash
cd ai-news-pipeline
cp config.yaml.example config.yaml   # 填入 llm.api_key

# 一键全流程
python run.py daily

# 或分步
python run.py collect    # 采集
python run.py process    # 筛选评分
python run.py digest     # 生成日报（auto_publish_digest=true 时自动推送）
```

## 四个阶段

| 阶段 | 命令 | 作用 |
|---|---|---|
| 采集 | `run.py collect` | HN + RSS（含 arXiv / X）多源采集 |
| 处理 | `run.py process` | 去重、分类、启发式 + LLM 三关评分排序 |
| 输出 | `run.py digest` / `run.py plan` | 卡片日报 / 深度文章 brief |
| 推送 | `run.py publish` | 调用 wechat-publisher 推微信草稿箱 |

## 依赖

- **Python >= 3.10**（requests / feedparser / pyyaml / python-dateutil）
- **wechat-publisher skill** — 推送阶段（阶段四）的硬依赖，必须先装：
  `ash
  npx skills add jiji262/wechat-publisher
  `
  装到同级 skills 目录即可。采集 / 处理 / 输出（阶段一至三）不需要它。
- **LLM API**（可选，不配则降级为纯启发式评分）

## 文档

完整工作流、配置速查、故障排查见 [SKILL.md](SKILL.md)。