# -*- coding: utf-8 -*-
"""账版管理：补传事件触发重算，只产生新账版；旧账版文件原样保留。

账版目录结构：
    <ledger_dir>/
      state.json              当前累计输入（全部已接受事件 + 票制/日历/额度配置）
      batches/
        batch-0001.json       每批摄入结果（接收/去重/冲突）
      versions/
        ledger-v1.json        账版只增不改；v2 通过 supersedes 指向 v1
        ledger-v2.json
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .billing import price_dataset
from .ingest import ingest_events
from .models import Dataset, Event, parse_dataset


def _event_to_dict(ev: Event) -> dict:
    return {
        "event_id": ev.event_id,
        "passenger_id": ev.passenger_id,
        "ticket_type": ev.ticket_type,
        "time": ev.time.isoformat(),
        "line": ev.line,
        "direction": ev.direction,
        "board_station": ev.board_station,
        "alight_station": ev.alight_station,
        "fare": ev.fare,
    }


def _dataset_to_dict(ds: Dataset) -> dict:
    return {
        "calendars": [asdict(c) | {"start": c.start.isoformat(),
                                   "end": c.end.isoformat()}
                      for c in ds.calendars],
        "stations": [asdict(s) for s in ds.stations.values()],
        "transfers": [asdict(t) for t in ds.transfers],
        "templates": [
            {
                "template_id": t.template_id,
                "size": t.size,
                "max_elapsed_minutes": t.max_elapsed_minutes,
                "transfers": [None if r is None else asdict(r) for r in t.transfers],
                "directions": list(t.directions),
                "ticket_types": list(t.ticket_types),
                "same_service_day": t.same_service_day,
                "entitlement": t.entitlement,
            }
            for t in ds.templates.values()
        ],
        "packages": [asdict(p) for p in ds.packages],
        "quotas": [asdict(q) for q in ds.quotas],
        "events": [_event_to_dict(e) for e in ds.events],
    }


def run_batch(
    ledger_dir: str | Path,
    batch_input: dict,
    *,
    note: str = "",
) -> dict:
    """执行一个补传批次：摄入事件 -> 重算 -> 落新账版。返回写入结果摘要。"""
    root = Path(ledger_dir)
    (root / "batches").mkdir(parents=True, exist_ok=True)
    (root / "versions").mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"

    if state_path.exists():
        state_raw = json.loads(state_path.read_text(encoding="utf-8"))
        # 票制等配置以首批为准；后续批次只取事件（若再传配置，要求与既有配置一致）
        merged = dict(state_raw)
        for section in ("calendars", "stations", "transfers",
                        "templates", "packages", "quotas"):
            if section in batch_input and batch_input[section] != merged.get(section):
                raise ValueError(
                    f"补传批次中的 {section} 与既有账版配置不一致；"
                    "票制配置不可变更，只允许补传事件")
        batch_for_parse = dict(merged)
        batch_for_parse["events"] = batch_input.get("events", [])
    else:
        merged = {k: v for k, v in batch_input.items()}
        batch_for_parse = batch_input

    # 用累计状态构造“已知事件表”，再摄入本批
    known_ds = parse_dataset(merged) if state_path.exists() else Dataset()
    known = {e.event_id: e for e in known_ds.events}
    batch_ds = parse_dataset(batch_for_parse)
    ingestion = ingest_events(batch_ds.events, known)

    accumulated = Dataset(
        calendars=known_ds.calendars or batch_ds.calendars,
        stations=known_ds.stations or batch_ds.stations,
        transfers=known_ds.transfers or batch_ds.transfers,
        templates=known_ds.templates or batch_ds.templates,
        packages=known_ds.packages or batch_ds.packages,
        quotas=known_ds.quotas or batch_ds.quotas,
        events=sorted(known.values(), key=lambda e: (e.time, e.event_id)),
    )

    # 批次序号 & 账版序号只增不减
    existing_batches = sorted((root / "batches").glob("batch-*.json"))
    batch_no = len(existing_batches) + 1
    existing_versions = sorted((root / "versions").glob("ledger-v*.json"))
    version_no = len(existing_versions) + 1

    ingestion_record = {
        "batch_no": batch_no,
        "note": note,
        "accepted_event_ids": [e.event_id for e in ingestion.accepted],
        "duplicate_event_ids": ingestion.duplicate_ids,
        "conflict_event_ids": ingestion.conflict_ids,
        "conflict_detail": ingestion.conflict_detail,
    }
    (root / "batches" / f"batch-{batch_no:04d}.json").write_text(
        json.dumps(ingestion_record, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 重算（只产生新账版；即使没有新事件，也保留独立版本以便审计）
    bill = price_dataset(accumulated)
    ledger_doc = {
        "schema": "transit-fare-ledger/1",
        "version": version_no,
        "supersedes": version_no - 1 if version_no > 1 else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch_no": batch_no,
        "note": note,
        "ingestion": {
            "accepted": len(ingestion.accepted),
            "duplicates": len(ingestion.duplicate_ids),
            "conflicts": len(ingestion.conflict_ids),
        },
        "bill": bill,
    }
    (root / "versions" / f"ledger-v{version_no}.json").write_text(
        json.dumps(ledger_doc, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # state.json 是工作状态，不属于不可变账版
    state_path.write_text(
        json.dumps(_dataset_to_dict(accumulated), ensure_ascii=False, indent=2),
        encoding="utf-8")

    return {
        "ledger_dir": str(root),
        "version": version_no,
        "batch_no": batch_no,
        "ingestion": ingestion_record,
        "bill": bill,
    }
