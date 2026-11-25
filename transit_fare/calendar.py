# -*- coding: utf-8 -*-
"""服务日日历：用带 UTC 偏移的绝对时刻落到半开区间 [start, end)。

夜班线路的服务日可以跨自然日（end 的日历日晚于 start），区间互不重叠。
"""
from __future__ import annotations

from datetime import datetime

from .models import ServiceCalendar


class ServiceDayIndex:
    def __init__(self, calendars: list[ServiceCalendar]):
        self._intervals = sorted(
            ((c.start, c.end, c.service_day) for c in calendars),
            key=lambda t: t[0],
        )

    def service_day_of(self, dt: datetime) -> str | None:
        """返回时刻所属服务日；不属于任何已声明日历则返回 None（待核，不猜测）。"""
        for start, end, day in self._intervals:
            if dt < start:
                # 区间按 start 有序，dt 比当前区间还早，后面的更晚
                return None
            if start <= dt < end:
                return day
        return None

    @property
    def service_days(self) -> list[str]:
        return [day for _, _, day in self._intervals]
