"""ask_user — CLI interactive loop to collect clarification answers."""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich import box

console = Console()


def ask_user(state: dict[str, Any]) -> dict[str, Any]:
    """Block and prompt the user for each missing field via stdin."""
    questions: list[dict] = state.get("clarification_questions", [])
    answers: dict[str, Any] = dict(state.get("clarification_answers", {}))

    if not questions:
        return {"clarification_answers": answers}

    console.print(
        Panel(
            "[bold cyan]A few quick questions before I build your plan[/bold cyan]",
            box=box.ROUNDED,
        )
    )

    for q in questions:
        field = q["field"]
        question_text = q["question"]
        options: list[dict] = q.get("options", [])

        console.print(f"\n[bold yellow]{question_text}[/bold yellow]")
        for i, opt in enumerate(options, 1):
            console.print(f"  [green]{i}[/green]. {opt['label']}")

        while True:
            raw = Prompt.ask(
                f"  Enter number (1-{len(options)}) or type custom value",
                default="1",
            ).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                answers[field] = options[int(raw) - 1]["value"]
                console.print(f"  [dim]→ {options[int(raw)-1]['label']}[/dim]")
                break
            elif raw and not raw.isdigit():
                # Accept free-text answer
                answers[field] = raw
                console.print(f"  [dim]→ {raw}[/dim]")
                break
            else:
                console.print("[red]  Invalid choice, please try again.[/red]")

    messages = state.get("messages", [])
    messages.append(
        {"role": "user", "content": f"[ask_user] collected answers: {answers}"}
    )

    return {
        "clarification_answers": answers,
        "clarification_needed": False,
        "messages": messages,
    }
