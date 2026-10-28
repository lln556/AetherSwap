# Issue tracker：本地 Markdown

本仓库的 issue 和 spec 以 Markdown 文件形式保存在 `.scratch/` 中。

## 约定

- 每个功能使用一个独立目录：`.scratch/<feature-slug>/`
- 功能 spec 保存为：`.scratch/<feature-slug>/spec.md`
- 每个实现 issue 单独保存为一个文件：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- issue 从 `01` 开始编号，不要把所有 ticket 合并到同一个文件
- triage 状态记录在 issue 文件顶部附近的 `Status:` 行中，状态字符串见 `triage-labels.md`
- 评论和讨论历史追加到文件底部的 `## Comments` 标题下

例如：

```markdown
# 修复历史记录中的 User-Agent 问题

Status: ready-for-agent

## 描述

...

## Comments

- 2026-10-28：完成初步分析。
```

## 当 skill 要求“publish to the issue tracker”

在 `.scratch/<feature-slug>/` 下创建新的 Markdown 文件；如果目录不存在则一并创建。

## 当 skill 要求“fetch the relevant ticket”

直接读取指定路径的文件。用户通常会提供文件路径或 issue 编号。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是一个 Markdown 文件，每个 **child ticket** 是独立文件。

- **Map**：`.scratch/<effort>/map.md`，正文记录 Notes / Decisions-so-far / Fog。
- **Child ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 开始编号，正文记录待解决的问题。使用 `Type:` 行记录 ticket 类型（`research`、`prototype`、`grilling` 或 `task`），使用 `Status:` 行记录 `claimed` 或 `resolved`。
- **阻塞关系**：在文件顶部附近写入 `Blocked by: NN, NN`。当列出的所有文件状态均为 `resolved` 时，该 ticket 解除阻塞。
- **Frontier 查询**：扫描 `.scratch/<effort>/issues/`，找出尚未完成、没有阻塞项且未被认领的文件；编号最小者优先。
- **认领**：开始工作前，将状态改为 `Status: claimed` 并保存。
- **完成**：在 `## Answer` 标题下追加答案，将状态改为 `Status: resolved`，然后把上下文指针（结论摘要 + 链接）追加到 `map.md` 的 Decisions-so-far。
