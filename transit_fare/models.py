# -*- coding: utf-8 -*-
"""数据模型与输入解析（所有金额单位：整数分；时间均为带 UTC 偏移的 ISO-8601）。

输入 JSON 顶层结构：
{
  "calendars": [ {"service_day": "2026-11-24", "start": "...", "end": "..."} ],
  "stations":  [ {"station_id": "S1", "name": "..."} ],
  "events":    [ {"event_id": ..., "passenger_id": ..., "ticket_type": ...,
                  "time": "...", "line": ..., "direction": ...,
                  "board_station": ..., "alight_station": ..., "fare": 整数分} ],
  "transfers": [ {"from_station": ..., "to_station": ...,
                  "from_line": 可空, "to_line": 可空} ],
  "templates": [ {...套餐模板...} ],
  "packages":  [ {"package_id": ..., "template_id": ...,
                  "price": 整数, "allocations": [整数, ...]} ],
  "quotas":    [ {"passenger_id": 可空, "service_day": 可空,
                  "entitlement": ..., "limit": 整数} ]
}

权益额度键：
  * 同时给 passenger_id 与 service_day：某乘客某服务日的个人额度（跨日套餐扣首程服务日）。
  * passenger_id 为 null：所有乘客共享的全局额度（共享权益竞争）。
  * service_day 为 null 但给 passenger_id：该乘客跨服务日总额度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ----------------------------- 基础模型 -----------------------------

@dataclass(frozen=True)
class ServiceCalendar:
    """一个服务日：[start, end) 半开区间，start/end 带 UTC 偏移，可跨自然日（夜班）。"""
    service_day: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Station:
    station_id: str
    name: str


@dataclass(frozen=True)
class Event:
    event_id: str
    passenger_id: str
    ticket_type: str
    time: datetime
    line: str
    direction: str
    board_station: str
    alight_station: str
    fare: int  # 单程整数分价


@dataclass(frozen=True)
class TransferRule:
    """有向换乘关系：from_station 下车 -> to_station 上车。
    line 为 None 表示不限线路（仅按站判断）。"""
    from_station: str
    to_station: str
    from_line: Optional[str]
    to_line: Optional[str]


@dataclass(frozen=True)
class Template:
    """套餐模板：覆盖 2~3 程，按指定顺序匹配。"""
    template_id: str
    size: int
    max_elapsed_minutes: Optional[int]          # 首末最大实际经过时长（分钟）
    transfers: tuple[Optional[TransferRule], ...]  # 长度 size-1，None 表示该相邻程不校验换乘
    directions: tuple[Optional[str], ...]       # 每程方向，None 表示不限制
    ticket_types: tuple[Optional[str], ...]     # 每程票种，None 表示不限制
    same_service_day: bool                       # 是否要求各程同属一个服务日
    entitlement: Optional[str]                  # 消耗的权益，None 表示不消耗权益


@dataclass(frozen=True)
class Package:
    """套餐实例：固定总价 + 按模板各程的整数分摊，分摊和必须等于总价。"""
    package_id: str
    template_id: str
    price: int
    allocations: tuple[int, ...]


@dataclass(frozen=True)
class Quota:
    """权益额度。
    passenger_id=None  -> 全局共享额度（跨乘客竞争）
    service_day=None   -> 不区分服务日的总额度
    """
    passenger_id: Optional[str]
    service_day: Optional[str]
    entitlement: str
    limit: int


@dataclass
class Dataset:
    calendars: list[ServiceCalendar] = field(default_factory=list)
    stations: dict[str, Station] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    transfers: list[TransferRule] = field(default_factory=list)
    templates: dict[str, Template] = field(default_factory=dict)
    packages: list[Package] = field(default_factory=list)
    quotas: list[Quota] = field(default_factory=list)


class SchemaError(ValueError):
    """输入结构/取值非法（区别于运行期待核事件）。"""


# ----------------------------- 解析工具 -----------------------------

def _require(obj: dict, key: str, ctx: str) -> Any:
    if key not in obj:
        raise SchemaError(f"{ctx} 缺少字段 {key!r}")
    return obj[key]


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def parse_dt(raw: str, ctx: str) -> datetime:
    if not isinstance(raw, str):
        raise SchemaError(f"{ctx} 时间必须是字符串: {raw!r}")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SchemaError(f"{ctx} 时间无法解析: {raw!r} ({exc})") from None
    if dt.tzinfo is None:
        raise SchemaError(f"{ctx} 时间必须带 UTC 偏移: {raw!r}")
    return dt


def parse_dataset(raw: dict) -> Dataset:
    """解析并校验整份输入，返回 Dataset。"""
    if not isinstance(raw, dict):
        raise SchemaError("输入顶层必须是对象")
    ds = Dataset()

    # ---- 服务日历 ----
    seen_days: set[str] = set()
    intervals: list[tuple[datetime, datetime, str]] = []
    for i, c in enumerate(raw.get("calendars", [])):
        ctx = f"calendars[{i}]"
        day = str(_require(c, "service_day", ctx))
        if day in seen_days:
            raise SchemaError(f"{ctx} 服务日 {day} 重复")
        seen_days.add(day)
        start = parse_dt(_require(c, "start", ctx), ctx + ".start")
        end = parse_dt(_require(c, "end", ctx), ctx + ".end")
        if not start < end:
            raise SchemaError(f"{ctx} 要求 start < end")
        cal = ServiceCalendar(day, start, end)
        ds.calendars.append(cal)
        intervals.append((start, end, day))
    # 日历不重叠（半开区间）
    intervals.sort()
    for (s1, e1, d1), (s2, e2, d2) in zip(intervals, intervals[1:]):
        if s2 < e1:
            raise SchemaError(f"服务日 {d1} 与 {d2} 的日历区间重叠")

    # ---- 站点（同名异站靠稳定 station_id 区分）----
    for i, st in enumerate(raw.get("stations", [])):
        ctx = f"stations[{i}]"
        sid = str(_require(st, "station_id", ctx))
        if sid in ds.stations:
            raise SchemaError(f"{ctx} 站点 {sid} 重复定义")
        ds.stations[sid] = Station(sid, str(_require(st, "name", ctx)))

    # ---- 换乘关系 ----
    for i, tr in enumerate(raw.get("transfers", [])):
        ctx = f"transfers[{i}]"
        ds.transfers.append(TransferRule(
            from_station=str(_require(tr, "from_station", ctx)),
            to_station=str(_require(tr, "to_station", ctx)),
            from_line=_opt_str(tr.get("from_line")),
            to_line=_opt_str(tr.get("to_line")),
        ))

    # ---- 模板 ----
    for i, t in enumerate(raw.get("templates", [])):
        ctx = f"templates[{i}]"
        tid = str(_require(t, "template_id", ctx))
        if tid in ds.templates:
            raise SchemaError(f"{ctx} 模板 {tid} 重复定义")
        size = int(_require(t, "size", ctx))
        if size not in (2, 3):
            raise SchemaError(f"{ctx} 套餐只允许覆盖 2 或 3 程，得到 {size}")
        tr_raw = t.get("transfers")
        if tr_raw is None:
            tr_rules: tuple[Optional[TransferRule], ...] = tuple(None for _ in range(size - 1))
        else:
            if len(tr_raw) != size - 1:
                raise SchemaError(f"{ctx} transfers 长度必须为 {size - 1}")
            rules: list[Optional[TransferRule]] = []
            for tr in tr_raw:
                if tr is None:
                    rules.append(None)
                else:
                    # 哨兵 "__ANY__"：不固定具体站，只要求该相邻程的实际
                    # 下车->上车站对存在于全局 transfers 换乘表中。
                    rules.append(TransferRule(
                        from_station=str(_require(tr, "from_station", ctx)),
                        to_station=str(_require(tr, "to_station", ctx)),
                        from_line=_opt_str(tr.get("from_line")),
                        to_line=_opt_str(tr.get("to_line")),
                    ))
            tr_rules = tuple(rules)
        dirs_raw = t.get("directions")
        dirs = tuple(None for _ in range(size)) if dirs_raw is None else tuple(
            _opt_str(x) for x in dirs_raw)
        if len(dirs) != size:
            raise SchemaError(f"{ctx} directions 长度必须为 {size}")
        tt_raw = t.get("ticket_types")
        ttypes = tuple(None for _ in range(size)) if tt_raw is None else tuple(
            _opt_str(x) for x in tt_raw)
        if len(ttypes) != size:
            raise SchemaError(f"{ctx} ticket_types 长度必须为 {size}")
        max_el = t.get("max_elapsed_minutes")
        if max_el is not None:
            max_el = int(max_el)
            if max_el <= 0:
                raise SchemaError(f"{ctx} max_elapsed_minutes 必须为正整数")
        ent = t.get("entitlement")
        ds.templates[tid] = Template(
            template_id=tid, size=size, max_elapsed_minutes=max_el,
            transfers=tr_rules, directions=dirs, ticket_types=ttypes,
            same_service_day=bool(t.get("same_service_day", False)),
            entitlement=_opt_str(ent),
        )

    # ---- 套餐实例 ----
    for i, p in enumerate(raw.get("packages", [])):
        ctx = f"packages[{i}]"
        pid = str(_require(p, "package_id", ctx))
        tid = str(_require(p, "template_id", ctx))
        tpl = ds.templates.get(tid)
        if tpl is None:
            raise SchemaError(f"{ctx} 引用了不存在的模板 {tid}")
        price = int(_require(p, "price", ctx))
        if price < 0:
            raise SchemaError(f"{ctx} 套餐价格不能为负")
        alloc = tuple(int(x) for x in _require(p, "allocations", ctx))
        if len(alloc) != tpl.size:
            raise SchemaError(f"{ctx} 分摊数 {len(alloc)} 与模板程数 {tpl.size} 不符")
        if any(a < 0 for a in alloc):
            raise SchemaError(f"{ctx} 分摊金额不能为负")
        if sum(alloc) != price:
            raise SchemaError(
                f"{ctx} 分摊之和 {sum(alloc)} 不等于套餐总价 {price}")
        if any(p2.package_id == pid for p2 in ds.packages):
            raise SchemaError(f"{ctx} 套餐 {pid} 重复定义")
        ds.packages.append(Package(pid, tid, price, alloc))

    # ---- 权益额度 ----
    for i, q in enumerate(raw.get("quotas", [])):
        ctx = f"quotas[{i}]"
        limit = int(_require(q, "limit", ctx))
        if limit < 0:
            raise SchemaError(f"{ctx} limit 不能为负")
        ds.quotas.append(Quota(
            passenger_id=_opt_str(q.get("passenger_id")),
            service_day=_opt_str(q.get("service_day")),
            entitlement=str(_require(q, "entitlement", ctx)),
            limit=limit,
        ))

    # ---- 事件（字段完整性、站点引用在此校验；去重/冲突在 ingest 层处理）----
    known_ids = {e.event_id for e in ds.events}
    for i, ev in enumerate(raw.get("events", [])):
        ctx = f"events[{i}]"
        eid = str(_require(ev, "event_id", ctx))
        if eid in known_ids:
            raise SchemaError(f"{ctx} event_id {eid} 在同一份输入中重复")
        known_ids.add(eid)
        board = str(_require(ev, "board_station", ctx))
        alight = str(_require(ev, "alight_station", ctx))
        if ds.stations:  # 提供了站点表才强校验引用
            if board not in ds.stations:
                raise SchemaError(f"{ctx} 上车站 {board} 不在站点表中")
            if alight not in ds.stations:
                raise SchemaError(f"{ctx} 下车站 {alight} 不在站点表中")
        fare = int(_require(ev, "fare", ctx))
        if fare < 0:
            raise SchemaError(f"{ctx} 单程票价不能为负")
        ds.events.append(Event(
            event_id=eid,
            passenger_id=str(_require(ev, "passenger_id", ctx)),
            ticket_type=str(_require(ev, "ticket_type", ctx)),
            time=parse_dt(_require(ev, "time", ctx), ctx + ".time"),
            line=str(_require(ev, "line", ctx)),
            direction=str(_require(ev, "direction", ctx)),
            board_station=board,
            alight_station=alight,
            fare=fare,
        ))
    return ds
