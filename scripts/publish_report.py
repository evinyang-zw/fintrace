#!/usr/bin/env python3
"""Refresh the provenance block in docs/evaluation-report.md and snapshot a run.

Usage:
  python scripts/publish_report.py \
    --report reports/w4e/graph-2/2026-11-11-graph-s2.json \
    --snapshot reports/w4e/graph-2/2026-11-11-graph-s2.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

START = "<!-- AUTO:PROVENANCE:START -->"
END = "<!-- AUTO:PROVENANCE:END -->"
SNAP_START = "<!-- AUTO:SNAPSHOT:START -->"
SNAP_END = "<!-- AUTO:SNAPSHOT:END -->"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _provenance(dataset: Path, report: dict | None) -> str:
    commit = _git("rev-parse", "--short", "HEAD")
    committed = _git("show", "-s", "--format=%cs", "HEAD")
    agent = str(report.get("agent", "—")) if report else "—"
    judge = str(report.get("judge", "—")) if report else "—"
    generated = str(report.get("generated_at", "—")) if report else "—"
    rows = [
        ("代码版本", f"`{commit}`（{committed}）"),
        ("数据集", f"`{dataset}` · sha256 `{_sha256(dataset)[:16]}…`"),
        ("评测对象", f"`{agent}`"),
        ("判官", f"`{judge}`"),
        ("模型", "`deepseek-flash` · thinking=disabled · temperature=0"),
        ("运行时间", generated),
    ]
    lines = [START, "| 字段 | 值 |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in rows]
    lines.append(END)
    return "\n".join(lines)


def _replace(text: str, start: str, end: str, block: str, path: Path) -> str:
    begin = text.find(start)
    finish = text.find(end)
    if begin == -1 or finish == -1 or finish < begin:
        raise SystemExit(f"{path}: markers {start} / {end} not found")
    return text[:begin] + block + text[finish + len(end) :]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, help="raw eval report .json")
    parser.add_argument("--dataset", type=Path, default=Path("eval/datasets/gold_v2.jsonl"))
    parser.add_argument("--doc", type=Path, default=Path("docs/evaluation-report.md"))
    parser.add_argument("--snapshot", type=Path, help="raw eval report .md to copy")
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else None
    doc = args.doc.read_text(encoding="utf-8")
    doc = _replace(doc, START, END, _provenance(args.dataset, report), args.doc)
    args.doc.write_text(doc, encoding="utf-8")
    print(f"updated provenance in {args.doc}")

    if args.snapshot:
        out = Path("docs/evidence/latest-run.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        label = f"{report.get('agent', '—')} · seed {report.get('seed', '—')}" if report else "—"
        header = (
            f"{SNAP_START}\n"
            f"> 单次运行快照（{label}）。**头条数字是 4 seed 合并，见"
            f" [评测报告](../evaluation-report.md)。**\n"
            f"> 由 `make publish-report` 生成，请勿手改。\n"
            f"{SNAP_END}\n\n"
        )
        out.write_text(header + args.snapshot.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"snapshot -> {out}")


if __name__ == "__main__":
    main()
