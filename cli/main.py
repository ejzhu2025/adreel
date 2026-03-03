"""video-agent-hero CLI — entry point for all commands."""
from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

# Load .env early so deps can read VAH_DATA_DIR
load_dotenv()

app = typer.Typer(
    name="vah",
    help="[bold cyan]video-agent-hero[/bold cyan] — Agentic Video Generator",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
console = Console()


def _init_deps():
    """Initialize global DB + VectorStore singletons."""
    import agent.deps as deps
    deps.init()
    return deps.db(), deps.vs()


# ── init ──────────────────────────────────────────────────────────────────────

@app.command()
def init():
    """Initialize the database and load the sample Tong Sui brand kit."""
    from memory.schemas import BrandKit, UserPrefs, LogoConfig, ColorPalette, FontConfig, SubtitleStyle, IntroOutro
    from scripts.create_assets import create_placeholder_logo

    db, _ = _init_deps()

    # Create placeholder logo
    logo_path = create_placeholder_logo()
    console.print(f"[green]✓[/green] Placeholder logo created: {logo_path}")

    # Load Tong Sui brand kit
    tong_sui = BrandKit(
        brand_id="tong_sui",
        name="Tong Sui",
        logo=LogoConfig(path=str(logo_path), safe_area="top_right"),
        colors=ColorPalette(
            primary="#00B894",
            secondary="#FFFFFF",
            accent="#FF7675",
            background="#1A1A2E",
        ),
        fonts=FontConfig(title="Poppins-SemiBold", body="Inter-Regular"),
        subtitle_style=SubtitleStyle(
            position="bottom_center",
            box_opacity=0.55,
            box_radius=12,
            padding_px=14,
            max_chars_per_line=18,
            highlight_keywords=True,
            font_size=44,
        ),
        intro_outro=IntroOutro(
            intro_template="mint_splash",
            outro_cta="Order now",
            intro_duration_sec=1.5,
            outro_duration_sec=2.0,
        ),
    )
    db.upsert_brand_kit(tong_sui)
    console.print("[green]✓[/green] Tong Sui brand kit loaded")

    # Load default user prefs
    ej = UserPrefs(
        user_id="ej",
        default_platform="tiktok",
        preferred_duration_sec=20,
        tone=["fresh", "playful", "premium"],
        pacing="fast",
        shot_density=7,
        cta_style="soft",
    )
    db.upsert_user_prefs(ej)
    console.print("[green]✓[/green] User 'ej' prefs loaded")

    console.print("\n[bold green]✓ Init complete![/bold green] Run [cyan]vah new --brief \"...\"[/cyan] to start.")


# ── new ───────────────────────────────────────────────────────────────────────

@app.command()
def new(
    brief: str = typer.Option(..., "--brief", "-b", help="Project brief description"),
    brand: str = typer.Option("tong_sui", "--brand", help="Brand ID"),
    user: str = typer.Option("ej", "--user", "-u", help="User ID"),
):
    """Create a new project and print its ID."""
    db, _ = _init_deps()
    pid = db.create_project(brief=brief, brand_id=brand, user_id=user)
    console.print(f"[bold green]✓ Project created:[/bold green] [cyan]{pid}[/cyan]")
    console.print(f'  Run: [dim]vah run --project {pid}[/dim]')


# ── run ───────────────────────────────────────────────────────────────────────

@app.command()
def run(
    project: str = typer.Option(..., "--project", "-p", help="Project ID"),
    skip_clarification: bool = typer.Option(False, "--yes", "-y", help="Skip clarification questions"),
):
    """Run the full video generation pipeline for a project."""
    import agent.deps as deps
    from agent.graph import build_graph

    db, _ = _init_deps()
    proj = db.get_project(project)
    if not proj:
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(1)

    db.update_project_status(project, "running")

    initial_state: dict = {
        "project_id": project,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "messages": [],
        "clarification_answers": {},
        "plan_version": 0,
        "qc_attempt": 1,
        "needs_replan": False,
    }

    if skip_clarification:
        prefs = db.get_user_prefs(proj["user_id"])
        initial_state["clarification_answers"] = {
            "platform": prefs.default_platform if prefs else "tiktok",
            "duration_sec": prefs.preferred_duration_sec if prefs else 20,
            "style_tone": prefs.tone if prefs else ["fresh"],
            "language": "en",
            "assets_available": "none",
        }

    console.print(f"\n[bold cyan]▶ Running pipeline for project:[/bold cyan] [green]{project}[/green]")
    console.print(f"  Brief: [italic]{proj['brief'][:80]}[/italic]\n")

    graph = build_graph()
    try:
        result = graph.invoke(initial_state)
        output = result.get("output_path", "N/A")
        console.print(f"\n[bold green]🎬 Done![/bold green] Output: [cyan]{output}[/cyan]")
    except Exception as e:
        db.update_project_status(project, "failed")
        console.print(f"[red]Pipeline failed: {e}[/red]")
        import traceback
        traceback.print_exc()
        raise typer.Exit(1)


# ── feedback ──────────────────────────────────────────────────────────────────

@app.command()
def feedback(
    project: str = typer.Option(..., "--project", "-p", help="Project ID"),
    text: str = typer.Option(..., "--text", "-t", help="Feedback text"),
    rating: int = typer.Option(None, "--rating", "-r", help="Rating 1-5"),
    replan: bool = typer.Option(True, "--replan/--no-replan", help="Trigger re-plan after feedback"),
):
    """Store feedback for a project and optionally trigger re-plan + re-render."""
    import agent.deps as deps
    from agent.graph import build_replan_graph

    db, _ = _init_deps()
    proj = db.get_project(project)
    if not proj:
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(1)

    db.add_feedback(project, text, rating)
    console.print(f"[green]✓ Feedback saved[/green] for project {project}")

    if not replan:
        return

    console.print("[cyan]↻ Triggering re-plan with feedback…[/cyan]\n")

    plan = proj.get("latest_plan_json") or {}
    answers = {}
    if plan:
        answers = {
            "platform": plan.get("platform", "tiktok"),
            "duration_sec": plan.get("duration_sec", 20),
            "style_tone": plan.get("style_tone", ["fresh"]),
            "language": plan.get("language", "en"),
            "assets_available": "none",
        }

    brand_kit_obj = db.get_brand_kit(proj["brand_id"])

    initial_state: dict = {
        "project_id": project,
        "brief": proj["brief"],
        "brand_id": proj["brand_id"],
        "user_id": proj["user_id"],
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": {},
        "similar_projects": [],
        "plan": plan,
        "plan_version": plan.get("version", 1),
        "plan_feedback": text,
        "clarification_answers": answers,
        "messages": [],
        "qc_attempt": 1,
        "needs_replan": True,
    }

    graph = build_replan_graph()
    try:
        result = graph.invoke(initial_state)
        output = result.get("output_path", "N/A")
        console.print(f"\n[bold green]🎬 Re-render done![/bold green] Output: [cyan]{output}[/cyan]")
    except Exception as e:
        console.print(f"[red]Re-plan failed: {e}[/red]")
        import traceback
        traceback.print_exc()
        raise typer.Exit(1)


# ── export ────────────────────────────────────────────────────────────────────

@app.command()
def export(
    project: str = typer.Option(..., "--project", "-p", help="Project ID"),
    ratio: str = typer.Option("9:16", "--ratio", help="Output ratio"),
):
    """Show export path(s) for a completed project."""
    db, _ = _init_deps()
    proj = db.get_project(project)
    if not proj:
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(1)

    paths = proj.get("output_paths", [])
    if not paths:
        console.print(f"[yellow]No exports yet. Run 'vah run --project {project}' first.[/yellow]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Exports for project [cyan]{project}[/cyan]:[/bold]")
    for p in paths:
        exists = "[green]✓[/green]" if Path(p).exists() else "[red]✗ missing[/red]"
        console.print(f"  {exists}  {p}")


# ── list ──────────────────────────────────────────────────────────────────────

@app.command(name="list")
def list_projects(limit: int = typer.Option(10, "--limit", "-n")):
    """List recent projects."""
    db, _ = _init_deps()
    projects = db.list_projects(limit=limit)

    table = Table(
        "ID", "Brief", "Brand", "Status", "Created",
        box=box.SIMPLE,
        show_header=True,
    )
    for p in projects:
        brief = p["brief"][:40] + ("…" if len(p["brief"]) > 40 else "")
        table.add_row(p["project_id"], brief, p["brand_id"], p["status"], p["created_at"][:19])

    console.print(table)


# ── demo ──────────────────────────────────────────────────────────────────────

@app.command()
def demo():
    """Run the end-to-end Tong Sui demo (init + new + run --yes)."""
    import agent.deps as deps
    from agent.graph import build_graph

    console.print("[bold cyan]🎬 Running Tong Sui Demo[/bold cyan]\n")

    # 1. Init deps + brand kit
    db, _ = _init_deps()
    init()
    console.print()

    # 2. Create project
    pid = db.create_project(
        brief="Create a summer promo video for Tong Sui's new drink Coconut Watermelon Refresh.",
        brand_id="tong_sui",
        user_id="ej",
    )
    console.print(f"[green]✓ Demo project:[/green] {pid}\n")

    # 3. Build state — skip clarification, use loaded brand/prefs
    prefs = db.get_user_prefs("ej")
    brand_kit_obj = db.get_brand_kit("tong_sui")

    initial_state: dict = {
        "project_id": pid,
        "brief": "Create a summer promo video for Tong Sui's new drink Coconut Watermelon Refresh.",
        "brand_id": "tong_sui",
        "user_id": "ej",
        "brand_kit": brand_kit_obj.model_dump() if brand_kit_obj else {},
        "user_prefs": prefs.model_dump() if prefs else {},
        "similar_projects": [],
        "messages": [],
        "clarification_answers": {
            "platform": "tiktok",
            "duration_sec": 20,
            "style_tone": ["fresh", "playful"],
            "language": "en",
            "assets_available": "none",
        },
        "plan_version": 0,
        "qc_attempt": 1,
        "needs_replan": False,
    }

    db.update_project_status(pid, "running")
    graph = build_graph()
    try:
        result = graph.invoke(initial_state)
        output = result.get("output_path", "N/A")
        console.print(f"\n[bold green]🎉 Demo complete![/bold green]")
        console.print(f"   Video: [cyan]{output}[/cyan]")
        console.print(f"   Project ID: [cyan]{pid}[/cyan]")
        console.print(f"\n   Try: [dim]vah feedback --project {pid} --text \"Make it more energetic\"[/dim]")
    except Exception as e:
        console.print(f"[red]Demo failed: {e}[/red]")
        import traceback
        traceback.print_exc()


def main():
    app()


if __name__ == "__main__":
    main()
