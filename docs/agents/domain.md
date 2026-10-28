# 领域文档

说明工程 skills 在探索代码库时应如何读取本仓库的领域文档。

本仓库采用**单上下文**领域文档布局。

## 探索代码前先读取

- 仓库根目录的 **`GLOSSARY.md`**。
- **`docs/adr/`**：读取与当前工作领域相关的 ADR。

如果上述文件尚不存在，**静默继续**。不要主动指出它们缺失，也不要建议预先创建。首次真正确定术语或架构决策时，由 `/domain-modeling` skill（通过 `/grill-with-docs` 和 `/improve-codebase-architecture` 使用）按需惰性创建。

## 文件结构

```text
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-example-decision.md
│   └── 0002-another-decision.md
├── app/
├── web/
└── ...
```

## 使用术语表中的词汇

当输出中出现领域概念时（例如 issue 标题、重构提案、假设或测试名称），使用 `GLOSSARY.md` 中定义的术语，不要改用术语表明确排除的同义词。

如果需要的概念尚未进入术语表，这是一个信号：要么正在引入项目并未使用的语言（应重新考虑），要么确实存在术语缺口（记录给 `/domain-modeling` 处理）。

## 标记 ADR 冲突

如果输出与现有 ADR 冲突，必须明确指出，而不是静默覆盖：

> _这与 ADR-0007（event-sourced orders）冲突，但值得重新讨论，因为……_
