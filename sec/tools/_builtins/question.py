"""定义内置的 ``question`` 工具，用于向用户提问并等待回答。

该工具不执行任何副作用：运行时在 ``tool_dispatch`` 中按名字拦截它，改为走
``ctx.ui`` 阻塞通道向宿主（CLI / Desktop）提问，并把用户回答包成
``ToolResultMessage`` 回喂模型。这里的函数体只是占位，正常路径不会调用。
"""

from __future__ import annotations

from sec.tools.tool import Tool, tool

QUESTION_TOOL_NAME = "question"
QUESTION_TOOL_SOURCE = "sec.builtin.user"


def create_question_tool() -> Tool:
    @tool(
        name=QUESTION_TOOL_NAME,
        description=(
            "Ask the user a clarifying question and wait for their answer. "
            "Use this only when the task is genuinely ambiguous and the user's "
            "answer would change how you proceed; prefer proceeding with a "
            "reasonable default over asking. If 'options' is given (two or more "
            "strings), the user picks one; otherwise they type a free-form answer."
        ),
        risk_level="read",
        source=QUESTION_TOOL_SOURCE,
        workspace_scoped=False,
    )
    def question(question: str, options: list[str] = None) -> str:
        """Ask the user a question and return their answer (host-interactive)."""
        raise NotImplementedError(
            "the question tool is handled by the runtime via ctx.ui"
        )

    return question
