# AdReel

An **AI-powered video ad generator** for short-form social media. Paste a product URL → AI plans a storyboard → renders a polished 9:16 MP4 → exports platform-ready copy and covers for TikTok, Instagram, and 小红书.

Powered by **LangGraph** + **Claude Sonnet 4.6** + **fal.ai** + **Replicate** + **Gemini** + **FFmpeg**.

Live at: [adreel.studio](https://adreel.studio)

---

## Features

- **Chat-driven UI** — describe your product, refine via conversation
- **Two-phase pipeline** — review and edit the storyboard before generating
- **Multi-backend rendering** — fal.ai (T2V / T2I / I2V / transitions), Replicate (T2V / I2V), Gemini (T2I concept images)
- **4-stage Creative Pipeline** — Director → Storyboard → Critic → PromptCompiler runs inside the planner
- **Smart partial re-render** — AI classifies feedback as global (full replan) or local (affected shots only)
- **Relevance re-render** — shots with low visual-relevance scores are automatically re-rendered
- **Brand kit** — consistent logo, colors, fonts, subtitles across all videos
- **Background music** — auto-generated via Replicate MusicGen, mixed to video
- **Product image scraping** — paste any URL, Gemini extracts brand info and product image
- **TikTok publishing** — OAuth flow to post videos directly to TikTok
- **Marketing automation** — discover brands, generate ads, produce platform copy, track conversions
- **AI team** — 5-agent system (PM / SDE / QA / DevOps / Data) that self-manages the service
- **Credit system** — per-shot billing with Stripe top-up; new users get free credits
- **Google OAuth** — one-click sign-in
- **Memory** — past projects stored in ChromaDB for semantic retrieval

---

## Architecture

```
POST /api/projects/{id}/plan        POST /api/projects/{id}/execute     POST /api/projects/{id}/modify
           │                                       │                                  │
           ▼                                       ▼                                  ▼
    intent_parser                       executor_pipeline                  change_classifier
    memory_loader                       caption_agent                      ├─ local → partial_executor
    clarification_planner               layout_branding                    └─ global → planner_llm
    ask_user (if fields missing)        quality_gate                                  │
    planner_llm ◄── Creative Pipeline  qc_diagnose                        music_mixer
    plan_checker (loop ≤3×)            relevance_rerender (low score)     result_summarizer
           │                           render_export                      memory_writer → END
           ▼                           music_mixer
       Plan JSON                       result_summarizer
       saved to DB                     memory_writer → END
```

**LangGraph** compiles 4 graphs:

| Graph | Triggered by | Nodes |
|-------|-------------|-------|
| `build_plan_only_graph` | `POST /plan` | intent_parser → plan_checker → END |
| `build_execute_only_graph` | `POST /execute` | executor_pipeline → render_export → memory_writer → END |
| `build_partial_rerender_graph` | `POST /modify` | change_classifier → partial_executor or full replan → END |
| `build_replan_graph` | `POST /feedback` | planner_llm → executor_pipeline → render_export → END |

---

## Pipeline Nodes

| Node | Role | External Call |
|------|------|--------------|
| `intent_parser` | Extracts platform / duration / tone from brief | — |
| `memory_loader` | Loads brand kit (SQLite) + similar projects (ChromaDB) | SQLite, ChromaDB |
| `clarification_planner` | Detects missing fields; generates clarifying questions | — |
| `ask_user` | Collects missing fields interactively *(skipped if all known)* | — |
| `planner_llm` | Runs 4-stage Creative Pipeline → storyboard + T2V prompts | **Claude Sonnet 4.6** |
| `plan_checker` | Validates shot count, durations, script; auto-fixes; loops ≤3× | — |
| `executor_pipeline` | Renders shots in parallel (6 workers) via fal.ai / Replicate | **fal.ai, Replicate** |
| `caption_agent` | Maps script lines to timed subtitle segments | — |
| `layout_branding` | Burns captions + brand logo onto clips | FFmpeg / PIL |
| `quality_gate` | Checks resolution, duration, bitrate; VLM scores each shot's relevance | FFmpeg, Claude |
| `qc_diagnose` | Root-cause analysis; routes to retry / user action / proceed | Claude |
| `relevance_rerender` | Re-renders shots with low visual-relevance scores (up to 2 attempts) | **fal.ai, Replicate** |
| `render_export` | Final H.264 CRF23 + AAC 128k encode | FFmpeg |
| `music_mixer` | Infers tone, generates and mixes background music track | **Replicate MusicGen** |
| `result_summarizer` | Compiles summary; deducts credits | — |
| `memory_writer` | Persists plan + output path + vector embedding | SQLite, ChromaDB |
| `change_classifier` | Classifies feedback as global/local; identifies affected shot indices | **Claude Haiku** |
| `partial_executor` | Re-renders only affected shots; reuses cached clips for the rest | **fal.ai, Replicate** |
| `creative_pipeline` | Sub-pipeline: Director → Storyboard → Critic → PromptCompiler | **Claude Sonnet 4.6** |

---

## Creative Pipeline (inside planner_llm)

```
Brief + Brand Kit
       │
       ▼
① Director          — generates 3 creative concepts, picks the strongest
       │               (hook archetypes: pov-immersion / problem-contrast / asmr-reveal / micro-story / social-proof)
       ▼
② Storyboard        — expands concept into shot-by-shot plan with concept images (Gemini T2I)
       │               (scene desc, duration, narrative beat, transition language)
       ▼
③ Critic            — reviews plan, patches VFX violations via JSON Patch
       │
       ▼
④ PromptCompiler    — writes one optimized T2V/I2V prompt per shot
       │
       ▼
   storyboard plan  +  concept_images (one per shot)  +  {shot_id: {positive, negative}} prompt dict
```

Each stage is one Claude API call (~20–30 s, ~3–4 min total).

---

## Render Backends

| Backend | Models | Used for |
|---------|--------|---------|
| **fal.ai** | `wan/v2.2-a14b/text-to-video` | T2V shot rendering |
| **fal.ai** | `flux/schnell` | T2I (product / concept images) |
| **fal.ai** | `wan/v2.2-a14b/image-to-video` | I2V when product image available |
| **fal.ai** | transition model | Shot transitions |
| **Replicate** | Wan 2.2 T2V | T2V fallback |
| **Replicate** | I2V model | I2V fallback |
| **Gemini** | `gemini-2.0-flash` | T2I concept images per shot (storyboarding) |

`shot_renderer.py` auto-selects T2I→I2V when a product image is present, otherwise T2V.

---

## State (AgentState)

```python
{
  # Identity
  "project_id": "a1b2c3d4",
  "brief": "Summer promo for Tong Sui Coconut Watermelon",
  "brand_id": "tong_sui",
  "product_image_path": "data/projects/a1b2c3d4/product.png",  # optional

  # Plan
  "plan": {
    "platform": "tiktok", "duration_sec": 10,
    "concept": { "mood": "fresh", "visual_style": "..." },
    "script": { "hook": "...", "body": [...], "cta": "..." },
    "storyboard": [{ "scene": 1, "desc": "...", "duration": 2.5 }],
    "shot_list":  [{ "shot_id": "S1", "text_overlay": "...", "duration": 2.5 }],
  },

  # Creative
  "creative_concept": { "hook_archetype": "...", "color_palette": [...] },
  "t2v_prompts": { "S1": { "positive": "...", "negative": "..." } },
  "concept_images": { "S1": "data/projects/.../concept_S1.png" },  # Gemini T2I

  # Execution
  "scene_clips": [{ "shot_id": "S1", "clip_path": "...", "duration": 2.5 }],
  "branded_clip_path": "data/projects/.../branded.mp4",
  "output_path": "data/exports/a1b2c3d4_9x16_....mp4",

  # Quality
  "quality": "turbo",              # "turbo" | "hd"
  "quality_result": { "passed": true, "issues": [] },
  "relevance_rerender_attempt": 0, # up to 2

  # Partial re-render
  "change_type": "local",
  "affected_shot_indices": [1],
  "shot_updates": { "1": { "desc": "...", "text_overlay": "..." } },

  # Control
  "needs_replan": false,
  "needs_user_action": false,
  "qc_user_message": null,
  "plan_version": 1,
  "token_usage": { "input": 12400, "output": 3200 },
  "messages": [...]
}
```

---

## Marketing Module

Automates the end-to-end brand outreach pipeline.

```
marketing new --url https://allbirds.com --size medium
      │
      ▼
1. brand_finder.py       BrandLead(url, size, category)
      │
      ▼
2. web/scrape_product.py Gemini scrapes website → brand info (name, colors, brief, logo)
      │
      ▼
3. campaign_runner.py    Creates BrandKit + project in DB → runs LangGraph pipeline
      │
      ▼
4. content_packager.py   Extracts cover frames + Claude-written copy (3 platforms)
      │
      ▼
5. tracker.py            Records campaign in marketing.db
      │
      ▼
   marketing/output/{date}/{brand}/
   ├── video.mp4
   ├── cover_tiktok.jpg / cover_instagram.jpg / cover_xiaohongshu.jpg
   ├── tiktok.txt  /  instagram.txt  /  xiaohongshu.txt
```

### Marketing CLI

```bash
python -m marketing.cli new  --url https://gymshark.com --size medium --quality turbo
python -m marketing.cli batch --file brands.csv --limit 10
python -m marketing.cli find  --count 10 --category fashion [--run]
python -m marketing.cli log   --campaign proj_abc123 --platform instagram --post-id 123
python -m marketing.cli sync  --post post_xyz456 --media-id 17896132429627826
python -m marketing.cli report
```

---

## AI Team

A 5-agent autonomous team that manages adreel.studio.

```bash
python -m ai_team health       # QA + DevOps health check
python -m ai_team weekly       # PM + Data weekly report
python -m ai_team bug "..."    # SDE investigates and fixes
python -m ai_team feature "..." # SDE implements
python -m ai_team review       # Code review recent changes
python -m ai_team deploy "..." # DevOps deploy to Cloud Run
python -m ai_team data         # Data usage report
python -m ai_team funnel       # Funnel analysis
python -m ai_team test [URL]   # QA regression test
python -m ai_team ask "..."    # Answer a codebase question
```

| Agent | Role |
|-------|------|
| **PM Agent** | Triage feedback, write sprint plans, weekly reports |
| **SDE Agent** | Read/write code, fix bugs, implement features |
| **QA Agent** | Test live API, health checks, bug reports |
| **DevOps Agent** | Monitor Cloud Run, check logs, trigger deploys |
| **Data Agent** | Query DB, funnel analysis, error trends |

---

## Credit System

| Action | Cost |
|--------|------|
| Turbo shot (480p, ~2 s) | 1 credit |
| HD shot (720p, ~5 s) | 3 credits |
| New user signup | 10 free credits |

1 credit ≈ $0.10. A typical 5-shot turbo video costs **5 credits (~$0.50)**.

### Stripe Packages

| Package | Credits | Price |
|---------|---------|-------|
| Starter | 50 | $5 |
| Pro | 200 | $15 |
| Studio | 500 | $30 |

---

## Output Spec

| Property | Value |
|----------|-------|
| Resolution | 1080 × 1920 (9:16 vertical) |
| Codec | H.264 (libx264), CRF 23 |
| Frame rate | 30 fps |
| Audio | AAC 128 kbps |
| Captions | Burned-in SRT, branded box style |
| Logo | Watermark in configurable safe area |
| Music | Mixed at −18 dB |

---

## Project Structure

```
adreel/
├── web/
│   ├── server.py              # FastAPI app (adreel.studio), SSE streaming, inline HTML/JS
│   ├── auth/                  # Google OAuth (router, models, deps)
│   ├── billing/               # Stripe checkout + credit operations
│   ├── brand_kit_api.py       # Brand kit management endpoints
│   ├── tiktok.py              # TikTok OAuth + Content API video publishing
│   ├── routers/
│   │   ├── projects.py        # Project CRUD + pipeline trigger endpoints
│   │   └── scrape.py          # POST /api/scrape-product (Gemini brand extraction)
│   ├── feedback_api.py        # User feedback collection
│   ├── changelog.json         # Product changelog
│   └── templates.py           # Landing page + app HTML
├── agent/
│   ├── graph.py               # 4 LangGraph compiled graphs
│   ├── state.py               # AgentState TypedDict
│   ├── deps.py                # DB + VectorStore singletons
│   └── nodes/                 # 17 node functions (one file each)
├── render/
│   ├── fal_t2v.py             # fal.ai T2V wrapper
│   ├── fal_t2i.py             # fal.ai T2I (flux/schnell)
│   ├── fal_i2v.py             # fal.ai I2V
│   ├── fal_transition.py      # fal.ai shot transitions
│   ├── replicate_t2v.py       # Replicate T2V fallback
│   ├── replicate_i2v.py       # Replicate I2V fallback
│   ├── gemini_t2i.py          # Gemini T2I (concept images)
│   ├── shot_renderer.py       # Orchestrates T2I→I2V or T2V per shot
│   ├── ffmpeg_composer.py     # Concat, subtitles, watermark, music mix
│   ├── caption_renderer.py    # SRT/ASS subtitle file writer
│   └── frame_generator.py     # PIL placeholder frames (no-key fallback)
├── marketing/
│   ├── brand_finder.py        # Product Hunt / CSV brand discovery
│   ├── campaign_runner.py     # End-to-end campaign orchestration
│   ├── content_packager.py    # Cover extraction + Claude copy generation
│   ├── tracker.py             # Campaign + post tracking (SQLite)
│   ├── notifier.py            # Telegram notifications
│   └── daily_runner.py        # Scheduled daily pipeline
├── ai_team/
│   ├── orchestrator.py        # CLI entry point — routes to agents
│   ├── pm_agent.py
│   ├── sde_agent.py
│   ├── qa_agent.py
│   ├── devops_agent.py
│   ├── data_agent.py
│   ├── base_agent.py          # Anthropic SDK agentic tool-use loop
│   └── tools.py               # All tool implementations + definitions
├── eval/                      # Video quality evaluation (6 metrics)
├── memory/
│   ├── db.py                  # SQLite (projects, brand_kits, users, billing)
│   ├── vector_store.py        # ChromaDB semantic search
│   └── schemas.py             # Pydantic v2 models
├── cli/main.py                # Typer CLI (vah init/new/run/feedback/demo)
├── assets/                    # Brand assets (logo, favicon)
├── Dockerfile                 # Cloud Run container
├── cloudbuild.yaml            # Cloud Build CI/CD
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
git clone https://github.com/ejzhu2025/adreel
cd adreel
pip install -r requirements.txt
cp .env.example .env       # fill in keys
uvicorn web.server:app --host 0.0.0.0 --port 8080 --reload
# Open http://localhost:8080
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude Sonnet 4.6 (planning, QC, classification) |
| `FAL_KEY` | For video gen | fal.ai T2V / T2I / I2V |
| `REPLICATE_API_TOKEN` | For video gen | Replicate T2V / I2V + MusicGen |
| `GOOGLE_API_KEY` | For storyboarding | Gemini T2I concept images + brand scraping |
| `GOOGLE_CLIENT_ID` | For auth | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | For auth | Google OAuth client secret |
| `STRIPE_SECRET_KEY` | For billing | Stripe API secret |
| `STRIPE_WEBHOOK_SECRET` | For billing | Stripe webhook signing secret |
| `PRODUCT_HUNT_TOKEN` | Marketing only | Brand discovery from Product Hunt |
| `INSTAGRAM_ACCESS_TOKEN` | Marketing only | Instagram Graph API analytics |
| `TELEGRAM_BOT_TOKEN` | Optional | PM insights daily Telegram notifications |
| `REDIS_URL` | Optional | Redis for state (falls back to in-memory dict) |
| `VAH_DATA_DIR` | Optional | Data directory (default: `./data`) |
| `SESSION_SECRET` | Optional | Cookie signing secret (auto-generated if unset) |

Without `ANTHROPIC_API_KEY` / `FAL_KEY`: mock planner + PIL placeholder frames (UI development mode).

---

## CLI Reference

```bash
pip install -e .   # installs `vah` command

vah init           # seed DB with Tong Sui demo brand kit
vah new --brief "..." [--brand X] [--user Y]
vah run --project ID [--yes]
vah feedback --project ID --text "..."
vah list
vah demo           # full end-to-end Tong Sui demo
```

---

## Deployment (Google Cloud Run)

```bash
# Manual deploy
bash deploy_cloudrun.sh

# CI/CD via Cloud Build (triggered on push to main)
# See cloudbuild.yaml
```

Set all environment variables as Cloud Run secrets. Data is stored in `$VAH_DATA_DIR` (mount a persistent volume or use Turso for cloud-synced SQLite).

---

## Evaluation

`eval/` contains a video quality evaluation framework with 6 metrics: prompt adherence, temporal consistency, visual defects, audio alignment, narrative coherence, and cost/latency. Run with:

```bash
python -m eval.runner --project-id <id>
```
