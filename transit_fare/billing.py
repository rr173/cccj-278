# -*- coding: utf-8 -*-
"""编排：候选生成 -> 带额度约束的全局最优求解 -> 重建逐程金额与权益明细。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .calendar import ServiceDayIndex
from .solver import PassengerSolver, QuotaStore, State, combine_frontiers
from .templates import Leg, build_legs, generate_candidates


@dataclass
class PassengerPlan:
    passenger_id: str
    total_fare: int
    legs: list[Leg]
    pending_events: list          # Event 列表
    leg_lines: list[dict]
    packages_used: list[dict]


def _reconstruct(
    state: State,
    solver: PassengerSolver,
    quotas: QuotaStore,
) -> tuple[list[dict], list[dict]]:
    """沿 State 链重建逐程计费行与套餐权益明细。"""
    leg_lines: dict[int, dict] = {}
    packages_used: list[dict] = []
    cur = state
    while cur.action != "done":
        if cur.action == "single":
            leg = solver.legs[cur.leg_index]
            leg_lines[leg.index] = {
                "event_id": leg.event.event_id,
                "service_day": leg.service_day,
                "time": leg.event.time.isoformat(),
                "line": leg.event.line,
                "direction": leg.event.direction,
                "board_station": leg.event.board_station,
                "alight_station": leg.event.alight_station,
                "billing": "single",
                "amount": leg.event.fare,
                "package_id": None,
                "position": None,
                "entitlements": [],
            }
            cur = cur.rest
            continue

        # package
        cc = solver.charged_by_key(cur.candidate_key)
        cand = cc.candidate
        charges = [quotas.describe(k) for k in cc.personal_delta]
        # 共享维度的实际扣减（delta 非零即扣 1）
        for key, inc in zip(quotas.shared_keys, cc.shared_delta):
            if inc:
                charges.append(quotas.describe(key))
        pkg_record = {
            "package_id": cand.package.package_id,
            "template_id": cand.template.template_id,
            "price": cand.package.price,
            "event_ids": [l.event.event_id for l in cand.legs],
            "first_service_day": cand.legs[0].service_day,
            "entitlement": cand.template.entitlement,
            "entitlement_charges": charges,
        }
        packages_used.append(pkg_record)
        for pos, leg in enumerate(cand.legs):
            leg_lines[leg.index] = {
                "event_id": leg.event.event_id,
                "service_day": leg.service_day,
                "time": leg.event.time.isoformat(),
                "line": leg.event.line,
                "direction": leg.event.direction,
                "board_station": leg.event.board_station,
                "alight_station": leg.event.alight_station,
                "billing": "package",
                "amount": cand.package.allocations[pos],
                "package_id": cand.package.package_id,
                "position": pos + 1,
                "entitlements": charges,
            }
        cur = cur.rest
    return [leg_lines[i] for i in sorted(leg_lines)], packages_used


def price_dataset(dataset) -> dict:
    """对一份已摄入事件全集执行全局最优计费，返回完整账版（dict，可直接 JSON 序列化）。"""
    sday_index = ServiceDayIndex(dataset.calendars)
    quotas = QuotaStore(dataset.quotas)

    # 按乘客分组
    grouped: dict[str, list] = {}
    for ev in dataset.events:
        grouped.setdefault(ev.passenger_id, []).append(ev)

    passenger_ids = sorted(grouped)
    solvers: dict[str, PassengerSolver] = {}
    frontiers: dict[str, list[State]] = {}
    plans: dict[str, PassengerPlan] = {}

    for pax in passenger_ids:
        legs, pending = build_legs(grouped[pax], sday_index)
        candidates = generate_candidates(
            legs, dataset.packages, dataset.templates, sday_index,
            dataset.transfers)
        solver = PassengerSolver(pax, legs, candidates, quotas)
        solvers[pax] = solver
        frontiers[pax] = solver.frontier()
        plans[pax] = PassengerPlan(
            passenger_id=pax, total_fare=0, legs=legs,
            pending_events=pending, leg_lines=[], packages_used=[])

    shared_limits = tuple(
        quotas.shared_limit(k) for k in quotas.shared_keys
    )
    chosen = combine_frontiers(passenger_ids, frontiers, shared_limits)

    passengers_out = []
    grand_total = 0
    for pax in passenger_ids:
        leg_lines, packages_used = _reconstruct(chosen[pax], solvers[pax], quotas)
        total = sum(row["amount"] for row in leg_lines)
        # 校验：每程恰好一次计费；套餐分摊和 = 套餐价
        assert len(leg_lines) == len(plans[pax].legs)
        for rec in packages_used:
            assert sum(
                row["amount"] for row in leg_lines
                if row.get("package_id") == rec["package_id"]
                and row["event_id"] in rec["event_ids"]
            ) == rec["price"]
        plans[pax].total_fare = total
        plans[pax].leg_lines = leg_lines
        plans[pax].packages_used = packages_used
        grand_total += total
        passengers_out.append({
            "passenger_id": pax,
            "total_fare": total,
            "pending_events": [
                {
                    "event_id": ev.event_id,
                    "reason": "no_service_day",
                    "detail": "事件时刻不属于任何已声明服务日日历，待核，不猜测归属",
                    "time": ev.time.isoformat(),
                }
                for ev in plans[pax].pending_events
            ],
            "legs": leg_lines,
            "packages": packages_used,
        })

    return {
        "grand_total_fare": grand_total,
        "shared_quota_dimensions": [quotas.describe(k) for k in quotas.shared_keys],
        "passengers": passengers_out,
    }
