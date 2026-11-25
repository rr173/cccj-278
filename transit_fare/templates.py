# -*- coding: utf-8 -*-
"""套餐模板匹配：为每名乘客枚举所有合法套餐候选。

候选是该乘客按时间排序后、长度为 2~3 的“连续程窗口”（窗口内各程都进套餐，
窗口外的程走单程；套餐腿集合互不重叠），逐对相邻程校验：
  1) 有向换乘关系（前一程下车稳定站 -> 后一程上车稳定站，可再限线路）；
  2) 每程的方向 / 票种；
  3) 首末最大实际经过时长（用带偏移的绝对时刻相减，天然支持跨午夜）；
  4) 是否要求同一服务日（跨午夜但落在同一夜班服务日仍合法）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Event, Package, Template, TransferRule


@dataclass(frozen=True)
class Leg:
    """乘客的一程（一个可解释服务日的事件）。"""
    index: int            # 在该乘客排序后事件列表中的位置
    event: Event
    service_day: str


@dataclass(frozen=True)
class Candidate:
    package: Package
    template: Template
    legs: tuple[Leg, ...]

    @property
    def key(self) -> tuple:
        return (self.package.package_id, tuple(l.index for l in self.legs))


_ANY = "__ANY__"


def _transfer_ok(rule: Optional[TransferRule], a: Event, b: Event,
                 valid_pairs: frozenset[tuple]) -> bool:
    if rule is None:
        return True
    if rule.from_station == _ANY and rule.to_station == _ANY:
        # 哨兵：只要求实际站对存在于全局换乘表（线路另作可选约束）
        if (a.alight_station, b.board_station) not in valid_pairs:
            return False
        if rule.from_line is not None and a.line != rule.from_line:
            return False
        if rule.to_line is not None and b.line != rule.to_line:
            return False
        return True
    if a.alight_station != rule.from_station or b.board_station != rule.to_station:
        return False
    if rule.from_line is not None and a.line != rule.from_line:
        return False
    if rule.to_line is not None and b.line != rule.to_line:
        return False
    return True


def _template_matches(
    tpl: Template,
    legs: tuple[Leg, ...],
    valid_pairs: frozenset[tuple],
) -> bool:
    events = tuple(l.event for l in legs)

    # 每程方向 / 票种
    for i, ev in enumerate(events):
        want_dir = tpl.directions[i]
        if want_dir is not None and ev.direction != want_dir:
            return False
        want_tt = tpl.ticket_types[i]
        if want_tt is not None and ev.ticket_type != want_tt:
            return False

    # 相邻有向换乘
    for i, rule in enumerate(tpl.transfers):
        if not _transfer_ok(rule, events[i], events[i + 1], valid_pairs):
            return False

    # 首末最大实际经过时长（绝对时刻差，跨午夜自动正确）
    if tpl.max_elapsed_minutes is not None:
        elapsed = (events[-1].time - events[0].time).total_seconds()
        if elapsed < 0:
            return False
        if elapsed > tpl.max_elapsed_minutes * 60:
            return False

    # 同一服务日
    if tpl.same_service_day:
        days = {l.service_day for l in legs}
        if len(days) != 1:
            return False
    return True


def build_legs(events: list[Event], sday_index) -> tuple[list[Leg], list[Event]]:
    """按 (时刻, event_id) 稳定排序，拆出可解释的程与待核事件。"""
    ordered = sorted(events, key=lambda e: (e.time, e.event_id))
    legs: list[Leg] = []
    pending: list[Event] = []
    for i, ev in enumerate(ordered):
        day = sday_index.service_day_of(ev.time)
        if day is None:
            pending.append(ev)
        else:
            legs.append(Leg(index=len(legs), event=ev, service_day=day))
    return legs, pending


def generate_candidates(
    legs: list[Leg],
    packages: list[Package],
    templates: dict[str, Template],
    sday_index,
    transfer_rules=(),
) -> list[Candidate]:
    """枚举全部合法候选，按确定性顺序返回（保证并列结果可复现）。"""
    valid_pairs = frozenset(
        (r.from_station, r.to_station) for r in transfer_rules)
    out: list[Candidate] = []
    n = len(legs)
    for pkg in sorted(packages, key=lambda p: p.package_id):
        tpl = templates[pkg.template_id]
        k = tpl.size
        if k > n:
            continue
        # 连续程窗口：套餐覆盖相邻的 k 程，不允许跳过中间程
        for start in range(n - k + 1):
            chosen = tuple(legs[start:start + k])
            if _template_matches(tpl, chosen, valid_pairs):
                out.append(Candidate(package=pkg, template=tpl, legs=chosen))
    out.sort(key=lambda c: (
        tuple(l.index for l in c.legs),
        c.package.price,
        c.package.package_id,
    ))
    return out
