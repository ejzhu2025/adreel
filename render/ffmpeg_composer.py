"""ffmpeg_composer — all FFmpeg subprocess calls."""
from __future__ import annotations

import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional


class FFmpegComposer:
    """Thin wrapper around FFmpeg for video composition."""

    def _run(self, cmd: list[str], timeout: int = 120) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-500:]}")

    # ── Image → Clip (Ken Burns) ──────────────────────────────────────────────

    def image_to_clip(
        self,
        image_path: str,
        output_path: str,
        duration: float,
        width: int = 1080,
        height: int = 1920,
        ken_burns: bool = True,
        fps: int = 30,
    ) -> None:
        """Convert a still image to a video clip with optional Ken Burns zoom."""
        frames = int(duration * fps)
        if frames < 1:
            frames = 1

        if ken_burns:
            # zoompan: slow zoom-in from 1.0 to 1.05 over the clip duration
            zoom_expr = f"'min(zoom+{0.05/max(frames,1):.6f},1.05)'"
            vf = (
                f"zoompan=z={zoom_expr}:d={frames}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={width}x{height}:fps={fps}"
                f",scale={width}:{height}"
            )
        else:
            vf = f"scale={width}:{height}"

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", image_path,
            "-vf", vf,
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-an",  # no audio for now
            output_path,
        ]
        self._run(cmd)

    # ── Concatenate clips ─────────────────────────────────────────────────────

    def concat_clips(self, clip_paths: list[str], output_path: str) -> None:
        """Concatenate a list of MP4 clips using the concat demuxer."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for p in clip_paths:
                f.write(f"file '{os.path.abspath(p)}'\n")
            list_path = f.name
        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                output_path,
            ]
            self._run(cmd)
        finally:
            os.unlink(list_path)

    # ── Burn subtitles ────────────────────────────────────────────────────────

    def burn_subtitles(
        self,
        input_path: str,
        srt_path: str,
        output_path: str,
        subtitle_style: Optional[dict] = None,
    ) -> None:
        """Burn SRT subtitles into video with styled caption box."""
        style = subtitle_style or {}
        font_size = style.get("font_size", 38)
        box_opacity_pct = int(style.get("box_opacity", 0.55) * 100)

        # ASS override style via force_style
        force_style = (
            f"FontSize={font_size},"
            f"PrimaryColour=&H00FFFFFF,"
            f"BackColour=&H{_opacity_to_ass(style.get('box_opacity', 0.55))}000000,"
            f"BorderStyle=3,"  # opaque box
            f"Outline=0,"
            f"Shadow=0,"
            f"MarginV=120,"
            f"Alignment=2"  # bottom-center
        )

        # Escape path for FFmpeg subtitle filter
        safe_srt = srt_path.replace("\\", "/").replace(":", "\\:")

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", f"subtitles={safe_srt}:force_style='{force_style}'",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "copy",
            output_path,
        ]
        try:
            self._run(cmd)
        except RuntimeError as e:
            # Fallback: copy without subtitles if filter fails
            import shutil
            shutil.copy(input_path, output_path)

    # ── Logo watermark ────────────────────────────────────────────────────────

    def add_watermark(
        self,
        input_path: str,
        logo_path: str,
        output_path: str,
        position: str = "top_right",
        scale_w: int = 120,
    ) -> None:
        """Overlay a logo at the specified safe-area position."""
        margin = 40
        pos_map = {
            "top_right":    f"W-w-{margin}:{margin}",
            "top_left":     f"{margin}:{margin}",
            "bottom_right": f"W-w-{margin}:H-h-{margin}",
            "bottom_left":  f"{margin}:H-h-{margin}",
        }
        overlay = pos_map.get(position, pos_map["top_right"])

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-i", logo_path,
            "-filter_complex",
            f"[1:v]scale={scale_w}:-1[logo];[0:v][logo]overlay={overlay}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "copy",
            output_path,
        ]
        try:
            self._run(cmd)
        except RuntimeError:
            import shutil
            shutil.copy(input_path, output_path)

    # ── Trim + scale a video clip ─────────────────────────────────────────────

    def trim_and_scale_clip(
        self,
        input_path: str,
        output_path: str,
        duration: float,
        width: int = 1080,
        height: int = 1920,
    ) -> None:
        """Trim to duration and upscale/crop to width×height (cover fill, no letterbox)."""
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-t", str(duration),
            "-vf", vf,
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-an",
            output_path,
        ]
        self._run(cmd, timeout=180)

    # ── Add silent audio ──────────────────────────────────────────────────────

    def add_silent_audio(self, input_path: str, output_path: str) -> None:
        """Add a silent AAC audio track to a video."""
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_path,
        ]
        self._run(cmd)

    # ── Mix background music ──────────────────────────────────────────────────

    def mix_audio_track(
        self,
        video_path: str,
        music_path: str,
        output_path: str,
        music_volume: float = 0.15,
        fade_duration: float = 2.0,
    ) -> None:
        """Add background music to a (silent) video with volume control and fade-out."""
        info = _probe_duration(video_path)
        duration = info or 20.0
        fade_start = max(0.0, duration - fade_duration)

        # Videos are silent (no audio track), so use music directly as sole audio.
        af = (
            f"[1:a]volume={music_volume},"
            f"afade=t=out:st={fade_start:.2f}:d={fade_duration:.2f}[a]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-stream_loop", "-1",
            "-i", music_path,
            "-filter_complex", af,
            "-map", "0:v",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            output_path,
        ]
        self._run(cmd, timeout=180)


def _opacity_to_ass(opacity: float) -> str:
    """Convert 0.0-1.0 opacity to ASS alpha hex (inverted: 0=opaque, FF=transparent)."""
    alpha = int((1.0 - opacity) * 255)
    return f"{alpha:02X}"


def _probe_duration(path: str) -> float | None:
    """Return video duration in seconds via ffprobe, or None on failure."""
    import json as _json
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=15,
        )
        data = _json.loads(result.stdout)
        val = data.get("format", {}).get("duration")
        return float(val) if val else None
    except Exception:
        return None
