# -*- coding: utf-8 -*-
"""手算例与机制测试。以 python -I -m unittest 运行。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transit_fare.billing import price_dataset
from transit_fare.ingest import ingest_events
from transit_fare.ledger import run_batch
from transit_fare.models import Event, SchemaError, parse_dataset
from tests import examples_data as ex


def bill(raw):
    return price_dataset(parse_dataset(raw))


def pax_bill(raw, pax_id):
    b = bill(raw)
    (p,) = [p for p in b["passengers"] if p["passenger_id"] == pax_id]
    return p, b


def leg_map(p):
    return {row["event_id"]: row for row in p["legs"]}


class Example1SharedEntitlement(unittest.TestCase):
    """手算例 1：共享/竞争权益不能逐程先到先占。"""

    def test_limit_1_global_opt_beats_first_come(self):
        # 先到先占：e1 先拿 X1=(e1,e2)，省 200，总费 1500-200=1300
        # 全局最优：X3=(e3,e4) 省 300，总费 1200
        p, _ = pax_bill(ex.example1_personal(limit=1), "P1")
        self.assertEqual(p["total_fare"], 1200)
        lm = leg_map(p)
        self.assertEqual(lm["e1"]["billing"], "single")
        self.assertEqual(lm["e2"]["billing"], "single")
        self.assertEqual(lm["e3"]["billing"], "package")
        self.assertEqual(lm["e4"]["billing"], "package")
        self.assertEqual(lm["e3"]["amount"], 250)
        self.assertEqual(lm["e4"]["amount"], 250)
        # 权益只扣一次，且记在首程 e3 的服务日
        (pkg,) = p["packages"]
        self.assertEqual(pkg["first_service_day"], "2026-11-24")
        self.assertEqual(len(pkg["entitlement_charges"]), 1)
        self.assertEqual(pkg["entitlement_charges"][0]["entitlement"], "E")

    def test_limit_2_non_overlapping_packages(self):
        # 额度 2：X1+X3（互不重叠）省 500；X2 与 X1 互斥无法再加入
        p, _ = pax_bill(ex.example1_personal(limit=2), "P1")
        self.assertEqual(p["total_fare"], 1000)
        used = sorted(
            (row["event_id"], row["billing"]) for row in p["legs"])
        self.assertEqual(dict(used)["e1"], "package")
        self.assertEqual(dict(used)["e2"], "package")
        self.assertEqual(dict(used)["e3"], "package")
        self.assertEqual(dict(used)["e4"], "package")
        self.assertEqual(len(p["packages"]), 2)

    def test_limit_0_all_single(self):
        p, _ = pax_bill(ex.example1_personal(limit=0), "P1")
        self.assertEqual(p["total_fare"], 1500)
        self.assertEqual(p["packages"], [])

    def test_greedy_counterexample_by_construction(self):
        """构造对照：若按事件顺序贪心，会与全局最优不同（直接验证求解器不贪心）。"""
        raw = ex.example1_personal(limit=1)
        # 把 e4 单程提高到 900 -> X3 省 700，更应被全局选择
        raw["events"][3]["fare"] = 900
        p, _ = pax_bill(raw, "P1")
        # 单程合计 300+400+300+900=1900；选 X3 后 = 1900-1200+500=1200
        self.assertEqual(p["total_fare"], 1200)
        self.assertEqual(leg_map(p)["e3"]["billing"], "package")

    def test_global_pool_cross_passenger(self):
        """两名乘客竞争同一个全局 E 池（2 次），全局 DP 统一分配。"""
        b = bill(ex.example1_global_pool())
        self.assertEqual(len(b["passengers"]), 2)
        # 每名单独最优需 2 次 E（两个套餐），池子只够 2 次 -> 全系统恰好两个套餐
        total_pkgs = sum(len(p["packages"]) for p in b["passengers"])
        self.assertEqual(total_pkgs, 2)
        # 每人四程单程 1500；一个套餐替掉两条腿（最少替 500 的 X3 组合省 300）
        # 全局最优 = 3000 - 600 = 2400
        self.assertEqual(b["grand_total_fare"], 2400)
        # 共享维度被记录
        self.assertTrue(b["shared_quota_dimensions"])
        dim = b["shared_quota_dimensions"][0]
        self.assertEqual(dim["scope"], "shared")
        self.assertIsNone(dim["passenger_id"])


class Example2CrossMidnight(unittest.TestCase):
    def test_night_service_package_and_pending_gap(self):
        p, _ = pax_bill(ex.example2_cross_midnight(), "P9")
        # 套餐合法（跨自然日但同服务日），e3 待核不计费
        self.assertEqual(p["total_fare"], 600)
        lm = leg_map(p)
        self.assertEqual(lm["e1"]["service_day"], "2026-11-24")
        self.assertEqual(lm["e2"]["service_day"], "2026-11-24")
        self.assertEqual(lm["e1"]["amount"], 300)
        self.assertEqual(lm["e2"]["amount"], 300)
        (pkg,) = p["packages"]
        # 跨日套餐扣首程服务日
        self.assertEqual(pkg["first_service_day"], "2026-11-24")
        (pending,) = p["pending_events"]
        self.assertEqual(pending["event_id"], "e3")
        self.assertEqual(pending["reason"], "no_service_day")

    def test_max_elapsed_violation_falls_back_to_single(self):
        raw = ex.example2_cross_midnight()
        raw["templates"][0]["max_elapsed_minutes"] = 30  # 实际经过 50 分钟
        p, _ = pax_bill(raw, "P9")
        self.assertEqual(p["total_fare"], 700)  # 400+300，e3 仍待核
        self.assertEqual(p["packages"], [])


class Example3SameNameDifferentStation(unittest.TestCase):
    def test_stable_id_mismatch_blocks_transfer(self):
        p, _ = pax_bill(ex.example3_same_name(), "P7")
        self.assertEqual(p["total_fare"], 600)
        self.assertTrue(all(r["billing"] == "single" for r in p["legs"]))
        self.assertEqual(p["packages"], [])

    def test_control_same_id_allows_package(self):
        p, _ = pax_bill(ex.example3_same_name_ok(), "P7")
        self.assertEqual(p["total_fare"], 500)
        self.assertEqual(len(p["packages"]), 1)


class Example4SupplementChangesOptimum(unittest.TestCase):
    def test_two_ledger_versions_old_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            r1 = run_batch(Path(tmp) / "ledger", ex.example4_base(), note="首批")
            r2 = run_batch(Path(tmp) / "ledger", ex.example4_supplement(),
                           note="补传 e4")
            v1 = json.loads((Path(tmp) / "ledger" / "versions"
                             / "ledger-v1.json").read_text(encoding="utf-8"))
            v2 = json.loads((Path(tmp) / "ledger" / "versions"
                             / "ledger-v2.json").read_text(encoding="utf-8"))

            # v1：P3=(e1,e2,e3) 480 严格优于 P2+e3=500
            self.assertEqual(r1["bill"]["grand_total_fare"], 480)
            self.assertEqual(v1["version"], 1)
            self.assertIsNone(v1["supersedes"])
            lm1 = {r["event_id"]: r for r in v1["bill"]["passengers"][0]["legs"]}
            self.assertEqual(lm1["e1"]["amount"], 170)
            self.assertEqual(lm1["e2"]["amount"], 170)
            self.assertEqual(lm1["e3"]["amount"], 140)

            # v2：最优 680 = e1 单程 200 + P3=(e2,e3,e4) 480
            self.assertEqual(r2["bill"]["grand_total_fare"], 680)
            self.assertEqual(v2["version"], 2)
            self.assertEqual(v2["supersedes"], 1)
            lm2 = {r["event_id"]: r for r in v2["bill"]["passengers"][0]["legs"]}
            self.assertEqual(lm2["e1"]["billing"], "single")
            self.assertEqual(lm2["e1"]["amount"], 200)
            self.assertEqual(lm2["e2"]["billing"], "package")
            self.assertEqual(lm2["e2"]["amount"], 170)
            self.assertEqual(lm2["e3"]["amount"], 170)
            self.assertEqual(lm2["e4"]["amount"], 140)
            self.assertEqual(sum(r["amount"] for r in lm2.values()), 680)

            # 旧账版文件原样保留
            files = sorted(p.name for p in
                           (Path(tmp) / "ledger" / "versions").iterdir())
            self.assertEqual(files, ["ledger-v1.json", "ledger-v2.json"])


class IngestionAndSchema(unittest.TestCase):
    def test_dedup_and_conflict(self):
        raw = ex.example3_same_name()
        ds = parse_dataset(raw)
        known = {e.event_id: e for e in ds.events}
        # 完全重传 -> 去重
        dup = Event(**{**ds.events[0].__dict__})
        r = ingest_events([dup], known)
        self.assertEqual(r.duplicate_ids, ["e1"])
        self.assertEqual(r.accepted, [])
        # 同 ID 改票价 -> 冲突拒绝，旧值保留
        changed = Event(**{**ds.events[0].__dict__, "fare": 999})
        r2 = ingest_events([changed], known)
        self.assertEqual(r2.conflict_ids, ["e1"])
        self.assertEqual(known["e1"].fare, 300)

    def test_allocation_sum_must_equal_price(self):
        raw = ex.example3_same_name()
        raw["packages"][0]["allocations"] = [250, 260]
        with self.assertRaises(SchemaError):
            parse_dataset(raw)

    def test_overlapping_calendars_rejected(self):
        raw = ex.example2_cross_midnight()
        raw["calendars"][1]["start"] = "2026-11-25T02:00:00+08:00"
        with self.assertRaises(SchemaError):
            parse_dataset(raw)

    def test_naive_datetime_rejected(self):
        raw = ex.example3_same_name()
        raw["events"][0]["time"] = "2026-11-24T08:00:00"
        with self.assertRaises(SchemaError):
            parse_dataset(raw)

    def test_every_leg_billed_exactly_once(self):
        raw = ex.example1_personal(limit=2)
        p, _ = pax_bill(raw, "P1")
        self.assertEqual(len(p["legs"]), 4)
        self.assertEqual(sum(r["amount"] for r in p["legs"]), p["total_fare"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
