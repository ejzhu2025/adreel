"""qc_diagnose — LLM-powered root cause analysis for quality gate failures."""
from __future__ import annotations

import os
from typing import Any

from rich.console import Console

console = Console()

# ── Rule-based diagnosis catalogue ───────────────────────────────────────────

_RULES: list[dict] = [
    {
        "match": lambda issues, state: (
            any("blank" in i.lower() or "uniform" in i.lower() or "bitrate" in i.lower() for i in issues)
            and not (os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))
        ),
        "diagnosis": "missing_fal_key",
        "severity": "blocking",
        "message": (
            "视频出现蓝屏/纯色帧，原因是 **fal.ai API Key 未设置**。\n"
            "当前使用 PIL 占位符生成画面（纯色渐变），不是真实 AI 生成视频。\n\n"
            "👉 请点右上角 ⚙️ Settings → 填入 fal.ai API Key → Save，然后重新跑 Pipeline。"
        ),
        "action": "open_settings",
    },
    {
        "match": lambda issues, state: (
            any("blank" in i.lower() or "uniform" in i.lower() or "bitrate" in i.lower() for i in issues)
            and bool(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))
        ),
        "diagnosis": "fal_bad_output",
        "severity": "warning",
        "message": (
            "视频出现蓝屏/纯色帧，fal.ai Key 已设置但生成内容异常。\n"
            "可能原因：prompt 过于抽象、fal.ai 服务短暂故障、或网络下载失败。\n\n"
            "👉 建议：修改 brief 让描述更具体，然后重新跑 Pipeline。"
        ),
        "action": "rerun",
    },
    {
        "match": lambda issues, state: any("Resolution" in i for i in issues),
        "diagnosis": "wrong_resolution",
        "severity": "warning",
        "message": (
            "视频分辨率不符合 1080×1920（9:16 竖屏）要求。\n"
            "可能是 trim_and_scale_clip 的 scale/crop 滤镜未生效。\n\n"
            "👉 Pipeline 会自动重试，如仍失败请联系开发者。"
        ),
        "action": "retry_render",
    },
    {
        "match": lambda issues, state: any("Duration" in i for i in issues),
        "diagnosis": "duration_mismatch",
        "severity": "warning",
        "message": (
            "视频实际时长与计划不符。\n"
            "可能是 fal.ai clip 生成时长不足，或 concat 步骤丢帧。\n\n"
            "👉 Pipeline 会自动重试布局步骤。"
        ),
        "action": "retry_render",
    },
]


def qc_diagnose(state: dict[str, Any]) -> dict[str, Any]:
    quality_result = state.get("quality_result", {})
    issues: list[str] = quality_result.get("issues", [])

    diagnosis = "unknown"
    user_message = ""
    needs_user_action = False
    action = "retry_render"

    # Rule-based first
    for rule in _RULES:
        try:
            if rule["match"](issues, state):
                diagnosis = rule["diagnosis"]
                user_message = rule["message"]
                action = rule["action"]
                needs_user_action = (action == "open_settings")
                break
        except Exception:
            continue

    # LLM fallback for unknown issues
    if diagnosis == "unknown" and issues:
        user_message = _llm_diagnose(issues, state)
        needs_user_action = False

    console.print(
        f"[{'red' if needs_user_action else 'yellow'}]"
        f"[qc_diagnose] {diagnosis} — {user_message[:80]}…[/]"
    )

    messages = state.get("messages", [])
    messages.append({
        "role": "system",
        "content": (
            f"[qc_diagnose] diagnosis={diagnosis} "
            f"needs_user_action={needs_user_action} action={action}"
        ),
    })

    return {
        "qc_diagnosis": diagnosis,
        "qc_user_message": user_message,
        "needs_user_action": needs_user_action,
        "messages": messages,
    }


def _llm_diagnose(issues: list[str], state: dict) -> str:
    """Ask Claude to diagnose unfamiliar QC issues."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return (
            f"视频质量检测发现问题：{'; '.join(issues)}。\n"
            "请检查日志或联系开发者。"
        )
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=300)
        prompt = (
            f"视频生成质量检查失败，问题列表：\n{chr(10).join(f'- {i}' for i in issues)}\n\n"
            "请用中文简洁（2-3句）告诉用户：\n"
            "1. 最可能的根本原因\n"
            "2. 用户需要做什么操作来修复\n"
            "不要废话，直接说原因和操作步骤。"
        )
        response = llm.invoke([
            SystemMessage(content="你是视频生成系统的诊断助手，专门帮用户排查问题。"),
            HumanMessage(content=prompt),
        ])
        return str(response.content)
    except Exception as e:
        return f"质量检测失败：{'; '.join(issues)}。请检查日志（{e}）。"
