"""eval/runner.py — CLI to run eval prompts against the video-agent-hero server.

Usage:
    python3.11 -m eval.runner \\
        --server http://localhost:7860 \\
        [--ids p001,p002] \\
        [--quality turbo] \\
        [--no-execute] \\
        [--out eval/run_reports/]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

# Project root on sys.path so metric imports work
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval.metrics import prompt_adherence, temporal_consistency, narrative_coherence
from eval.metrics import visual_defects, audio_alignment, cost_latency

console = Console()

WEIGHTS = {
    "prompt_adherence": 0.30,
    "temporal_consistency": 0.20,
    "narrative_coherence": 0.20,
    "visual_defects": 0.20,
    "audio_alignment": 0.10,
}

PLAN_TIMEOUT = 5 * 60      # 5 min
EXECUTE_TIMEOUT = 10 * 60  # 10 min
POLL_INTERVAL = 3          # seconds


def _load_prompts(ids_filter: list[str] | None = None) -> list[dict]:
    prompts_path = Path(__file__).parent / "prompts.json"
    with open(prompts_path, encoding="utf-8") as f:
        prompts = json.load(f)
    if ids_filter:
        prompts = [p for p in prompts if p["id"] in ids_filter]
    return prompts


def _poll_until(
    client: httpx.Client,
    server: str,
    project_id: str,
    target_statuses: set[str],
    timeout: int,
    label: str,
) -> tuple[dict | None, float]:
    """Poll GET /api/projects/{id} until status in target_statuses or timeout."""
    deadline = time.time() + timeout
    start = time.time()
    while time.time() < deadline:
        try:
            resp = client.get(f"{server}/api/projects/{project_id}", timeout=15)
            proj = resp.json()
            status = proj.get("status", "")
            if status in target_statuses:
                return proj, time.time() - start
            if status == "failed":
                console.print(f"[red]  [{label}] project failed[/red]")
                return proj, time.time() - start
        except Exception as e:
            console.print(f"[yellow]  poll error: {e}[/yellow]")
        time.sleep(POLL_INTERVAL)
    console.print(f"[red]  [{label}] timed out after {timeout}s[/red]")
    return None, time.time() - start


def _compute_overall(metrics: dict) -> float:
    """Weighted mean of scored metrics; skip metrics with score=None."""
    total_weight = 0.0
    weighted_sum = 0.0
    for key, weight in WEIGHTS.items():
        m = metrics.get(key, {})
        s = m.get("score") if isinstance(m, dict) else None
        if s is not None:
            weighted_sum += s * weight
            total_weight += weight
    if total_weight == 0:
        return 0.0
    return round(weighted_sum / total_weight, 4)


def _run_one(
    client: httpx.Client,
    server: str,
    prompt: dict,
    quality: str,
    no_execute: bool,
    data_dir: str,
) -> dict:
    run_id = str(uuid.uuid4())
    prompt_id = prompt["id"]
    brief = prompt["brief"]
    expected_keywords = prompt.get("expected_keywords", [])

    console.print(f"\n[bold cyan]── {prompt_id}: {brief[:60]}…[/bold cyan]")

    record: dict = {
        "run_id": run_id,
        "prompt_id": prompt_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "brief": brief,
        "project_id": None,
        "status": "error",
        "plan": None,
        "metrics": {},
        "overall_score": None,
        "error": None,
    }

    try:
        # 1. Create project
        create_resp = client.post(
            f"{server}/api/projects",
            json={"brief": brief},
            timeout=15,
        )
        create_resp.raise_for_status()
        project_id = create_resp.json()["project_id"]
        record["project_id"] = project_id
        console.print(f"  project_id: {project_id}")

        # 2. Plan phase
        plan_start = time.time()
        plan_resp = client.post(
            f"{server}/api/projects/{project_id}/plan",
            json={
                "brief": brief,
                "clarification_answers": prompt.get("clarification_answers", {}),
            },
            timeout=15,
        )
        plan_resp.raise_for_status()
        console.print("  planning…", end="")

        proj, plan_elapsed = _poll_until(
            client, server, project_id,
            target_statuses={"planned", "done", "failed"},
            timeout=PLAN_TIMEOUT,
            label="plan",
        )
        plan_sec = plan_elapsed
        console.print(f" done in {plan_sec:.1f}s")

        if proj is None or proj.get("status") == "failed":
            record["status"] = "failed"
            record["error"] = "plan phase failed or timed out"
            return record

        plan = proj.get("latest_plan_json") or {}
        record["plan"] = plan

        # 3. Prompt adherence metric (works on plan alone)
        pa = prompt_adherence.score(plan, expected_keywords)
        record["metrics"]["prompt_adherence"] = pa
        console.print(f"  prompt_adherence: {pa['score']:.2f} (matched {pa['matched']})")

        if no_execute:
            record["status"] = "done"
            record["overall_score"] = _compute_overall(record["metrics"])
            return record

        # 4. Execute phase
        execute_start = time.time()
        exec_resp = client.post(
            f"{server}/api/projects/{project_id}/execute",
            json={"quality": quality},
            timeout=15,
        )
        exec_resp.raise_for_status()
        console.print("  executing…", end="")

        proj, exec_elapsed = _poll_until(
            client, server, project_id,
            target_statuses={"done", "failed"},
            timeout=EXECUTE_TIMEOUT,
            label="execute",
        )
        execute_sec = exec_elapsed
        console.print(f" done in {execute_sec:.1f}s")

        if proj is None:
            record["status"] = "failed"
            record["error"] = "execute phase timed out"
            record["metrics"]["cost_latency"] = cost_latency.measure(plan_sec, execute_sec)
            record["overall_score"] = _compute_overall(record["metrics"])
            return record

        # Gather shot IDs from plan
        shot_ids: list[str] = []
        for shot in plan.get("shot_list", plan.get("storyboard", [])):
            if isinstance(shot, dict):
                sid = shot.get("shot_id") or shot.get("id")
                if sid:
                    shot_ids.append(str(sid))

        # 5. Temporal consistency
        tc = temporal_consistency.score(project_id, shot_ids, data_dir=data_dir)
        record["metrics"]["temporal_consistency"] = tc
        console.print(f"  temporal_consistency: {tc['score']:.2f}")

        # 6. Narrative coherence
        storyboard = plan.get("storyboard") or plan.get("shot_list", [])
        nc = narrative_coherence.score(brief, storyboard)
        record["metrics"]["narrative_coherence"] = nc
        status_str = "(skipped)" if nc["skipped"] else f"{nc['score']:.2f}"
        console.print(f"  narrative_coherence: {status_str}")

        # 7. Visual defects
        vd = visual_defects.score(project_id, shot_ids, data_dir=data_dir)
        record["metrics"]["visual_defects"] = vd
        console.print(f"  visual_defects: {vd['score']:.2f}")

        # 8. Audio alignment
        quality_result = proj.get("latest_plan_json", {}) or {}
        aa = audio_alignment.score(project_id, data_dir=data_dir)
        record["metrics"]["audio_alignment"] = aa
        status_str = "(skipped)" if aa.get("skipped") else f"{aa['score']:.2f}"
        console.print(f"  audio_alignment: {status_str}")

        # 9. Cost/latency
        output_paths = proj.get("output_paths", [])
        output_path = output_paths[-1] if output_paths else None
        cl = cost_latency.measure(
            plan_sec=plan_sec,
            execute_sec=execute_sec,
            quality_result=quality_result.get("quality_result"),
            output_path=output_path,
        )
        record["metrics"]["cost_latency"] = cl
        console.print(f"  cost_latency: total={cl['total_sec']:.1f}s size={cl['output_size_mb']}MB")

        record["status"] = proj.get("status", "done")
        record["overall_score"] = _compute_overall(record["metrics"])
        console.print(f"  [bold]overall_score: {record['overall_score']:.2f}[/bold]")

    except Exception as e:
        record["status"] = "error"
        record["error"] = str(e)
        console.print(f"  [red]error: {e}[/red]")

    return record


def main():
    parser = argparse.ArgumentParser(description="Run eval prompts against video-agent-hero server")
    parser.add_argument("--server", default="http://localhost:7860")
    parser.add_argument("--ids", default="", help="Comma-separated prompt IDs to run (default: all)")
    parser.add_argument("--quality", default="turbo", choices=["turbo", "hd"])
    parser.add_argument("--no-execute", action="store_true", help="Plan only, skip execute phase")
    parser.add_argument("--out", default="eval/run_reports/")
    parser.add_argument("--data-dir", default="data", help="Path to data directory with project clips")
    args = parser.parse_args()

    ids_filter = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else None
    prompts = _load_prompts(ids_filter)

    if not prompts:
        console.print("[red]No prompts matched.[/red]")
        sys.exit(1)

    console.print(f"[bold]Running {len(prompts)} prompt(s) against {args.server}[/bold]")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"run_{ts}.jsonl"

    with httpx.Client() as client:
        for prompt in prompts:
            record = _run_one(
                client=client,
                server=args.server,
                prompt=prompt,
                quality=args.quality,
                no_execute=args.no_execute,
                data_dir=args.data_dir,
            )
            with open(out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    console.print(f"\n[green]Report written to: {out_file}[/green]")


if __name__ == "__main__":
    main()
