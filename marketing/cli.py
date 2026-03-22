"""marketing/cli.py — command-line interface for the marketing module."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich import box

# Ensure project root on path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

app = typer.Typer(
    name="marketing",
    help="[bold cyan]adreel marketing[/bold cyan] — AI-generated ads for outreach",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
console = Console()


# ── new ───────────────────────────────────────────────────────────────────────

@app.command()
def new(
    url: str = typer.Option(..., "--url", "-u", help="Brand website URL"),
    size: str = typer.Option("small", "--size", "-s", help="Brand size: large / medium / small"),
    category: str = typer.Option("", "--category", "-c", help="Product category hint"),
    quality: str = typer.Option("kling", "--quality", "-q", help="Video quality: turbo / hd / kling"),
    platforms: str = typer.Option("tiktok,instagram", "--platforms", "-p", help="Comma-separated platforms"),
):
    """Generate a full content package for one brand URL."""
    from marketing.brand_finder import from_url, BrandSize
    from marketing.campaign_runner import run_campaign
    from marketing.tracker import Tracker

    _size: BrandSize = size if size in ("large", "medium", "small") else "small"
    lead = from_url(url, size=_size, category=category)
    platform_list = [p.strip() for p in platforms.split(",") if p.strip()]

    console.print(f"\n[bold cyan]Generating ad for:[/bold cyan] {url}")
    console.print(f"  Size: {_size}  |  Quality: {quality}  |  Platforms: {', '.join(platform_list)}\n")

    tracker = Tracker()
    result = run_campaign(lead, platforms=platform_list, quality=quality, tracker=tracker)

    if not result.ok:
        console.print(f"[red]Failed:[/red] {result.error}")
        raise typer.Exit(1)

    console.print(f"\n[bold green]Done![/bold green]")
    console.print(f"  Brand:    {result.brand}")
    console.print(f"  Video:    {result.video_path}")
    console.print(f"  Output:   {result.output_dir}")

    for platform, copy_data in result.copy.items():
        copy = copy_data.get("data", {})
        console.print(f"\n  [bold]{platform.upper()}[/bold]")
        console.print(f"    Title: {copy.get('title', '')[:60]}")
        console.print(f"    File:  {copy_data.get('path', '')}")


# ── batch ─────────────────────────────────────────────────────────────────────

@app.command()
def batch(
    file: str = typer.Option(..., "--file", "-f", help="CSV file with brand URLs"),
    quality: str = typer.Option("kling", "--quality", "-q", help="Video quality: turbo / hd / kling"),
    platforms: str = typer.Option("tiktok,instagram", "--platforms", "-p"),
    limit: int = typer.Option(0, "--limit", "-n", help="Max brands to process (0 = all)"),
):
    """Process a CSV file of brand URLs and generate content packages for each."""
    from marketing.brand_finder import find_from_csv
    from marketing.campaign_runner import run_campaign
    from marketing.tracker import Tracker

    leads = find_from_csv(file)
    if limit > 0:
        leads = leads[:limit]

    platform_list = [p.strip() for p in platforms.split(",") if p.strip()]
    tracker = Tracker()

    console.print(f"[cyan]Processing {len(leads)} brands...[/cyan]\n")
    ok = fail = 0
    for i, lead in enumerate(leads, 1):
        console.print(f"[{i}/{len(leads)}] {lead.url}")
        result = run_campaign(lead, platforms=platform_list, quality=quality, tracker=tracker)
        if result.ok:
            ok += 1
            console.print(f"  [green]✓[/green] {result.brand} → {result.output_dir}")
        else:
            fail += 1
            console.print(f"  [red]✗[/red] {result.error}")

    console.print(f"\n[bold]Done: {ok} succeeded, {fail} failed[/bold]")


# ── find ──────────────────────────────────────────────────────────────────────

@app.command()
def find(
    count: int = typer.Option(10, "--count", "-n", help="Number of brands to find"),
    category: str = typer.Option("", "--category", "-c", help="Filter by category"),
    source: str = typer.Option("product_hunt", "--source", help="Source: product_hunt"),
    run_now: bool = typer.Option(False, "--run", help="Immediately generate ads for found brands"),
):
    """Discover brands from external sources."""
    from marketing.brand_finder import find_from_product_hunt

    console.print(f"[cyan]Finding {count} brands from {source}...[/cyan]")

    leads = []
    if source == "product_hunt":
        leads = find_from_product_hunt(count=count, category=category)
    else:
        console.print(f"[red]Unknown source: {source}[/red]")
        raise typer.Exit(1)

    if not leads:
        console.print("[yellow]No brands found.[/yellow]")
        raise typer.Exit(0)

    table = Table("Name", "URL", "Category", "Size", "Source", box=box.SIMPLE)
    for lead in leads:
        table.add_row(lead.name, lead.url[:50], lead.category, lead.size, lead.source)
    console.print(table)

    if run_now:
        from marketing.campaign_runner import run_campaign
        from marketing.tracker import Tracker
        tracker = Tracker()
        for lead in leads:
            console.print(f"\n[cyan]Generating:[/cyan] {lead.name}")
            result = run_campaign(lead, tracker=tracker)
            if result.ok:
                console.print(f"  [green]✓[/green] → {result.output_dir}")
            else:
                console.print(f"  [red]✗[/red] {result.error}")


# ── log ───────────────────────────────────────────────────────────────────────

@app.command()
def log(
    campaign: str = typer.Option(..., "--campaign", "-c", help="Campaign ID"),
    platform: str = typer.Option(..., "--platform", "-p", help="Platform: tiktok / instagram / xiaohongshu"),
    post_id: str = typer.Option("", "--post-id", help="Platform post ID (for API sync)"),
    notes: str = typer.Option("", "--notes", help="Optional notes"),
):
    """Record that you've posted a campaign on a platform."""
    from marketing.tracker import Tracker
    tracker = Tracker()
    pid = tracker.record_post(campaign_id=campaign, platform=platform, post_id=post_id, notes=notes)
    console.print(f"[green]✓ Logged post:[/green] {pid}  ({platform})")
    if post_id and platform == "instagram":
        console.print(f"  [dim]Run 'marketing sync --post {pid}' to pull Instagram metrics[/dim]")


# ── sync ──────────────────────────────────────────────────────────────────────

@app.command()
def sync(
    post: str = typer.Option(..., "--post", help="Post ID from 'marketing log'"),
    ig_media_id: str = typer.Option(..., "--media-id", help="Instagram media ID"),
):
    """Pull Instagram analytics for a post via Graph API."""
    from marketing.tracker import Tracker
    tracker = Tracker()
    metrics = tracker.sync_instagram(post, ig_media_id)
    if metrics:
        console.print(f"[green]✓ Synced Instagram metrics:[/green]")
        for k, v in metrics.items():
            console.print(f"  {k}: {v}")
    else:
        console.print("[yellow]Sync failed or no token set.[/yellow]")


# ── report ────────────────────────────────────────────────────────────────────

@app.command()
def report():
    """Show conversion report grouped by brand size and platform."""
    from marketing.tracker import Tracker
    tracker = Tracker()
    rows = tracker.report()

    if not rows:
        console.print("[yellow]No data yet. Run some campaigns first.[/yellow]")
        return

    table = Table(
        "Size", "Platform", "Campaigns", "Posts",
        "Avg Views", "Avg Likes", "Total DMs", "DM Rate %",
        box=box.SIMPLE,
    )
    for r in rows:
        table.add_row(
            str(r.get("size", "")),
            str(r.get("platform", "")),
            str(r.get("campaigns", 0)),
            str(r.get("posts", 0)),
            str(int(r.get("avg_views") or 0)),
            str(int(r.get("avg_likes") or 0)),
            str(r.get("total_dms", 0)),
            str(r.get("dm_rate_pct", 0)),
        )
    console.print(table)

    campaigns = tracker.list_campaigns(limit=5)
    if campaigns:
        console.print("\n[bold]Recent Campaigns[/bold]")
        ct = Table("ID", "Brand", "Size", "Category", "Created", box=box.SIMPLE)
        for c in campaigns:
            ct.add_row(
                c["id"][:16], c["brand"], c["size"], c["category"],
                c["created_at"][:10],
            )
        console.print(ct)


def main():
    app()


if __name__ == "__main__":
    main()
