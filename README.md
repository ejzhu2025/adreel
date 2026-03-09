---
title: Video Agent Hero
emoji: 🎬
colorFrom: purple
colorTo: blue
sdk: docker
pinned: false
---

# Video Agent Hero 🎬

An **agentic short-form video generator** with a chat-driven two-phase pipeline:
**Plan → Review → Generate → Modify**

Powered by **LangGraph** + **Claude** + **fal.ai Wan 2.2** + **Google OAuth** + **Stripe**.

🌐 **Live demo**: [ejzhu2026-video-agent-hero.hf.space](https://ejzhu2026-video-agent-hero.hf.space)

---

## Features

- **Chat-driven UI** — describe your video, then refine via conversation
- **Two-phase pipeline** — see and edit the storyboard before generating
- **Turbo → HD upgrade** — quick 480p preview first, then upgrade to 720p
- **Smart partial re-render** — AI classifies feedback as global (full replan) or local (re-render only affected shots), saving time and cost
- **Real-time agent steps** — watch each pipeline node run with live elapsed timers
- **LLM auto-naming** — projects are automatically named by Claude Haiku after planning
- **Brand kit** — consistent logo, colors, fonts, subtitles across all videos
- **Background music** — auto-generated via Replicate MusicGen, mixed to video
- **Memory** — past projects stored in ChromaDB for semantic retrieval
- **Credit system** — per-shot billing with Stripe top-up; new users get 10 free credits
- **Google OAuth** — one-click sign-in, no passwords

---

## Architecture

```
POST /plan                POST /execute             POST /modify
     │                         │                         │
     ▼                         ▼                         ▼
intent_parser          executor_pipeline         change_classifier
memory_loader          caption_agent              ├─ local → partial_executor
clarification_planner  layout_branding            └─ global → planner_llm
planner_llm ◄──────    quality_gate                            │
plan_checker (loop)    qc_diagnose                    music_mixer
     │                 render_export              result_summarizer
     ▼                 music_mixer                memory_writer → END
  Plan JSON            result_summarizer
  saved to DB          memory_writer → END
```

**LangGraph** orchestrates 4 compiled graphs:

| Graph | Entry → Exit | Used by |
|-------|-------------|---------|
| `build_plan_only_graph` | `intent_parser` → `plan_checker` → END | `/plan` |
| `build_execute_only_graph` | `executor_pipeline` → `music_mixer` → `memory_writer` → END | `/execute` |
| `build_partial_rerender_graph` | `change_classifier` → … → `music_mixer` → `memory_writer` → END | `/modify` |
| `build_replan_graph` | `planner_llm` → … → `music_mixer` → `memory_writer` → END | `/feedback` |

---

## Pipeline Nodes

| Node | Role | External Call |
|------|------|--------------|
| `intent_parser` | Extracts platform / duration hints from brief | — |
| `memory_loader` | Loads brand kit (SQLite) + similar projects (ChromaDB) | SQLite, ChromaDB |
| `clarification_planner` | Detects missing fields, generates questions | — |
| `planner_llm` | Generates 4-shot Plan JSON; includes existing plan on replan | **Claude Sonnet** |
| `plan_checker` | Validates duration, shots, script; auto-fixes; loops ≤3× | — |
| `executor_pipeline` | Renders each shot via T2V (parallel, 6 workers) | **fal.ai wan/v2.2-a14b** |
| `caption_agent` | Maps script lines to shot durations → caption segments | — |
| `layout_branding` | Concat clips + burn subtitles + add logo watermark | FFmpeg |
| `quality_gate` | Probes resolution, duration, bitrate, frame content | FFmpeg (ffprobe) |
| `qc_diagnose` | Root-cause analysis; routes to retry / user action / proceed | Claude (fallback) |
| `render_export` | Final H.264 CRF23 + AAC 128k encode | FFmpeg |
| `music_mixer` | Infers tone from brief/mood, generates music, mixes into video | **Replicate MusicGen** |
| `result_summarizer` | Builds human-readable summary; deducts credits | — |
| `memory_writer` | Persists plan + output path + vector embedding | SQLite, ChromaDB |
| `change_classifier` | Classifies feedback as global/local; identifies shot indices | **Claude Haiku** |
| `partial_executor` | Re-renders only affected shots; reuses disk clips for the rest | **fal.ai wan/v2.2-a14b** |

---

## State (AgentState)

All nodes share a single `TypedDict` that flows through the graph:

```python
{
  # Identity
  "project_id": "a1b2c3d4",
  "brief": "Summer promo for Tong Sui Coconut Watermelon",
  "brand_id": "tong_sui",

  # Plan (output of planner_llm)
  "plan": {
    "platform": "tiktok", "duration_sec": 10,
    "concept": { "mood": "fresh", "visual_style": "..." },
    "script": { "hook": "...", "body": [...], "cta": "..." },
    "storyboard": [{ "scene": 1, "desc": "...", "duration": 2.5 }, ...],
    "shot_list":  [{ "shot_id": "S1", "text_overlay": "...", "duration": 2.5 }, ...],
    "_quality": "turbo"          # written after execute
  },

  # Execution
  "scene_clips": [{ "shot_id": "S1", "clip_path": "...", "duration": 2.5 }],
  "branded_clip_path": "data/projects/.../branded.mp4",
  "output_path": "data/exports/a1b2c3d4_9x16_....mp4",

  # Quality
  "quality": "turbo",            # "turbo" | "hd"
  "quality_result": { "passed": true, "issues": [] },

  # Partial re-render
  "change_type": "local",
  "affected_shot_indices": [1],
  "shot_updates": { "1": { "desc": "...", "text_overlay": "..." } },

  # Control
  "needs_replan": false,
  "plan_version": 1,
  "messages": [...]
}
```

---

## Credit System

| Action | Cost |
|--------|------|
| Turbo shot (480p) | **1 credit** |
| HD shot (720p) | **3 credits** |
| New user signup | **10 free credits** |

1 credit ≈ $0.10. A typical 5-shot turbo video costs **5 credits (~$0.50)**.

### Credit Packages (Stripe)

| Package | Credits | Price |
|---------|---------|-------|
| Starter | 50 | $5 |
| Pro | 200 | $15 |
| Studio | 500 | $30 |

Credits are deducted after successful generation. A pre-flight check prevents generation if balance is insufficient — the Approve bar stays visible so users can top up and retry.

---

## Video Quality Tiers

| Tier | Model | Frames | Resolution | Clip length |
|------|-------|--------|-----------|-------------|
| **Turbo** | `fal-ai/wan/v2.2-a14b/text-to-video` | 33 @ 16fps | 480p | ~2s |
| **HD** | `fal-ai/wan/v2.2-a14b/text-to-video` | 81 @ 16fps | 720p | ~5s |

---

## UI Flow

```
idle ──[Send brief]──► planning ──[done]──► plan_ready
                                                │
                                    Edit storyboard cards
                                    ⚡ Turbo / ✦ HD quality chip
                                    ~X credits estimate shown
                                                │
                                    [▶ Approve & Generate]
                                                │
                                           executing ──[done]──► done
                                                                   │
                                              ⚡ Turbo preview shown
                                              [✦ Upgrade to HD] button
                                                                   │
                                              [chat: "modify..."] ─┘
                                                     smart re-render
```

Chat bar states:

| State | Input placeholder | Action |
|-------|------------------|--------|
| `idle` / `error` | Describe your video... | Create project + plan |
| `plan_ready` | Ask to change the plan... | Replan with feedback |
| `done` | Modify this video... | Smart partial/global re-render |

---

## Project Structure

```
video-agent-hero/
├── web/
│   ├── server.py              # FastAPI + SSE streaming + inline HTML/JS frontend
│   ├── auth/                  # Google OAuth (router, models, deps)
│   └── billing/               # Stripe checkout + credit operations
├── agent/
│   ├── graph.py               # 4 LangGraph compiled graphs
│   ├── state.py               # AgentState TypedDict
│   ├── deps.py                # DB + VectorStore singletons
│   └── nodes/                 # 15 node functions (one file each)
├── render/
│   ├── fal_t2v.py             # fal.ai T2V wrapper (turbo/hd quality tiers)
│   ├── ffmpeg_composer.py     # concat, subtitles, watermark, trim/scale, music mix
│   ├── caption_renderer.py    # SRT/ASS subtitle file writer
│   └── frame_generator.py     # PIL placeholder frames (no-key fallback)
├── memory/
│   ├── db.py                  # SQLite (projects, brand_kits, user_prefs, feedback)
│   ├── vector_store.py        # ChromaDB semantic search
│   └── schemas.py             # Pydantic v2 models
├── cli/main.py                # Typer CLI (vah init/new/run/feedback/demo)
├── assets/                    # Brand assets (auto-generated on startup)
├── data/                      # Runtime data — gitignored
│   ├── vah.db                 # SQLite (projects + users + billing)
│   ├── chroma/                # ChromaDB
│   ├── projects/{id}/clips/   # Per-shot MP4s (reused on partial re-render)
│   └── exports/               # Final deliverable MP4s
├── Dockerfile                 # HuggingFace Spaces deployment
└── requirements.txt
```

---

## Quick Start (Local)

### Prerequisites

```bash
brew install ffmpeg        # macOS
# sudo apt install ffmpeg  # Ubuntu
python3.11 -m venv .venv && source .venv/bin/activate
```

### Install & Run

```bash
git clone https://github.com/ejzhu2025/video-agent-hero
cd video-agent-hero
pip install -r requirements.txt
uvicorn web.server:app --host 0.0.0.0 --port 7860 --reload
# Open http://localhost:7860
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | For AI planning | Claude Sonnet planner + Haiku classifier/namer |
| `FAL_KEY` | For video generation | fal.ai Wan 2.2 T2V |
| `GOOGLE_CLIENT_ID` | For auth | Google OAuth app client ID |
| `GOOGLE_CLIENT_SECRET` | For auth | Google OAuth app client secret |
| `STRIPE_SECRET_KEY` | For billing | Stripe API secret key |
| `STRIPE_WEBHOOK_SECRET` | For billing | Stripe webhook signing secret |
| `REPLICATE_API_TOKEN` | Optional | Replicate MusicGen background music |
| `VAH_DATA_DIR` | Optional | Data directory (default: `./data`) |
| `SESSION_SECRET` | Optional | Cookie signing secret (auto-generated if unset) |

Without `ANTHROPIC_API_KEY` / `FAL_KEY`: mock planner + PIL placeholder frames (useful for UI development).

---

## CLI Reference

```bash
pip install -e .   # installs `vah` command

vah init           # seed DB with Tong Sui brand kit
vah new --brief "..." [--brand X] [--user Y]
vah run --project ID [--yes]
vah feedback --project ID --text "..."
vah list
vah demo           # full end-to-end Tong Sui demo
```

---

## Output Spec

| Property | Value |
|----------|-------|
| Resolution | 1080 × 1920 (9:16 vertical) |
| Codec | H.264 (libx264), CRF 23 |
| Frame rate | 30 fps |
| Audio | AAC 128kbps |
| Captions | Burned-in SRT, branded box style |
| Logo | Watermark in configurable safe area |
| Music | Mixed at −18 dB under original audio |

---

## HuggingFace Spaces Deployment

Set these as **Space Secrets** (Settings → Repository secrets):

```
ANTHROPIC_API_KEY
FAL_KEY
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
REPLICATE_API_TOKEN   # optional, for background music
SESSION_SECRET        # any random string
```

Data is stored in `/data` (Docker volume). The app auto-seeds the brand kit on startup.
