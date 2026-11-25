# -*- coding: utf-8 -*-
"""最优计费求解器（精确解，非贪心）。

约束建模：
  * 每程要么按单程计费，要么恰好属于一个套餐；套餐之间腿集合不得重叠。
  * 套餐消耗权益：每次消耗 1 个“权益实例”，记在首程服务日（跨日套餐同此规则）。
  * 权益额度三类键：
      - 个人×服务日  ("P", pax, day,  ent)
      - 个人×跨日    ("P", pax, None, ent)
      - 共享×服务日  ("G", day,  ent)；共享×跨日 ("G", None, ent)
    一次消耗会同时扣减所有适用的额度键；没有任何适用额度键的候选直接不可行。
      —— 这保证“共享权益”不能被某一程先到先占：额度在全局组合优化中统一分配。

算法：
  * 个人额度随 DFS 状态走（记忆化 + 按“第一未覆盖程”分支），共享额度用消费向量表示；
  * 每个 DFS 节点保留关于 (费用, 共享消费向量) 的 Pareto 前沿；
  * 存在共享额度时，再用全局资源 DP 合并各乘客前沿，得到跨乘客竞争下的全局最优；
  * 无共享额度时退化为各乘客独立取最小费用。
全程整数运算，结果确定可复现（并列时取候选序最前者）。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .templates import Candidate, Leg


# ----------------------------- 额度存储 -----------------------------

class QuotaStore:
    def __init__(self, quotas):
        self._limits: dict[tuple, int] = {}
        shared_keys: list[tuple] = []
        for q in quotas:
            if q.passenger_id is None:
                key = ("G", q.service_day, q.entitlement)
                shared_keys.append(key)
            else:
                key = ("P", q.passenger_id, q.service_day, q.entitlement)
            if key in self._limits:
                raise ValueError(f"额度规则重复: {key}")
            self._limits[key] = q.limit
        # 共享键稳定排序，构成全局消费向量的维度顺序
        self.shared_keys = tuple(sorted(set(shared_keys)))
        self.shared_index = {k: i for i, k in enumerate(self.shared_keys)}

    def personal_keys(self, passenger_id: str) -> tuple[tuple, ...]:
        return tuple(sorted(k for k in self._limits if k[0] == "P" and k[1] == passenger_id))

    def initial_rem(self, personal_keys) -> tuple[tuple[tuple, int], ...]:
        return tuple((k, self._limits[k]) for k in personal_keys)

    def shared_limit(self, key: tuple) -> int:
        return self._limits[key]

    def limit_of(self, key: tuple) -> int:
        return self._limits[key]

    def applicable_personal(self, passenger_id: str, ent: str, day: str) -> tuple[tuple, ...]:
        out = []
        for k in self._limits:
            if k[0] != "P" or k[1] != passenger_id or k[3] != ent:
                continue
            if k[2] is None or k[2] == day:
                out.append(k)
        return tuple(sorted(out))

    def applicable_shared(self, ent: str, day: str) -> tuple[tuple, ...]:
        out = []
        for k in self.shared_keys:
            if k[2] != ent:
                continue
            if k[1] is None or k[1] == day:
                out.append(k)
        return tuple(out)

    def describe(self, key: tuple) -> dict:
        if key[0] == "P":
            return {"scope": "personal", "passenger_id": key[1],
                    "service_day": key[2], "entitlement": key[3]}
        return {"scope": "shared", "passenger_id": None,
                "service_day": key[1], "entitlement": key[2]}


# ----------------------------- 解状态 -----------------------------

@dataclass(frozen=True)
class State:
    """DFS 节点的一个 Pareto 最优方案（从该节点向下的完整安排）。"""
    cost: int
    usage: tuple[int, ...]          # 共享消费向量
    action: str                     # 'done' | 'single' | 'package'
    leg_index: Optional[int]
    candidate_key: Optional[tuple]
    personal_delta: tuple[tuple, ...]   # 本步扣减的个人额度键
    rest: Optional["State"]


def _prune(states: list[State]) -> list[State]:
    """保留关于 (cost, usage) 的非支配方案；完全相同的方案去重。"""
    ordered = sorted(states, key=lambda s: (s.cost, s.usage))
    keep: list[State] = []
    for s in ordered:
        dominated = any(
            k.cost <= s.cost and all(a <= b for a, b in zip(k.usage, s.usage))
            for k in keep
        )
        if not dominated:
            keep.append(s)
    return keep


# ----------------------------- 单乘客求解 -----------------------------

@dataclass(frozen=True)
class ChargedCandidate:
    candidate: Candidate
    mask: int
    personal_delta: tuple[tuple, ...]   # 适用的个人额度键
    shared_delta: tuple[int, ...]       # 共享消费向量增量


class PassengerSolver:
    def __init__(self, passenger_id: str, legs: list[Leg],
                 candidates: list[Candidate], quotas: QuotaStore):
        self.pax = passenger_id
        self.legs = legs
        self.quotas = quotas
        self.n = len(legs)
        self.personal_keys = quotas.personal_keys(passenger_id)
        self.shared_dim = len(quotas.shared_keys)
        self._by_key: dict[tuple, ChargedCandidate] = {}

        self.charged: list[ChargedCandidate] = []
        for cand in candidates:
            cc = self._prepare(cand)
            if cc is not None:
                self.charged.append(cc)
                self._by_key[cand.key] = cc
        # 以每条腿为最小腿索引的候选，供 DFS 分支
        self._starting: dict[int, list[ChargedCandidate]] = {}
        for cc in self.charged:
            first = min(i for i in range(self.n) if cc.mask & (1 << i))
            self._starting.setdefault(first, []).append(cc)

    def _prepare(self, cand: Candidate) -> Optional[ChargedCandidate]:
        mask = 0
        for l in cand.legs:
            mask |= 1 << l.index
        ent = cand.template.entitlement
        if ent is None:
            personal_delta: tuple[tuple, ...] = tuple()
            shared_delta = tuple(0 for _ in range(self.shared_dim))
            return ChargedCandidate(cand, mask, personal_delta, shared_delta)
        day = cand.legs[0].service_day  # 跨日套餐扣首程服务日
        p_keys = self.quotas.applicable_personal(self.pax, ent, day)
        g_keys = self.quotas.applicable_shared(ent, day)
        if not p_keys and not g_keys:
            return None  # 权益无处可扣 -> 候选不可行
        delta = [0] * self.shared_dim
        for k in g_keys:
            delta[self.quotas.shared_index[k]] = 1
        return ChargedCandidate(cand, mask, p_keys, tuple(delta))

    def frontier(self) -> list[State]:
        if self.n == 0:
            return [State(
                cost=0, usage=tuple(0 for _ in range(self.shared_dim)),
                action="done", leg_index=None, candidate_key=None,
                personal_delta=tuple(), rest=None)]
        done = State(
            cost=0, usage=tuple(0 for _ in range(self.shared_dim)),
            action="done", leg_index=None, candidate_key=None,
            personal_delta=tuple(), rest=None)

        @lru_cache(maxsize=None)
        def solve(mask: int, rem_tuple: tuple) -> tuple[State, ...]:
            if mask == (1 << self.n) - 1:
                return (done,)
            i = next(j for j in range(self.n) if not (mask & (1 << j)))
            found: list[State] = []

            # 分支 1：第 i 程按单程
            for sub in solve(mask | (1 << i), rem_tuple):
                found.append(State(
                    cost=self.legs[i].event.fare + sub.cost,
                    usage=sub.usage,
                    action="single", leg_index=i, candidate_key=None,
                    personal_delta=tuple(), rest=sub,
                ))

            # 分支 2：用一个覆盖第 i 程且与已覆盖腿不相交的套餐
            rem = dict(rem_tuple)
            for cc in self._starting.get(i, ()):
                if cc.mask & mask:
                    continue
                # 个人额度可行性
                new_rem = rem
                ok = True
                for k in cc.personal_delta:
                    if rem.get(k, 0) <= 0:
                        ok = False
                        break
                if not ok:
                    continue
                if cc.personal_delta:
                    new_rem = dict(rem)
                    for k in cc.personal_delta:
                        new_rem[k] -= 1
                sub_states = solve(mask | cc.mask, tuple(sorted(new_rem.items())))
                for sub in sub_states:
                    found.append(State(
                        cost=cc.candidate.package.price + sub.cost,
                        usage=tuple(a + b for a, b in zip(cc.shared_delta, sub.usage)),
                        action="package", leg_index=i,
                        candidate_key=cc.candidate.key,
                        personal_delta=cc.personal_delta, rest=sub,
                    ))
            return tuple(_prune(found))

        root = solve(0, self.quotas.initial_rem(self.personal_keys))
        solve.cache_clear()
        return list(root)

    def charged_by_key(self, key: tuple) -> ChargedCandidate:
        return self._by_key[key]


# ----------------------------- 全局合并（共享额度竞争）-----------------------------

@dataclass(frozen=True)
class GlobalState:
    usage: tuple[int, ...]
    cost: int
    choices: tuple[Optional[State], ...]  # 与乘客顺序对齐


def _prune_global(states: list[GlobalState]) -> list[GlobalState]:
    ordered = sorted(states, key=lambda s: (s.cost, s.usage))
    keep: list[GlobalState] = []
    for s in ordered:
        dominated = any(
            k.cost <= s.cost and all(a <= b for a, b in zip(k.usage, s.usage))
            for k in keep
        )
        if not dominated:
            keep.append(s)
    return keep


def combine_frontiers(
    passenger_ids: list[str],
    frontiers: dict[str, list[State]],
    shared_limits: tuple[int, ...],
) -> dict[str, State]:
    """合并各乘客 Pareto 前沿，遵守共享额度向量上限，返回全局最优选择。"""
    if not shared_limits:
        # 无共享权益：各乘客独立取最小费用（usage 恒为零向量）
        return {p: min(frontiers[p], key=lambda s: (s.cost, s.usage))
                for p in passenger_ids}

    dim = len(shared_limits)
    zero = tuple(0 for _ in range(dim))
    dp: list[GlobalState] = [GlobalState(zero, 0, tuple())]
    for pax in passenger_ids:
        nxt: list[GlobalState] = []
        for gs in dp:
            for st in frontiers[pax]:
                usage = tuple(a + b for a, b in zip(gs.usage, st.usage))
                if any(u > lim for u, lim in zip(usage, shared_limits)):
                    continue
                nxt.append(GlobalState(usage, gs.cost + st.cost,
                                       gs.choices + (st,)))
        dp = _prune_global(nxt)
        if not dp:
            raise RuntimeError(f"共享额度约束下无可行方案（乘客 {pax} 附近）")
    best = min(dp, key=lambda s: (s.cost, s.usage))
    return dict(zip(passenger_ids, best.choices))
