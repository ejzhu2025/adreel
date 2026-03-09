"""compare_planners.py — side-by-side comparison of old vs new planning approach.

Usage:
    python3.11 scripts/compare_planners.py
    python3.11 scripts/compare_planners.py --brief "Your custom brief here"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich import box

console = Console()

# ── Shared test fixtures ───────────────────────────────────────────────────────

DEFAULT_BRIEF = "Create a summer promo video for Tong Sui's new Coconut Watermelon Refresh drink."

BRAND_KIT = {
    "brand_id": "tong_sui",
    "name": "Tong Sui",
    "colors": {"primary": "#00B894"},
    "intro_outro": {"outro_cta": "Order now"},
}

STATE = {
    "brief": DEFAULT_BRIEF,
    "brand_kit": BRAND_KIT,
    "clarification_answers": {
        "platform": "tiktok",
        "duration_sec": 15,
        "language": "en",
        "style_tone": ["fresh", "summer", "vibrant"],
    },
    "similar_projects": [],
    "plan_feedback": "",
    "plan": None,
}


# ── Old approach: single monolithic prompt ────────────────────────────────────

def run_old_planner(llm_call, brief: str) -> dict:
    from agent.nodes.planner_llm import PLANNER_SYSTEM

    OLD_USER = """\
Brief: {brief}
Brand: Tong Sui, primary color: #00B894, CTA: "Order now"
Platform: tiktok, Duration: 15s, Language: en
Tone: fresh, summer, vibrant
User feedback / modification request: None
Similar past projects (for reference): []

Generate the plan JSON now."""

    raw = llm_call(PLANNER_SYSTEM, OLD_USER.format(brief=brief))
    import re, json as _json
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return _json.loads(text)


# ── New approach: Director → Storyboard → Critic → Compiler ──────────────────

def run_new_planner(llm_call, brief: str) -> tuple[dict, dict, dict]:
    state = {**STATE, "brief": brief}
    from agent.nodes.creative_pipeline import run_creative_pipeline
    concept, plan, prompts = run_creative_pipeline(state, "cmp001", llm_call)
    return concept, plan, prompts


# ── Display helpers ───────────────────────────────────────────────────────────

def print_storyboard(plan: dict, title: str, color: str):
    storyboard = plan.get("storyboard", [])
    shot_list = plan.get("shot_list", [])
    script = plan.get("script", {})

    table = Table(title=title, box=box.ROUNDED, style=color, show_lines=True, width=80)
    table.add_column("#", width=3, style="bold")
    table.add_column("Type", width=10)
    table.add_column("Scene Description", width=38)
    table.add_column("Overlay", width=20)
    table.add_column("s", width=5)

    for i, scene in enumerate(storyboard):
        shot = shot_list[i] if i < len(shot_list) else {}
        table.add_row(
            str(scene.get("scene", i + 1)),
            shot.get("type", scene.get("asset_hint", "")),
            scene.get("desc", "")[:80],
            shot.get("text_overlay", ""),
            str(scene.get("duration", "")),
        )

    console.print(table)
    console.print(f"  [dim]Hook:[/dim] {script.get('hook', '')}")
    console.print(f"  [dim]CTA:[/dim]  {script.get('cta', '')}")
    console.print()


def print_concept(concept: dict):
    console.print(Panel(
        f"[bold]Hook angle:[/bold]  {concept.get('hook_angle', '')}\n"
        f"[bold]Visual style:[/bold] {concept.get('visual_style', '')}\n"
        f"[bold]Key message:[/bold] {concept.get('key_message', '')}\n"
        f"[bold]Mood:[/bold]        {concept.get('mood', '')}\n"
        f"[bold]Scene count:[/bold] {concept.get('scene_count', '')}",
        title="[cyan]Director's concept[/cyan]",
        border_style="cyan",
        width=80,
    ))


def print_prompts(prompts: dict):
    if not prompts:
        console.print("[dim]  (no compiled prompts — executor will build them)[/dim]\n")
        return
    table = Table(title="Compiled T2V/I2V prompts", box=box.SIMPLE, width=80)
    table.add_column("Shot", width=6)
    table.add_column("Prompt", width=70)
    for shot_id, prompt in prompts.items():
        display = f"[dim](PIL frame)[/dim]" if not prompt else prompt[:120]
        table.add_row(shot_id, display)
    console.print(table)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", default=DEFAULT_BRIEF)
    args = parser.parse_args()
    brief = args.brief

    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print("[red]No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.[/red]")
        sys.exit(1)

    # Build LLM call
    if os.getenv("ANTHROPIC_API_KEY"):
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=2048)  # type: ignore[call-arg]
        def llm_call(system: str, user: str) -> str:
            r = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            return r.content if hasattr(r, "content") else str(r)
    else:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = ChatOpenAI(model="gpt-4o", max_tokens=2048, response_format={"type": "json_object"})
        def llm_call(system: str, user: str) -> str:
            r = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            return r.content if hasattr(r, "content") else str(r)

    console.rule(f"[bold]Brief:[/bold] {brief}")
    console.print()

    # ── OLD approach ──
    console.print("[yellow bold]▶ OLD — single monolithic prompt[/yellow bold]")
    t0 = time.time()
    try:
        old_plan = run_old_planner(llm_call, brief)
        old_elapsed = time.time() - t0
        print_storyboard(old_plan, f"OLD storyboard  ({old_elapsed:.1f}s, 1 LLM call)", "yellow")
    except Exception as e:
        console.print(f"[red]OLD planner failed: {e}[/red]")
        old_plan = None

    # ── NEW approach ──
    console.print("[green bold]▶ NEW — Director → Storyboard → Critic → Compiler[/green bold]")
    t1 = time.time()
    try:
        concept, new_plan, prompts = run_new_planner(llm_call, brief)
        new_elapsed = time.time() - t1
        print_concept(concept)
        print_storyboard(new_plan, f"NEW storyboard  ({new_elapsed:.1f}s, 4 LLM calls)", "green")
        print_prompts(prompts)
    except Exception as e:
        console.print(f"[red]NEW planner failed: {e}[/red]")
        new_plan = None

    # ── Quick quality check ──
    console.rule("Quality check")
    FORBIDDEN = ["branded", "branding", "logo shown", "text appears", "caption",
                 "title card", "overlay", "tagline"]

    for label, plan in [("OLD", old_plan), ("NEW", new_plan)]:
        if plan is None:
            continue
        descs = [s.get("desc", "") for s in plan.get("storyboard", [])]
        violations = [
            (i + 1, w, descs[i])
            for i, d in enumerate(descs)
            for w in FORBIDDEN
            if w in d.lower()
        ]
        if violations:
            console.print(f"[red]{label} — {len(violations)} desc violation(s):[/red]")
            for scene_n, word, desc in violations:
                console.print(f"  Scene {scene_n}: '{word}' found in: {desc[:60]}…")
        else:
            console.print(f"[green]{label} — no desc violations ✓[/green]")

    # ── Timing summary ──
    console.print()
    if old_plan and new_plan:
        console.print(
            f"[dim]Latency: OLD {old_elapsed:.1f}s (1 call)  vs  "
            f"NEW {new_elapsed:.1f}s (4 calls)[/dim]"
        )


if __name__ == "__main__":
    main()
