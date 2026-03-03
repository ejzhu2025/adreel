"""caption_renderer — write SRT subtitle files from CaptionSegment list."""
from __future__ import annotations

from typing import Any


def _format_time(sec: float) -> str:
    """Convert seconds to SRT timestamp HH:MM:SS,mmm."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec % 1) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class CaptionRenderer:
    def write_srt(self, segments: list[dict[str, Any]], output_path: str) -> None:
        """Write a standard SRT file from caption segment dicts."""
        lines: list[str] = []
        for seg in segments:
            idx = seg.get("index", 1)
            start = float(seg.get("start_sec", 0.0))
            end = float(seg.get("end_sec", start + 2.0))
            text = seg.get("text", "")

            # Ensure minimum subtitle display duration
            if end - start < 0.5:
                end = start + 0.5

            lines.append(str(idx))
            lines.append(f"{_format_time(start)} --> {_format_time(end)}")
            lines.append(text)
            lines.append("")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def write_ass(self, segments: list[dict[str, Any]], output_path: str, style: dict | None = None) -> None:
        """Write an ASS subtitle file with brand styling."""
        s = style or {}
        font_size = s.get("font_size", 38)
        primary_color = s.get("primary_color", "&H00FFFFFF")
        box_color = s.get("box_color", "&H99000000")

        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{primary_color},&H000000FF,&H00000000,{box_color},0,0,0,0,100,100,0,0,3,0,0,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        event_lines = [header]
        for seg in segments:
            start = float(seg.get("start_sec", 0.0))
            end = float(seg.get("end_sec", start + 2.0))
            text = seg.get("text", "").replace("\n", "\\N")
            event_lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
            )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(event_lines))


def _ass_time(sec: float) -> str:
    """Convert seconds to ASS timestamp H:MM:SS.cc"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec % 1) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
