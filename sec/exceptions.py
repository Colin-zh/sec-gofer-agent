"""定义 sec SDK 对外报告的运行时异常类型。

异常用于区分协议违规、Run 预算耗尽和 Session 并发冲突等可处理失败；
调用方应据异常类型决定下一步，而不是依赖错误文本匹配。
"""

import math


class SecError(Exception):
    """sec SDK 异常的基类。"""


class ProviderProtocolError(SecError):
    """Provider 返回内容不符合模型协议时抛出。"""


class SecProtocolError(SecError):
    """SDK 或外部协议数据不符合预期结构时抛出。"""


class RetryableProviderError(SecError):
    """Provider 报告可重试请求失败及可选的服务端退避建议。"""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
    ) -> None:
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise ValueError("retry-after must be a non-negative finite number")
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class RetryableToolError(SecError):
    """工具明确报告可安全重试的暂时性失败。"""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise ValueError("retry-after must be a non-negative finite number")
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ToolExecutorProtocolError(SecError):
    """Executor 未返回与请求匹配的规范化工具结果时抛出。"""


class RunLimitExceeded(SecError):
    """Run 达到配置的调用上限时抛出。"""


class ContextBuildError(SecError):
    """模型工作上下文无法安全构建时抛出。"""


class ContextWindowExceeded(ContextBuildError):
    """保留近期完整对话后仍无法满足模型上下文上限时抛出。"""


class SessionBusyError(SecError):
    """同一 Session 被多个活动 Run 并发使用时抛出。"""


class SecSubscriptionOverflow(SecError):
    """事件订阅者未能及时消费，导致其有界缓冲区溢出。"""


class SecUIHandlerError(SecError):
    """扩展 UI handler 执行失败；原始异常保留在 __cause__。"""

    def __init__(self, handler_name: str) -> None:
        self.handler_name = handler_name
        super().__init__(f"extension UI handler {handler_name!r} failed")


class SessionStoreError(SecError):
    """SessionStore 无法完成读取或追加操作时抛出。"""


class SessionVersionConflictError(SessionStoreError):
    """追加时的期望版本与存储中的当前版本不匹配。"""

    def __init__(
        self,
        session_id: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        super().__init__(
            f"session {session_id!r} version conflict: "
            f"expected {expected_version}, found {actual_version}"
        )
        self.session_id = session_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class ObjectStoreError(SecError):
    """对象存储无法完成读取、写入、删除或关闭操作时抛出。"""


class ObjectNotFoundError(ObjectStoreError):
    """请求读取的对象不存在时抛出。"""
