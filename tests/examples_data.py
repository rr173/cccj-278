# -*- coding: utf-8 -*-
"""四个手算例的数据构造与手工推导期望值（金额单位：分）。

例 1 共享权益竞争（个人额度版 + 跨乘客共享池版）
例 2 跨午夜夜班（同服务日判定 + 日历空档待核）
例 3 同名异站（稳定站 ID 不匹配则换乘不成立）
例 4 补传事件使原最优组合失效（账版只增不改）
"""
from __future__ import annotations

from datetime import date, timedelta


def _shift_day(day: str, n: int) -> str:
    y, m, d = map(int, day.split("-"))
    return str(date(y, m, d) + timedelta(days=n))


def calendar(day: str = "2026-11-24", start_hour: int = 3) -> dict:
    # 服务日从凌晨 3:00 到次日凌晨 2:59:59，可容纳夜班跨自然日
    return {
        "service_day": day,
        "start": f"{day}T{start_hour:02d}:00:00+08:00",
        "end": _shift_day(day, 1) + "T02:59:59+08:00",
    }


def stations(*pairs) -> list[dict]:
    return [{"station_id": sid, "name": name} for sid, name in pairs]


def event(eid, pax, t, line, direction, board, alight, fare,
          ticket="regular") -> dict:
    return {
        "event_id": eid, "passenger_id": pax, "ticket_type": ticket,
        "time": t, "line": line, "direction": direction,
        "board_station": board, "alight_station": alight, "fare": fare,
    }


# ----------------------------- 例 1：共享权益竞争 -----------------------------

def _chain_templates() -> list[dict]:
    """沿 A->B->C->D->E 链的任意相邻换乘都合法。

    模板用哨兵规则（__ANY__）声明“这里必须是一次有向换乘”，具体合法站对
    由 transfers 表枚举：因此 (e1,e3) 这种 A->C 的跳跃组合会被拒绝。"""
    return [{
        "template_id": "T2", "size": 2,
        "max_elapsed_minutes": 120,
        "transfers": [{"from_station": "__ANY__", "to_station": "__ANY__"}],
        "directions": [None, None],
        "ticket_types": [None, None],
        "same_service_day": True,
        "entitlement": "E",
    }]


def _chain_stations_transfers() -> tuple[list[dict], list[dict]]:
    """换乘关系语义：前一程“下车站” -> 后一程“上车站”。

    线路区间 A->B、B->C、C->D、D->E 意味着相邻程在 B/C/D/E 同站换乘，
    因此换乘表枚举同站有向对 (B,B)(C,C)(D,D)(E 无后继)；
    跳跃组合如 e1->e3 是 B->D，换乘表中不存在，被拒绝。"""
    st = stations(("A", "甲站"), ("B", "乙站"), ("C", "丙站"),
                  ("D", "丁站"), ("E", "戊站"))
    tr = [
        {"from_station": "B", "to_station": "B"},
        {"from_station": "C", "to_station": "C"},
        {"from_station": "D", "to_station": "D"},
    ]
    return st, tr


def example1_personal(limit: int) -> dict:
    """乘客 P1 四程，E 权益每服务日 limit 次，全局组合优化。

    单程票价：e1=300 e2=400 e3=300 e4=500（合计 1500）；
    合法套餐（固定价 500，分摊 250/250，耗 E 一次）：
        X1=(e1,e2) 替 700，省 200
        X2=(e2,e3) 替 700，省 200   —— 与 X1 互斥（都占 e2）
        X3=(e3,e4) 替 800，省 300
    (e1,e3) 等跳跃组合因 transfers 表无 A->C 站对而非法。

    额度=1 时：
      逐程先到先占：扫到 e1 即占用 X1（省 200），总额 1300；
      全局最优：比较后选 X3（省 300），总额 1200 —— 直接证伪先到先占。
    """
    st, tr = _chain_stations_transfers()
    return {
        "calendars": [calendar()],
        "stations": st,
        "transfers": tr,
        "templates": _chain_templates(),
        "packages": [{
            "package_id": "X", "template_id": "T2",
            "price": 500, "allocations": [250, 250],
        }],
        "quotas": [
            {"passenger_id": "P1", "service_day": "2026-11-24",
             "entitlement": "E", "limit": limit},
        ],
        "events": [
            event("e1", "P1", "2026-11-24T08:00:00+08:00", "L1", "up", "A", "B", 300),
            event("e2", "P1", "2026-11-24T08:30:00+08:00", "L1", "up", "B", "C", 400),
            event("e3", "P1", "2026-11-24T09:00:00+08:00", "L1", "up", "C", "D", 300),
            event("e4", "P1", "2026-11-24T09:30:00+08:00", "L1", "up", "D", "E", 500),
        ],
    }


def example1_global_pool() -> dict:
    """跨乘客共享池：P1、P2 各四程，竞争全局 E 额度（当天仅 2 次）。

    每名乘客最优都想同时拿两个套餐（共需 4 次 E），池子只有 2 次。
    全局 DP 必须在两名乘客间分配这 2 次额度，使总费用最低。
    """
    st, tr = _chain_stations_transfers()
    return {
        "calendars": [calendar()],
        "stations": st,
        "transfers": tr,
        "templates": _chain_templates(),
        "packages": [{
            "package_id": "X", "template_id": "T2",
            "price": 500, "allocations": [250, 250],
        }],
        "quotas": [
            {"passenger_id": None, "service_day": "2026-11-24",
             "entitlement": "E", "limit": 2},
        ],
        "events": [
            event("e1", "P1", "2026-11-24T08:00:00+08:00", "L1", "up", "A", "B", 300),
            event("e2", "P1", "2026-11-24T08:30:00+08:00", "L1", "up", "B", "C", 400),
            event("e3", "P1", "2026-11-24T09:00:00+08:00", "L1", "up", "C", "D", 300),
            event("e4", "P1", "2026-11-24T09:30:00+08:00", "L1", "up", "D", "E", 500),
            event("f1", "P2", "2026-11-24T10:00:00+08:00", "L2", "up", "A", "B", 300),
            event("f2", "P2", "2026-11-24T10:30:00+08:00", "L2", "up", "B", "C", 400),
            event("f3", "P2", "2026-11-24T11:00:00+08:00", "L2", "up", "C", "D", 300),
            event("f4", "P2", "2026-11-24T11:30:00+08:00", "L2", "up", "D", "E", 500),
        ],
    }


# ----------------------------- 例 2：跨午夜 -----------------------------

def example2_cross_midnight() -> dict:
    """夜班服务日 2026-11-24：23:30 与次日 00:20 同属一个服务日。

    手算：
      e1(23:30, X->Y, 400) + e2(次日 00:20, Y->Z, 300)，经过 50 分钟 <= 90；
      套餐 N 固定价 600（分摊 300/300），耗 NQ，扣首程服务日 2026-11-24 一次。
      e3 在 03:30，处于两个日历的空档（首日 02:59:59 结束、次日 04:00 才开始），
      无法解释归属 -> 待核，不猜测，不参与计费。
    """
    return {
        "calendars": [
            {
                "service_day": "2026-11-24",
                "start": "2026-11-24T03:00:00+08:00",
                "end": "2026-11-25T02:59:59+08:00",
            },
            {
                "service_day": "2026-11-25",
                "start": "2026-11-25T04:00:00+08:00",
                "end": "2026-11-26T02:59:59+08:00",
            },
        ],
        "stations": stations(("X", "夜班首站"), ("Y", "夜班换乘站"),
                              ("Z", "夜班末站"), ("Q", "空档站")),
        "transfers": [
            {"from_station": "Y", "to_station": "Y"},
        ],
        "templates": [{
            "template_id": "TN", "size": 2,
            "max_elapsed_minutes": 90,
            "transfers": [{"from_station": "Y", "to_station": "Y"}],
            "directions": [None, None],
            "ticket_types": [None, None],
            "same_service_day": True,
            "entitlement": "NQ",
        }],
        "packages": [{
            "package_id": "N", "template_id": "TN",
            "price": 600, "allocations": [300, 300],
        }],
        "quotas": [
            {"passenger_id": "P9", "service_day": "2026-11-24",
             "entitlement": "NQ", "limit": 1},
        ],
        "events": [
            event("e1", "P9", "2026-11-24T23:30:00+08:00", "N1", "down", "X", "Y", 400),
            event("e2", "P9", "2026-11-25T00:20:00+08:00", "N1", "down", "Y", "Z", 300),
            event("e3", "P9", "2026-11-25T03:30:00+08:00", "N1", "down", "Q", "Z", 100),
        ],
    }


# ----------------------------- 例 3：同名异站 -----------------------------

def example3_same_name() -> dict:
    """C1、C2 都叫“中心站”，但稳定站 ID 不同。

    手算：
      e1: A -> C1（300），e2: C2 -> D（300）。
      模板要求 C1 -> D 的有向换乘；实际相邻程是 C2 -> D，稳定 ID 不匹配，
      套餐非法（按站名匹配会错误放行）。正确结果：两程全单程，合计 600，不耗 R。
    """
    return {
        "calendars": [calendar()],
        "stations": stations(("A", "A 站"), ("C1", "中心站"),
                              ("C2", "中心站"), ("D", "D 站")),
        "transfers": [
            {"from_station": "C1", "to_station": "C1"},
        ],
        "templates": [{
            "template_id": "TR", "size": 2,
            "max_elapsed_minutes": 60,
            "transfers": [{"from_station": "C1", "to_station": "C1"}],
            "directions": [None, None],
            "ticket_types": [None, None],
            "same_service_day": True,
            "entitlement": "R",
        }],
        "packages": [{
            "package_id": "RPK", "template_id": "TR",
            "price": 500, "allocations": [250, 250],
        }],
        "quotas": [
            {"passenger_id": "P7", "service_day": "2026-11-24",
             "entitlement": "R", "limit": 1},
        ],
        "events": [
            event("e1", "P7", "2026-11-24T08:00:00+08:00", "L1", "up", "A", "C1", 300),
            event("e2", "P7", "2026-11-24T08:20:00+08:00", "L1", "up", "C2", "D", 300),
        ],
    }


def example3_same_name_ok() -> dict:
    """对照组：把 e2 上车站改成 C1（同名同 ID），套餐即合法，总费 500。"""
    raw = example3_same_name()
    raw["events"][1]["board_station"] = "C1"
    return raw


# ----------------------------- 例 4：补传改变最优组合 -----------------------------

def example4_base() -> dict:
    """首批三程 A->B->C->D：e1 方向 up，e2/e3 方向 down。

    票制：
      P2（2 程，300，分摊 150/150）：要求方向 [up, down]，不耗权益；
      P3（3 程，480，分摊 170/170/140）：方向不限，不耗权益。
    手算 v1：
      合法 P2 只有 (e1,e2)（e2,e3 是 down/down 不匹配 [up,down]）；
      P3=(e1,e2,e3) 合法。
      方案比较：P3 一票到底 = 480；P2+e3 单程 = 300+200 = 500。
      v1 全局最优 = 480，取 P3（e1/e2/e3 分摊 170/170/140）。
    """
    return {
        "calendars": [calendar()],
        "stations": stations(("A", "甲站"), ("B", "乙站"),
                              ("C", "丙站"), ("D", "丁站"), ("E", "戊站")),
        "transfers": [
            {"from_station": "B", "to_station": "B"},
            {"from_station": "C", "to_station": "C"},
            {"from_station": "D", "to_station": "D"},
        ],
        "templates": [
            {
                "template_id": "T2", "size": 2,
                "max_elapsed_minutes": 120,
                "transfers": [{"from_station": "__ANY__", "to_station": "__ANY__"}],
                "directions": ["up", "down"],
                "ticket_types": [None, None],
                "same_service_day": True,
                "entitlement": None,
            },
            {
                "template_id": "T3", "size": 3,
                "max_elapsed_minutes": 180,
                "transfers": [
                    {"from_station": "__ANY__", "to_station": "__ANY__"},
                    {"from_station": "__ANY__", "to_station": "__ANY__"},
                ],
                "directions": [None, None, None],
                "ticket_types": [None, None, None],
                "same_service_day": True,
                "entitlement": None,
            },
        ],
        "packages": [
            {"package_id": "P2", "template_id": "T2",
             "price": 300, "allocations": [150, 150]},
            {"package_id": "P3", "template_id": "T3",
             "price": 480, "allocations": [170, 170, 140]},
        ],
        "quotas": [],
        "events": [
            event("e1", "P3", "2026-11-24T08:00:00+08:00", "L1", "up", "A", "B", 200),
            event("e2", "P3", "2026-11-24T08:30:00+08:00", "L1", "down", "B", "C", 200),
            event("e3", "P3", "2026-11-24T09:00:00+08:00", "L1", "down", "C", "D", 200),
        ],
    }


def example4_supplement() -> dict:
    """补传 e4：方向 up，D->E，单程 350，在 D 站与 e3 同站换乘。

    手算 v2（四程，方向序列 up,down,down,up）：
      P2（[up,down]）合法对只有 (e1,e2)。
      P3 合法连续三元组：(e1,e2,e3) 与 (e2,e3,e4)（方向不限，换乘链均成立，
      时间跨度均 <=180 分钟）。
      关键互斥：P3=(e1,e2,e3) 与 P2=(e1,e2) 都占 e1/e2；
      最优组合：P2=(e1,e2) 300 + e3 单程 200 + e4 单程 350 = 850；
              或 P3=(e2,e3,e4) 480 + e1 单程 200 = 680；
              或 P3=(e1,e2,e3) 480 + e4 单程 350 = 830。
      v2 全局最优 = 680（P3 包 e2,e3,e4 + e1 单程），与 v1 的 480 不同，
      且逐程分摊发生变化；旧账 v1 文件保留，新账 v2 以 supersedes=1 落盘。
    """
    return {
        "events": [
            event("e4", "P3", "2026-11-24T09:30:00+08:00", "L1", "up", "D", "E", 350),
        ],
    }
