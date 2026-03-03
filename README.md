# video-agent-hero 🎬

An **Agentic Video Generator** — plan-first, interactive, memory-aware, brand-consistent.

---

## Architecture

```
Brief ──► intent_parser ──► memory_loader ──► clarification_planner
                                                       │
                                          ┌── ask_user ─┘  (if missing info)
                                          │
                                          ▼
                                     planner_llm  ◄── (replan loop)
                                          │
                                     plan_checker ──► (replan if invalid)
                                          │
                                    executor_pipeline  (PIL frames → FFmpeg clips)
                                          │
                                     caption_agent  (proportional SRT from script)
                                          │
                                    layout_branding  (concat + subtitles + logo)
                                          │
                                     quality_gate  ◄── (auto-fix loop, max 2×)
                                          │
                                     render_export  (H.264 1080×1920 MP4)
                                          │
                                   result_summarizer
                                          │
                                    memory_writer  (SQLite + ChromaDB)
```

**LangGraph** orchestrates the state machine. Every node is a pure function:
`(state: dict) → dict` (partial state update).

---

## Quick Start

### Prerequisites

```bash
brew install ffmpeg          # macOS
# or: sudo apt install ffmpeg  # Ubuntu/Debian
python3.11 -m venv .venv && source .venv/bin/activate
```

### Install

```bash
git clone <repo> video-agent-hero && cd video-agent-hero
pip install -r requirements.txt
pip install -e .             # installs `vah` CLI command
cp .env.example .env         # optionally add ANTHROPIC_API_KEY
```

### Run the Demo

```bash
vah demo
```

Or step-by-step:

```bash
# 1. Init DB + load Tong Sui brand kit
vah init

# 2. Create a new project
vah new --brief "Create a summer promo video for Tong Sui's new drink Coconut Watermelon Refresh."
# → prints project ID, e.g. "a1b2c3d4"

# 3. Run full pipeline (answers clarification questions interactively)
vah run --project a1b2c3d4

# 3b. Or skip questions (use defaults)
vah run --project a1b2c3d4 --yes

# 4. Provide feedback and re-render
vah feedback --project a1b2c3d4 --text "Make it more energetic, add more product shots"

# 5. Check export path
vah export --project a1b2c3d4

# List all projects
vah list
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `vah init` | Create DB, load Tong Sui brand kit + user "ej" |
| `vah new --brief "…" [--brand X] [--user Y]` | Create project, returns ID |
| `vah run --project ID [--yes]` | Run full pipeline; `--yes` skips clarification |
| `vah feedback --project ID --text "…" [--rating 1-5]` | Store feedback + re-render |
| `vah export --project ID` | Print output path(s) |
| `vah list [-n 20]` | List recent projects |
| `vah demo` | Full end-to-end Tong Sui demo |

---

## Project Structure

```
video-agent-hero/
├── agent/
│   ├── graph.py              # LangGraph StateGraph (full + replan variants)
│   ├── state.py              # AgentState TypedDict + WorkingMemory
│   └── nodes/                # 13 node functions (one file each)
│       ├── intent_parser.py
│       ├── memory_loader.py
│       ├── clarification_planner.py
│       ├── ask_user.py
│       ├── planner_llm.py    # LLM or mock planner
│       ├── plan_checker.py
│       ├── executor_pipeline.py
│       ├── caption_agent.py
│       ├── layout_branding.py
│       ├── quality_gate.py
│       ├── render_export.py
│       ├── result_summarizer.py
│       └── memory_writer.py
├── memory/
│   ├── db.py                 # SQLite (brand_kits, user_prefs, projects, assets, feedback)
│   ├── vector_store.py       # ChromaDB wrapper
│   └── schemas.py            # Pydantic v2 schemas
├── render/
│   ├── frame_generator.py    # PIL: branded placeholder frames
│   ├── ffmpeg_composer.py    # FFmpeg: clips, concat, subtitles, watermark
│   └── caption_renderer.py   # SRT / ASS subtitle file writer
├── cli/
│   └── main.py               # Typer CLI (init/new/run/feedback/export/demo)
├── scripts/
│   └── create_assets.py      # Generate placeholder logo
├── tests/
│   ├── test_schemas.py
│   └── test_plan_checker.py
├── assets/                   # Brand assets (auto-generated on init)
├── data/                     # Runtime data (DB, chroma, exports) — gitignored
├── Makefile
└── requirements.txt
```

---

## Memory Design

### Short-term (in-process)
- `WorkingMemory` dict (replaces Redis for MVP) — holds conversation answers, plan drafts, render params
- Passed through `AgentState` across all LangGraph nodes

### Long-term (SQLite `data/vah.db`)

| Table | Contents |
|-------|----------|
| `brand_kits` | Brand JSON keyed by `brand_id` |
| `user_prefs` | User preference JSON keyed by `user_id` |
| `projects` | Project record, latest plan JSON, output paths |
| `assets` | Asset file paths per brand |
| `feedback` | Text + rating feedback per project |

### Vector Memory (ChromaDB `data/chroma`)
- Stores project summary embeddings
- Retrieved by semantic similarity for `memory_loader` (top-K relevant past projects)
- Metadata filters: `brand_id`, `platform`, `language`

---

## LLM + Mock Mode

| Condition | Behavior |
|-----------|----------|
| `ANTHROPIC_API_KEY` set | Uses `claude-opus-4-6` via LangChain |
| `OPENAI_API_KEY` set | Uses `gpt-4o` via LangChain |
| No key | Deterministic mock planner (Tong Sui demo works offline) |

---

## Extending to Real T2V/I2V

The executor pipeline is intentionally modular. To plug in real video generation:

1. **Add an asset resolver** in `executor_pipeline.py`:
   ```python
   # Before calling fc.image_to_clip(), check for real asset:
   if shot["asset"] != "generate":
       asset_path = db.get_asset(brand_id, shot["asset"])
       if asset_path:
           # Use real image/video
   ```

2. **Add T2I/T2V node** (e.g. via fal.ai):
   ```python
   # render/t2v_generator.py
   def generate_clip_fal(prompt: str, duration: float) -> str:
       # POST to fal.ai wan/v2.1/t2v-14b
       ...
   ```

3. **Register as a LangGraph node** and wire after `executor_pipeline`.

The `FrameGenerator` placeholder output is intentionally high-resolution (1080×1920) so FFmpeg clips drop in without rescaling.

---

## Tests

```bash
make test
# or
python -m pytest tests/ -v
```

---

## Output Spec

| Property | Value |
|----------|-------|
| Resolution | 1080 × 1920 px (9:16) |
| Codec | H.264 (libx264), CRF 23 |
| Frame rate | 30 fps |
| Audio | AAC 128kbps (silent if no voiceover) |
| Captions | Burned-in SRT, branded box style |
| Logo | Watermark in safe area (top-right default) |
| Duration | 15 / 20 / 30 s |

---

## Configuration

Copy `.env.example` to `.env`:

```ini
ANTHROPIC_API_KEY=          # optional — enables real LLM planner
OPENAI_API_KEY=             # optional — fallback LLM
VAH_DATA_DIR=./data         # where DB, chroma, exports go
VAH_ASSETS_DIR=./assets     # brand assets
```
