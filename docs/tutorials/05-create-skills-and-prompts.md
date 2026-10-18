# 添加 Skills 和 Prompts

Skills 和 Prompts 都是 Markdown 提示资源。CLI 只将其内容作为提示词发送，不执行文件中的代码。

## 文件位置

工作区资源优先于用户级同名资源：

```text
.sec/
├── skills/
│   └── review/
│       └── SKILL.md
└── prompts/
    └── concise.md
```

用户级资源放在 `~/.sec/skills/` 和 `~/.sec/prompts/`。Skill 也可使用 `.sec/skills/<name>.md`。

## Skill 示例

```markdown
---
name: review
description: Review changes for bugs and regressions
---
检查相关实现、调用点和测试。
只报告有证据支持的问题，并给出文件和行号。
```

Skill 的 `name` 和 `description` 必填；资源名称使用小写字母、数字和连字符，例如 `code-review`。

## Prompt 示例

```markdown
---
name: concise
description: 简洁的回答风格
---
请先给结论，再提供必要的依据和操作步骤。
```

Prompt 的 `description` 可省略。比如角色设定、语气和固定输出格式通常适合做成 Prompt；可复用的任务流程通常适合做成 Skill。

## 调用和重新加载

```text
/skill:review 检查当前改动
/prompt:concise 总结这段日志
/reload
```

不附请求时，Skill 会按“将此 Skill 用于当前项目”调用；Prompt 则直接以其内容发起请求。资源重名时，工作区版本覆盖用户级版本；覆盖会显示在资源问题中。`/help full` 可查看加载资源及问题。
