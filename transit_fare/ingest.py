# -*- coding: utf-8 -*-
"""事件摄入：按稳定 event_id 去重；同 ID 但字段不一致视为冲突，整事件拒绝。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Event

# 冲突判断所比较的业务字段（event_id 之外的全部稳定字段）
_EVENT_FIELDS = (
    "passenger_id", "ticket_type", "time", "line", "direction",
    "board_station", "alight_station", "fare",
)


def _signature(ev: Event) -> tuple:
    return tuple(getattr(ev, f) for f in _EVENT_FIELDS)


@dataclass
class IngestionResult:
    accepted: list[Event] = field(default_factory=list)          # 本批新接收
    duplicate_ids: list[str] = field(default_factory=list)      # 同 ID 且内容一致：重送去重
    conflict_ids: list[str] = field(default_factory=list)       # 同 ID 但内容冲突：拒绝
    conflict_detail: dict[str, dict] = field(default_factory=dict)


def ingest_events(
    batch: list[Event],
    known: dict[str, Event],
) -> IngestionResult:
    """把一批事件并入已知事件表（known 会被原地更新）。

    * 同 event_id 且全部业务字段一致 -> 重传，去重，不计入 accepted。
    * 同 event_id 但任一字段不同     -> 冲突，拒绝，不覆盖旧事件。
    * 新 event_id                    -> 接收。
    """
    res = IngestionResult()
    for ev in batch:
        old = known.get(ev.event_id)
        if old is None:
            known[ev.event_id] = ev
            res.accepted.append(ev)
        elif _signature(old) == _signature(ev):
            res.duplicate_ids.append(ev.event_id)
        else:
            res.conflict_ids.append(ev.event_id)
            res.conflict_detail[ev.event_id] = {
                "existing": {f: str(getattr(old, f)) for f in _EVENT_FIELDS},
                "rejected": {f: str(getattr(ev, f)) for f in _EVENT_FIELDS},
            }
    return res
