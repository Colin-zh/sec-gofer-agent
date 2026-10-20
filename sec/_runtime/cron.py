"""5 字段 cron 表达式解析与下一次触发时间计算（纯 Python，无 I/O）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
# 一个 cron 周期内最多扫描的天数（覆盖 8 年，足以命中 2 月 29 日这类稀疏日程）。
_MAX_SCAN_DAYS = 366 * 8 + 2


class CronScheduleError(ValueError):
    """cron 表达式非法或未来 8 年内不存在可触发时刻。"""


class CronSchedule:
    """解析并匹配 5 字段 cron 表达式（分 时 日 月 周）。

    字段支持 ``*``、``*/n``、``a``、``a,b,c``、``a-b``、``a-b/n``；
    月与星期接受大小写不敏感的三字母缩写，星期 0-7（7 视为 0，即周日）。
    日与星期的取舍遵循 Vixie 规则：两者都受限时取 OR，仅一个受限时取 AND。
    """

    def __init__(self, expr: str) -> None:
        if not isinstance(expr, str) or not expr.strip():
            raise CronScheduleError("cron expression must not be empty")
        fields = expr.split()
        if len(fields) != 5:
            raise CronScheduleError(
                "cron expression must have exactly 5 fields: "
                "minute hour day-of-month month day-of-week"
            )
        self._expr = " ".join(fields)
        self._minutes = _parse_field(fields[0], 0, 59)
        self._hours = _parse_field(fields[1], 0, 23)
        self._months = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
        # Vixie 用字面量 ``*`` 判断字段是否受限。
        self._dom = None if fields[2] == "*" else _parse_field(fields[2], 1, 31)
        dow = _parse_field(fields[4], 0, 7, _DOW_NAMES)
        self._dow = None if fields[4] == "*" else frozenset(
            0 if value == 7 else value for value in dow
        )
        self._minutes_sorted = tuple(sorted(self._minutes))
        self._hours_sorted = tuple(sorted(self._hours))

    def __str__(self) -> str:
        return self._expr

    def matches(self, value: datetime) -> bool:
        """判断给定时刻是否命中该日程（分钟粒度）。"""
        if value.minute not in self._minutes or value.hour not in self._hours:
            return False
        return self._day_matches(value.date())

    def next_after(self, after: datetime) -> datetime:
        """返回严格晚于 ``after`` 的下一个触发时刻（本地朴素时间）。"""
        if after.tzinfo is not None:
            after = after.replace(tzinfo=None)
        day = after.date()
        for _ in range(_MAX_SCAN_DAYS):
            if self._day_matches(day):
                for hour in self._hours_sorted:
                    for minute in self._minutes_sorted:
                        candidate = datetime(
                            day.year, day.month, day.day, hour, minute,
                        )
                        if candidate > after:
                            return candidate
            day += timedelta(days=1)
        raise CronScheduleError(
            f"cron schedule {self._expr!r} has no occurrence within 8 years"
        )

    def _day_matches(self, day: date) -> bool:
        if day.month not in self._months:
            return False
        dom = self._dom
        dow = self._dow
        if dom is not None and dow is not None:
            return day.day in dom or _cron_dow(day) in dow
        if dom is not None:
            return day.day in dom
        if dow is not None:
            return _cron_dow(day) in dow
        return True


def _cron_dow(day: date) -> int:
    """把 Python 的 weekday（周一=0）映射为 cron 星期（周日=0）。"""
    return (day.weekday() + 1) % 7


def _parse_field(
    expr: str,
    lo: int,
    hi: int,
    names: dict[str, int] | None = None,
) -> frozenset[int]:
    result: set[int] = set()
    for part in expr.split(","):
        result.update(_parse_field_part(part, lo, hi, names))
    return frozenset(result)


def _parse_field_part(
    part: str,
    lo: int,
    hi: int,
    names: dict[str, int] | None,
) -> set[int]:
    step = 1
    if "/" in part:
        part, step_text = part.split("/", 1)
        step = _parse_step(step_text)
    if part == "*":
        start, end = lo, hi
    elif "-" in part:
        start_text, end_text = part.split("-", 1)
        start = _parse_value(start_text, lo, hi, names)
        end = _parse_value(end_text, lo, hi, names)
        if start > end:
            raise CronScheduleError(f"cron range {part!r} is descending")
    else:
        start = end = _parse_value(part, lo, hi, names)
    if start < lo or end > hi:
        raise CronScheduleError(
            f"cron value out of range [{lo}-{hi}]: {part!r}"
        )
    return set(range(start, end + 1, step))


def _parse_step(text: str) -> int:
    try:
        step = int(text)
    except ValueError as exc:
        raise CronScheduleError(f"cron step must be an integer: {text!r}") from exc
    if step <= 0:
        raise CronScheduleError(f"cron step must be positive: {step!r}")
    return step


def _parse_value(
    text: str,
    lo: int,
    hi: int,
    names: dict[str, int] | None,
) -> int:
    text = text.strip()
    if names is not None:
        key = text.casefold()
        if key in names:
            return names[key]
    try:
        return int(text)
    except ValueError as exc:
        raise CronScheduleError(f"invalid cron value: {text!r}") from exc


__all__ = ["CronSchedule", "CronScheduleError"]
