# Cursor Agent 日报任务提示词
#
# 用于 Cursor Automations 或手动 Agent 会话。
# 将 {{ARTICLE_DIR}} 替换为 plan 阶段输出的目录路径。

你正在执行 **AI 资讯日报** 的写作与发布任务。

## 任务目录

{{ARTICLE_DIR}}

## 必读文件

1. `brief.md` — 选题摘要与写作要求
2. `research.md` — 已采集的调研素材

## 执行步骤

使用 **wechat-publisher** skill 完成以下阶段：

1. **补充调研**：基于 brief 中的关键词，WebSearch 补充最新权威数据和真人讨论语料，更新 `research.md`
2. **撰写骨架稿**：按 brief 中的 `article_structure` 和 `opening_hook` 写 `article.md`
3. **人味化改写**：独立 pass，逐条过反 AI 检测清单
4. **生成配图**：为每个小节生成 6-10 张手绘风格配图到 `images/`，生成 `cover.jpg`
5. **AI 味 gate**：`python scripts/ai_score.py <article.md> --threshold 45`
6. **发布草稿**：在项目根目录执行
   ```
   cd ai-news-pipeline
   python run.py publish --dir {{ARTICLE_DIR}}
   ```

## 约束

- 所有文件写入任务目录，不要写到 wechat-publisher skill 根目录
- 发布前必须过 ai_score gate
- 文章发布到草稿箱，不自动群发
