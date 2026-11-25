# -*- coding: utf-8 -*-
"""对拍测试：独立暴力枚举所有不相交套餐子集，与 DFS 求解器比较。

暴力器不共享求解器任何代码路径，只依赖候选生成（已由模板规则保证合法性）。
"""
import random
import sys
import unittest
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transit_fare.billing import price_dataset
from transit_fare.calendar import ServiceDayIndex
from transit_fare.models import parse_dataset
from transit_fare.solver import QuotaStore, PassengerSolver
from transit_fare.templates import build_legs, generate_candidates


def brute_force_best(raw: dict) -> int:
    """完全独立的参考实现：枚举套餐子集 -> 校验不相交/额度 -> 取最小费用。"""
    ds = parse_dataset(raw)
    sday = ServiceDayIndex(ds.calendars)
    quotas = QuotaStore(ds.quotas)
    total = 0
    grouped: dict[str, list] = {}
    for ev in ds.events:
        grouped.setdefault(ev.passenger_id, []).append(ev)

    best_overall = None
    # 共享额度向量
    gkeys = quotas.shared_keys
    glim = [quotas.shared_limit(k) for k in gkeys]

    def pax_options(pax, events):
        legs, pending = build_legs(events, sday)
        assert not pending, "对拍数据要求全部事件可解释"
        cands = generate_candidates(
            legs, ds.packages, ds.templates, sday, ds.transfers)
        single_total = sum(l.event.fare for l in legs)
        options = [(single_total, (), (), tuple(0 for _ in gkeys))]
        # 枚举所有套餐子集
        for r in range(1, len(cands) + 1):
            for subset in combinations(cands, r):
                mask = 0
                ok = True
                covered_fare = 0
                pkg_price = 0
                p_charge: dict[tuple, int] = {}
                g_use = [0] * len(gkeys)
                for c in subset:
                    cm = 0
                    for l in c.legs:
                        cm |= 1 << l.index
                        covered_fare += l.event.fare
                    if cm & mask:
                        ok = False
                        break
                    mask |= cm
                    pkg_price += c.package.price
                    if c.template.entitlement is not None:
                        day = c.legs[0].service_day
                        pk = quotas.applicable_personal(
                            pax, c.template.entitlement, day)
                        gk = quotas.applicable_shared(
                            c.template.entitlement, day)
                        if not pk and not gk:
                            ok = False
                            break
                        for k in pk:
                            p_charge[k] = p_charge.get(k, 0) + 1
                        for k in gk:
                            g_use[quotas.shared_index[k]] += 1
                if not ok:
                    continue
                # 个人额度
                for k, used in p_charge.items():
                    if used > quotas.limit_of(k):  # noqa: SLF001
                        ok = False
                        break
                if not ok:
                    continue
                cost = single_total - covered_fare + pkg_price
                options.append((cost,
                                tuple(c.package.package_id for c in subset),
                                tuple(sorted(p_charge.items())),
                                tuple(g_use)))
        return legs, options

    all_options = []
    for pax in sorted(grouped):
        legs, options = pax_options(pax, grouped[pax])
        total += sum(l.event.fare for l in legs)
        all_options.append(options)

    # 笛卡尔积 + 共享额度校验
    def recurse(i, g_used, cost):
        nonlocal best_overall
        if i == len(all_options):
            if best_overall is None or cost < best_overall:
                best_overall = cost
            return
        for opt_cost, _pkgs, _pc, g_use in all_options[i]:
            nu = [a + b for a, b in zip(g_used, g_use)]
            if any(u > lim for u, lim in zip(nu, glim)):
                continue
            recurse(i + 1, nu, cost + opt_cost)

    recurse(0, [0] * len(gkeys), 0)
    return best_overall


def random_dataset(seed: int) -> dict:
    rng = random.Random(seed)
    n_pax = rng.randint(1, 3)
    n_legs = rng.randint(1, 7)
    stations = [f"S{i}" for i in range(rng.randint(2, 5))]
    events = []
    eid = 0
    for p in range(n_pax):
        pax = f"P{p}"
        cur = rng.randrange(len(stations) - 1)
        minute = 8 * 60
        for _ in range(n_legs):
            nxt = rng.randrange(len(stations))
            minute += rng.randint(10, 40)
            events.append({
                "event_id": f"e{eid}", "passenger_id": pax,
                "ticket_type": rng.choice(["regular", "concession"]),
                "time": f"2026-11-24T{minute // 60:02d}:{minute % 60:02d}:00+08:00",
                "line": rng.choice(["L1", "L2"]),
                "direction": rng.choice(["up", "down"]),
                "board_station": stations[cur],
                "alight_station": stations[nxt],
                "fare": rng.randint(100, 600),
            })
            cur = nxt
            eid += 1
    # 同站换乘表
    transfers = [{"from_station": s, "to_station": s} for s in stations]
    use_ent = rng.random() < 0.7
    shared = rng.random() < 0.4
    use_t3 = rng.random() < 0.5
    quotas = []
    if use_ent:
        for p in range(n_pax):
            if not shared:
                quotas.append({"passenger_id": f"P{p}",
                               "service_day": "2026-11-24",
                               "entitlement": "E",
                               "limit": rng.randint(0, 3)})
        if shared:
            quotas.append({"passenger_id": None,
                           "service_day": "2026-11-24",
                           "entitlement": "E",
                           "limit": rng.randint(0, n_pax + 1)})
    templates = [{
        "template_id": "T2", "size": 2,
        "max_elapsed_minutes": rng.choice([60, 120, 240]),
        "transfers": [{"from_station": "__ANY__", "to_station": "__ANY__"}],
        "directions": [None, None],
        "ticket_types": [None, None],
        "same_service_day": True,
        "entitlement": "E" if use_ent else None,
    }]
    packages = [{
        "package_id": "X", "template_id": "T2",
        "price": rng.randint(200, 700),
        "allocations": [0, 0],
    }]
    if use_t3:
        templates.append({
            "template_id": "T3", "size": 3,
            "max_elapsed_minutes": rng.choice([120, 240, 400]),
            "transfers": [
                {"from_station": "__ANY__", "to_station": "__ANY__"},
                {"from_station": "__ANY__", "to_station": "__ANY__"},
            ],
            "directions": [None, None, None],
            "ticket_types": [None, None, None],
            "same_service_day": True,
            "entitlement": "E" if use_ent else None,
        })
        p3 = rng.randint(300, 900)
        packages.append({"package_id": "Y", "template_id": "T3",
                         "price": p3, "allocations": [0, 0, 0]})
    return {
        "calendars": [{
            "service_day": "2026-11-24",
            "start": "2026-11-24T03:00:00+08:00",
            "end": "2026-11-25T02:59:59+08:00",
        }],
        "stations": [{"station_id": s, "name": s} for s in stations],
        "transfers": transfers,
        "templates": templates,
        "packages": packages,
        "quotas": quotas,
        "events": events,
    }


class BruteForceComparison(unittest.TestCase):
    def test_matches_brute_force_many_cases(self):
        for seed in range(300):
            raw = random_dataset(seed)
            for pkg in raw["packages"]:
                k = len([t for t in raw["templates"]
                         if t["template_id"] == pkg["template_id"]][0]["transfers"]) + 1
                base = pkg["price"] // k
                alloc = [base] * k
                alloc[0] += pkg["price"] - sum(alloc)
                pkg["allocations"] = alloc
            expected = brute_force_best(raw)
            got = price_dataset(parse_dataset(raw))["grand_total_fare"]
            self.assertEqual(
                got, expected,
                msg=f"seed={seed} solver={got} brute={expected}")

    def test_hand_examples_match_brute_force(self):
        from tests import examples_data as ex
        cases = [
            ex.example1_personal(0), ex.example1_personal(1),
            ex.example1_personal(2), ex.example1_global_pool(),
            ex.example3_same_name(), ex.example3_same_name_ok(),
            ex.example4_base(),
        ]
        for raw in cases:
            got = price_dataset(parse_dataset(raw))["grand_total_fare"]
            self.assertEqual(got, brute_force_best(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
