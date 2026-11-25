# -*- coding: utf-8 -*-
"""命令行：

  # 一次性计费（无状态，直接打印账版 JSON）
  python -m transit_fare.cli price input.json [-o bill.json]

  # 补传批次（写不可变账版目录，旧版保留）
  python -m transit_fare.cli batch --ledger ./ledger_dir --input batch.json [--note 备注]
"""
from __future__ import annotations

import argparse
import json
import sys

from .ledger import run_batch
from .models import SchemaError, parse_dataset
from .billing import price_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transit_fare")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_price = sub.add_parser("price", help="对单份输入做一次性计费")
    p_price.add_argument("input")
    p_price.add_argument("-o", "--output")

    p_batch = sub.add_parser("batch", help="补传批次并写账版目录")
    p_batch.add_argument("--ledger", required=True)
    p_batch.add_argument("--input", required=True)
    p_batch.add_argument("--note", default="")

    args = parser.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取输入失败: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "price":
            ds = parse_dataset(raw)
            result = {"bill": price_dataset(ds)}
            text = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as fh:
                    fh.write(text)
            else:
                print(text)
            return 0

        result = run_batch(args.ledger, raw, note=args.note)
        print(json.dumps(
            {"version": result["version"],
             "batch_no": result["batch_no"],
             "ingestion": {
                 "accepted": len(result["ingestion"]["accepted_event_ids"]),
                 "duplicates": len(result["ingestion"]["duplicate_event_ids"]),
                 "conflicts": result["ingestion"]["conflict_event_ids"],
             },
             "grand_total_fare": result["bill"]["grand_total_fare"]},
            ensure_ascii=False, indent=2))
        return 0
    except SchemaError as exc:
        print(f"输入非法: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
