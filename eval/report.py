"""eval/report.py — Render a run_report.jsonl as a Rich table + optional CSV.

Usage:
    python3.11 -m eval.report eval/run_reports/run_20260303.jsonl [--csv] [--compare older.jsonl]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text

console = Console()


def _load_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _score_style(s: float | None) -> tuple[str, str]:
    """Return (symbol, color) based on score threshold."""
    if s is None:
        return "-", "white"
    if s >= 0.8:
        return "✓", "green"
    if s >= 0.6:
        return "~", "yellow"
    return "✗", "red"


def _fmt_score(s: float | None, show_symbol: bool = False) -> Text:
    if s is None:
        return Text("—", style="dim")
    symbol, color = _score_style(s)
    text = f"{s:.2f}"
    if show_symbol:
        text += f" {symbol}"
    return Text(text, style=Style(color=color, bold=(s >= 0.8)))


def _fmt_latency(cl: dict | None) -> str:
    if not cl:
        return "—"
    total = cl.get("total_sec")
    if total is None:
        return "—"
    return f"{total:.1f}s"


def _row_data(rec: dict) -> dict:
    m = rec.get("metrics", {})
    return {
        "id": rec.get("prompt_id", "?"),
        "brief": rec.get("brief", "")[:22] + "…",
        "overall": rec.get("overall_score"),
        "pa": (m.get("prompt_adherence") or {}).get("score"),
        "tc": (m.get("temporal_consistency") or {}).get("score"),
        "nc": (m.get("narrative_coherence") or {}).get("score"),
        "vd": (m.get("visual_defects") or {}).get("score"),
        "aa": (m.get("audio_alignment") or {}).get("score"),
        "latency": m.get("cost_latency"),
    }


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def render_table(records: list[dict], compare_records: list[dict] | None = None) -> Table:
    has_compare = bool(compare_records)
    compare_by_id: dict[str, dict] = {}
    if compare_records:
        for r in compare_records:
            compare_by_id[r.get("prompt_id", "")] = _row_data(r)

    table = Table(
        title="Eval Results",
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
    )
    table.add_column("ID", style="dim", width=5)
    table.add_column("Brief", width=24)
    table.add_column("Overall", justify="right")
    table.add_column("PA", justify="right")
    table.add_column("TC", justify="right")
    table.add_column("NC", justify="right")
    table.add_column("VD", justify="right")
    table.add_column("AA", justify="right")
    table.add_column("Latency", justify="right")
    if has_compare:
        table.add_column("Δ Overall", justify="right")

    rows: list[dict] = [_row_data(r) for r in records]

    def _delta_text(current: float | None, prev: float | None) -> Text:
        if current is None or prev is None:
            return Text("—", style="dim")
        d = current - prev
        color = "green" if d > 0.01 else ("red" if d < -0.01 else "dim")
        sign = "+" if d > 0 else ""
        return Text(f"{sign}{d:.2f}", style=color)

    for row in rows:
        cmp = compare_by_id.get(row["id"])
        cells = [
            row["id"],
            row["brief"],
            _fmt_score(row["overall"], show_symbol=True),
            _fmt_score(row["pa"]),
            _fmt_score(row["tc"]),
            _fmt_score(row["nc"]),
            _fmt_score(row["vd"]),
            _fmt_score(row["aa"]),
            _fmt_latency(row["latency"]),
        ]
        if has_compare:
            cells.append(_delta_text(row["overall"], cmp.get("overall") if cmp else None))
        table.add_row(*cells)

    # Average row
    def _col_avg(key: str) -> float | None:
        return _avg([r[key] for r in rows])

    avg_overall = _col_avg("overall")
    avg_pa = _col_avg("pa")
    avg_tc = _col_avg("tc")
    avg_nc = _col_avg("nc")
    avg_vd = _col_avg("vd")
    avg_aa = _col_avg("aa")

    latency_vals = [r["latency"].get("total_sec") for r in rows if r["latency"] and r["latency"].get("total_sec")]
    avg_latency_str = f"{sum(latency_vals)/len(latency_vals):.1f}s" if latency_vals else "—"

    table.add_section()
    avg_cells = [
        Text("AVG", style="bold"),
        "",
        _fmt_score(avg_overall),
        _fmt_score(avg_pa),
        _fmt_score(avg_tc),
        _fmt_score(avg_nc),
        _fmt_score(avg_vd),
        _fmt_score(avg_aa),
        avg_latency_str,
    ]
    if has_compare:
        if compare_records:
            cmp_rows = [_row_data(r) for r in compare_records]
            cmp_avg = _avg([r["overall"] for r in cmp_rows])
        else:
            cmp_avg = None
        avg_cells.append(_delta_text(avg_overall, cmp_avg))
    table.add_row(*avg_cells)

    return table


def write_csv(records: list[dict], out_path: Path) -> None:
    fieldnames = ["prompt_id", "brief", "overall_score", "status",
                  "prompt_adherence", "temporal_consistency", "narrative_coherence",
                  "visual_defects", "audio_alignment",
                  "total_sec", "plan_sec", "execute_sec", "output_size_mb", "qc_attempts"]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            m = rec.get("metrics", {})
            cl = m.get("cost_latency") or {}
            writer.writerow({
                "prompt_id": rec.get("prompt_id"),
                "brief": rec.get("brief", "")[:80],
                "overall_score": rec.get("overall_score"),
                "status": rec.get("status"),
                "prompt_adherence": (m.get("prompt_adherence") or {}).get("score"),
                "temporal_consistency": (m.get("temporal_consistency") or {}).get("score"),
                "narrative_coherence": (m.get("narrative_coherence") or {}).get("score"),
                "visual_defects": (m.get("visual_defects") or {}).get("score"),
                "audio_alignment": (m.get("audio_alignment") or {}).get("score"),
                "total_sec": cl.get("total_sec"),
                "plan_sec": cl.get("plan_sec"),
                "execute_sec": cl.get("execute_sec"),
                "output_size_mb": cl.get("output_size_mb"),
                "qc_attempts": cl.get("qc_attempts"),
            })


def main():
    parser = argparse.ArgumentParser(description="Render eval run_report.jsonl as Rich table")
    parser.add_argument("jsonl", help="Path to run_report.jsonl")
    parser.add_argument("--csv", action="store_true", help="Write CSV alongside")
    parser.add_argument("--compare", default="", help="Path to older jsonl for Δ comparison")
    args = parser.parse_args()

    records = _load_jsonl(args.jsonl)
    if not records:
        console.print("[red]No records found in file.[/red]")
        sys.exit(1)

    compare_records: list[dict] | None = None
    if args.compare:
        compare_records = _load_jsonl(args.compare)

    table = render_table(records, compare_records)
    console.print(table)

    # Summary stats
    n_done = sum(1 for r in records if r.get("status") == "done")
    n_err = sum(1 for r in records if r.get("status") == "error")
    console.print(f"\n[dim]{n_done}/{len(records)} completed, {n_err} errors[/dim]")

    if args.csv:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = Path(args.jsonl).parent / f"report_{ts}.csv"
        write_csv(records, csv_path)
        console.print(f"[green]CSV written to: {csv_path}[/green]")


if __name__ == "__main__":
    main()
