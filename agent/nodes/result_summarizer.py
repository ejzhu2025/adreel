"""result_summarizer — produce a human-readable summary of the project run."""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()


def result_summarizer(state: dict[str, Any]) -> dict[str, Any]:
    plan = state.get("plan", {})
    output_path = state.get("output_path", "N/A")
    quality_result = state.get("quality_result", {})
    plan_version = state.get("plan_version", 1)
    project_id = state.get("project_id", "?")

    script = plan.get("script", {})
    shot_count = len(plan.get("shot_list", []))
    duration = plan.get("duration_sec", 0)
    platform = plan.get("platform", "tiktok")
    language = plan.get("language", "en")
    tone = plan.get("style_tone", [])

    summary_lines = [
        f"Project: {project_id}",
        f"Platform: {platform} | Duration: {duration}s | Language: {language}",
        f"Tone: {', '.join(tone) if isinstance(tone, list) else tone}",
        f"Shots: {shot_count} | Plan version: {plan_version}",
        f"Hook: \"{script.get('hook', '')}\"",
        f"CTA: \"{script.get('cta', '')}\"",
        f"QC: {'✓ passed' if quality_result.get('passed') else '⚠ issues: ' + str(quality_result.get('issues', []))}",
        f"Output: {output_path}",
    ]
    summary_text = "\n".join(summary_lines)

    # Rich display
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Project", project_id)
    table.add_row("Platform", f"{platform} | {duration}s | {language}")
    table.add_row("Tone", ", ".join(tone) if isinstance(tone, list) else str(tone))
    table.add_row("Shots", str(shot_count))
    table.add_row("Hook", script.get("hook", ""))
    table.add_row("CTA", script.get("cta", ""))
    table.add_row("QC", "✓ passed" if quality_result.get("passed") else "⚠ issues")
    table.add_row("Output", output_path)

    console.print(
        Panel(table, title="[bold green]Video Ready[/bold green]", box=box.ROUNDED)
    )

    messages = state.get("messages", [])
    messages.append({"role": "assistant", "content": summary_text})

    return {"summary": summary_text, "messages": messages}
