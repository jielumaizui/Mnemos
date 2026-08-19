# Mnemos

**Local Decision Brain & Behavior-Driven System**

> A local-first memory, knowledge, and decision-support system for AI agents — it doesn't just remember; it teaches your AI when to recall and how to act.
>
> Current version v2.0.0: the core pipeline (capture → distill → store → decision support) is production-ready; advanced capabilities such as adaptive scoring and push precision keep improving as your data accumulates.
>
> 🌍 [中文版](README.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/ci.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

---

**Do these problems sound familiar?**

- You finished a complex project with an AI two weeks ago — ask it again today and the context is gone
- You hit the same problems over and over, re-searching and re-stepping into the same pits
- You spend hours taking notes and organizing docs, yet can never find them when it matters
- You learn a lot and forget most of it within weeks
- You know you have knowledge blind spots, but not where they are

**All of these are the same problem at root: human cognition is limited.**

Mnemos is a local-first memory, knowledge, and decision-support system for AI agents. It connects to all your AI assistants, records every conversation in full, automatically distills structured knowledge from them, builds your personal knowledge graph and user persona, and then — when you need it — proactively pushes the right knowledge back into your AI's workflow.

**You do zero extra organizing.** No notes, no tags, no searching. Just chat with your AI and work as usual, hand files to Mnemos, and everything else — capture, distillation, scoring, storage, push — runs automatically.

> **Honest boundaries**: v2.0.0 is not a fully autonomous cognitive system. High-risk writes, trusted-push enforce mode, data deletion, and some repair actions still require your explicit approval. There is no web control center either — everything happens through the CLI, MCP, config files, and Obsidian.

Inspired by [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — letting an LLM incrementally build and maintain a persistent knowledge base. Mnemos goes one step further with a decision-support layer: **knowledge shouldn't just sit in storage; it should stay alive in decisions.**

## How Is It Different from Other "Second Brain" Tools?

| Dimension | Typical Second Brain tools | Mnemos |
|-----------|---------------------------|--------|
| Positioning | Knowledge storage & retrieval | Local-first AI memory, knowledge & decision support |
| Automation | Semi-automatic (manual organizing/tagging) | Automatic capture → distill → store → push, zero manual organizing |
| Knowledge flow | You → system → you search it yourself | System → AI agent → real-time decision support |
| Quality assurance | Deduplication (if any) | Seven-layer distillation pipeline + quality gate + cognitive value gate + adaptive scoring |
| Adaptability | Fixed rules | Cold-start rules → Bayesian adaptation → behavioral feedback loop |
| User modeling | None | Cognitive persona (three-layer radar + refutable assertions + consumption-effect logs) driving decision strategy |
| Knowledge lifecycle | Manual or unmanaged | Score-driven evolution, staleness alerts, forced-retrospective loop |
| Write safety | Direct writes | Optional trusted-push loop: ProposalQueue → approval → append-only Journal → controlled writes |
| Coupling | Monolith | Hot-pluggable modules, enable on demand |

## Core Strengths

### Storage Is the Floor, Not the Selling Point

Knowledge storage and memory retrieval are Mnemos' most basic features. Hand it files (PDF/Word/PPT/Excel/HTML/EBOOK) and they're distilled into the knowledge base; AI conversations are automatically captured and distilled into structured knowledge — no manual organizing, no manual tagging. But that's only the beginning — **storing isn't the goal; using is.**

### 1. Adaptive Dynamic Adjustment Engine

The system is not a set of hard-coded rules but a continuously evolving judgment machine:

- **Three-phase cold start**: COLD (pure rules) → WARM (rules + Bayesian blend) → HOT (data-driven). Every adaptive module falls back to rules when data is scarce — it never goes on strike for "not enough data"
- **Bayesian scoring**: every piece of knowledge, entity, and relation carries a confidence score, with posteriors updated in real time as new evidence arrives
- **Feedback loop**: implicit signals (searches/clicks/ignores) + explicit feedback → weighted fusion → scorer retraining
- **Drift detection**: automatic model recalibration when feature distributions shift beyond threshold

The scoring engine covers 5 domain scorers (sync, raw capture, knowledge graph, persona, operational health) plus 1 standalone distill scorer. Each domain scores, evolves, and degrades independently. The adaptive policy matrix spans 9 domains (distillation, quality gate, scoring, delivery, search, document processing, and more); shadow experiments keep a 24-hour rollback window, and with no active shadow your explicit configuration is strictly respected.

### 2. Persona Decision Hub

The persona is not a wall of labels — it's a decision hub. The system infers your cognitive patterns and value priorities from your AI conversation behavior, and injects the persona into the AI's workflow:

- **Three-layer radar**: energy patterns (focus/activation/endurance/switching), cognitive patterns (abstraction/systems/questioning/creativity), value priorities (correctness/efficiency/depth/perfection/innovation/autonomy)
- **Refutable persona assertions**: corrections, ignores, interruptions, rework, and stated preferences become assertions with evidence, confidence, and privacy levels — low-confidence or corrected assertions can be rebutted or revoked by later evidence
- **Persona-driven dialogue strategy**: dynamically generates prompt fragments so the AI's style adapts to you — perfectionists get more rigorous suggestions, efficiency-first users get more concise plans
- **Consumption-effect loop**: preflight, search, distillation, and quality gates all record which assertions they used, whether behavior changed, and how it turned out — the persona no longer talks to itself
- **Context isolation**: personas evolve independently across work/personal/study contexts, avoiding cross-contamination
- **14-dimension evolution timeline**: long-term tracking of persona drift, with automatic detection of burnout signals, cognitive shifts, and value reversals

### 3. Forced Retrospective & Logic Self-Check

Storing knowledge is not the finish line — continuous verification is. The system tracks the knowledge lifecycle with budgets, weights, and your confirmation policy, intervening at key moments:

- **Weighted forced opening**: every pending retrospective is scored in real time across five dimensions — severity, wait time, recurrence frequency, current-context relevance, and promise breaches. Above threshold, the relevant Obsidian page opens automatically; below it, you get a light in-conversation reminder that doesn't break your flow
- **User-scheduled reminders open directly**: say "remind me to review this in 1 day" and the page opens on time — no weighting algorithm for your own appointments
- **Startup compensation**: appointments that expired while the machine was off are reissued on next launch
- **Retrospectives actually get consumed**: conclusions fan out through durable plans → commands → receipts into retrieval, policy patches, persona, scheduling, and scoring; negative feedback can revoke already-committed effects — a retrospective is no longer a document nobody reads
- **Seven-layer distillation pipeline**: noise filtering → value pre-judgment → LLM judgment → knowledge extraction → self-check → cross-agent linking → feedback loop. Before hitting the Wiki, pages must also pass a general quality gate and a cognitive value gate that demands an explicit cognitive contribution to decisions, methods, anti-patterns, or preferences
- **Dispute arbitration**: when new knowledge conflicts with existing knowledge, the system neither overwrites nor ignores — it generates an arbitration page recording the dispute and waits for your ruling
- **Incremental + deferred distillation**: long conversations draft incrementally every 5 turns; low-confidence content waits in a deferred queue until signals accumulate
- **Recycling guard**: prevents Wiki-injected content from being distilled back into the knowledge base, eliminating self-referential pollution

### 4. Trusted Writes & Hot-Pluggable Modules

- **Trusted-push loop (optional)**: `trusted_push.mode=off|shadow|enforce`. In enforce mode, distilled pages must enter the ProposalQueue and be approved by you before an append-only WriteJournal and controlled writer touch disk — the AI cannot silently rewrite your knowledge base; every write is auditable and reversible
- **Wiki projection lifecycle**: every create/update/move/delete of a formal Wiki page lands in an append-only mutation ledger first, then publishes events to six consumers (knowledge graph, cognitive graph, relation embeddings, search index, page metrics, MOC navigation), each returning its own receipt — the Wiki and its derived indexes never silently drift apart
- **Modular architecture**: 14+ subsystems (knowledge graph, shadow pages, DNA fingerprints, entropy engine, time capsules, …) run independently; disabling any of them leaves the core pipeline intact
- **KIA scheduler**: 16 scheduled steps executed in parallel via topological ordering; a module that keeps failing is auto-disabled instead of dragging everything down
- **Event-driven**: modules communicate through a loosely-coupled EventBus — distillation done → graph update → persona refresh → push evaluation, fully asynchronous
- **Resource governance**: `ResourceBudget` monitors CPU/memory/thermal/power; background tasks slow down (not shut down) under heat or battery

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Application Layer — decision output                             │
│  IntentRouter │ ContextAwareSearch │ PredictivePush              │
│  BlindspotDiscovery │ DisputeResolver │ ForcedRetrospective      │
│  FreshnessAlert │ PolicyPatch │ DeliveryRouter                   │
├─────────────────────────────────────────────────────────────────┤
│  Cognitive Layer — observe & reflect (L3/L4/L5)                  │
│  ObservationEngine (behavioral observations)                     │
│  ReflectionEngine (deviation detection + insights)               │
│  FeedbackLoop (attribution & revocation) │ CognitiveGraph        │
├─────────────────────────────────────────────────────────────────┤
│  Knowledge Layer — understanding & modeling                      │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ Knowledge Graph       │  │ User Persona                     │  │
│  │ EntityManager         │  │ 3-layer radar + refutable        │  │
│  │ RelationManager       │  │ assertions, dialogue strategy,   │  │
│  │ EvolutionTracker      │  │ context isolation, 14-dim timeline│  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  Scoring & Distillation — quality assurance                      │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ Adaptive scoring      │  │ 7-layer pipeline (Hephaestus)    │  │
│  │ COLD/WARM/HOT phases  │  │ noise→prejudge→LLM→extract→      │  │
│  │ 5 domain scorers +    │  │ selfcheck→link→feedback          │  │
│  │ distill scorer        │  │ quality gate + cognitive gate    │  │
│  │ feedback + drift      │  │ incremental + deferred distill   │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  Trusted Writes — auditable changes                              │
│  ProposalQueue │ PushDecisionGate │ WriteJournal (append-only)   │
│  WikiProjectionLedger │ ActionLedger │ SnapshotManager           │
├─────────────────────────────────────────────────────────────────┤
│  Sync Layer — data ingestion                                     │
│  Capture (MCP reports) │ SyncEngine 8-step pipeline              │
│  12 Agent Sources │ DocumentImport (PDF/Word/PPT/Excel/HTML/…)   │
├─────────────────────────────────────────────────────────────────┤
│  Daemon — 38 background services                                 │
│  raw_sync │ distill_and_merge │ persona_analyzer │ scheduler_tick│
│  observation_engine │ reflection_engine │ recap_consumption │ …  │
└─────────────────────────────────────────────────────────────────┘
```

## How the System Runs (6-Step Pipeline)

Mnemos' core value chain is **capture → sync → project → distill → store → assist**:

```
1. Capture
   Agent conversation ends / MCP report / user imports a file
        ↓
2. Sync
   SyncEngine normalizes raw content into append-only raw revisions
        ↓
3. Raw Projection
   The daemon projects current revisions into readable raw/<agent>/<date>/<chunk>.md
        ↓
4. Distill
   The Hephaestus seven-layer pipeline refines raw material into structured Wiki pages
        ↓
5. Store & Graph
   Default mode writes straight to the Wiki; enforce mode requires ProposalQueue approval
        ↓
6. Assist (KIA)
   Preflight preloading, Guard checks, PredictivePush, forced-retrospective loop
```

The whole chain uses persistent, revision-aware typed receipts: capture only completes when a matching queue receipt arrives; distillation only terminates with a formal page or an explicit intentional skip; partial, retry, and write failures stay non-terminal and recoverable.

## Distillation Execution Model

Mnemos **calls LLM APIs directly through the `DistillBackend` interface**, keeping quality controllable and the pipeline closed-loop. The production default is `LLMBackend` (an OpenAI-compatible HTTP caller); local CLI `AgentBackend`s may only run on a shadow-only evaluation surface and never enter the production write path.

**Design principle: distillation executes inside Mnemos, not inside the agent.**

Why not delegate distillation to agents:

1. **Uncontrollable quality** — an agent might bypass the Mnemos pipeline and handle material itself, voiding hard validation, knowledge-graph construction, and Wiki storage
2. **Unreliable conventions** — autonomous agent behavior cannot be forced; a "gentleman's agreement" will be violated
3. **Closed loop** — only by executing itself can Mnemos guarantee the full loop: raw material → distillation → hard validation → storage → knowledge graph

Mnemos implements ordered failover via **LLMApiChain** (primary / same-provider / cross-provider fallback chains) and is vendor-agnostic — any endpoint with an OpenAI-style API works.

## 5-Minute Walkthrough

> Follow this example once and you'll know exactly what Mnemos does.

### Scenario: You Had Claude Fix a Bug

**Step 1: Talk normally**

You ask Claude: "Why does asyncio.gather blow up memory?" After some debugging, you find the root cause. Conversation ends.

**Step 2: Distillation triggers automatically**

The session-end signal triggers distillation. The conversation flows through the seven-layer pipeline — noise filtering drops the chit-chat, value pre-judgment recognizes "valuable debugging experience", the LLM extracts structured knowledge, self-check validates claims and code snippets, and the quality gate plus cognitive value gate confirm it's not generic reference text. A knowledge card is born.

**Step 3: Scoring & controlled storage**

The adaptive scoring engine grades the card. Once scoring and the cognitive-contribution gate pass, default mode writes it into the knowledge base; with `trusted_push.mode=enforce`, the card enters the ProposalQueue and is stored only after your approval. The knowledge graph creates entities and relations in sync.

**Step 4: Persona learning**

The system captures signals from the conversation: deep focus and a questioning tendency during debugging, maybe an explicit correction like "test before committing". These become refutable persona assertions. Next time a similar scenario arises, preflight/search/distill/quality gates consume these assertions and record whether they actually changed behavior.

**Step 5: Proactive decision support**

A week later you start writing a high-concurrency crawler. IntentRouter recognizes the task intent, ContextAwareSearch retrieves the earlier debugging experience, and the persona hub judges you'd care about memory issues — so it reminds you about the asyncio.gather pitfall at the start of the conversation.

**The only thing you did: talk normally.**

### Verify the System Is Working

```bash
# 1. Check the distillation queue
python3 -m core.kia.amphora --list

# 2. Check daemon status
python3 mnemos_cli.py daemon status

# 3. Check the Inbox for new pages (default Wiki path)
ls ~/Documents/mnemos/00-Inbox/

# 4. View the persona
cat ~/Documents/mnemos/L5-Feedback/user-persona.md

# 5. Check scorer status
python3 mnemos_cli.py scorer status
```

## 🚀 Quick Start

### Prerequisites

- Python >= 3.10
- One AI agent (any of Claude Code, Kimi, Crush, Codex, Hermes, Kiro, OpenCode, OpenClaw)
- **Required** [Obsidian](https://obsidian.md): the raw Vault holds original conversations and the Mnemos Vault holds distilled knowledge — both must be openable in Obsidian for human review; setup stops with an explanation if Obsidian is not detected
- **Required** three model endpoints: LLM (chat/distillation), Embedding (vector/semantic recall), Reranker (search reranking). Each needs a model ID, API base URL, and API key
- **Optional** multimodal endpoint: parses images, screenshots, and visual evidence; the system works fine without it

> Mnemos is vendor-agnostic. Any endpoint compatible with the required API works — just provide model ID, base URL, and API key. Setup smoke-tests all three required endpoints and asks again on failure. API keys are stored in the system keyring by preference, never in plaintext.

### Product-Grade Install (Recommended)

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
python3 mnemos_cli.py setup --dry-run --json   # preview the install plan
python3 mnemos_cli.py setup                    # interactive install
```

`mnemos setup` is the recommended entry point. It threads configuration, Vault initialization, agent integration, scheduler, and deployment verification into a single install state machine:

1. Checks Python >= 3.10 and installs dependencies
2. Detects Obsidian and confirms the two default Vault paths (Mnemos + raw)
3. Generates `~/.mnemos/configs/main.json` (mode 0600)
4. Initializes the standard Wiki directory layout
5. Installs AI agent integration (adapter hooks + MCP config)
6. Starts the background daemon and registers the system scheduler (macOS launchd / Linux cron / Windows Task Scheduler)
7. Runs deployment verification: smoke tests for the three required endpoints + a read-only E2E probe

Fully automatic mode (non-interactive, macOS / Linux):

```bash
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key
python3 mnemos_cli.py setup --yes
```

`--yes` skips all prompts; any missing required endpoint or failed smoke test aborts immediately. On Windows, use `setup.bat` or set the equivalent environment variables in PowerShell.

Upgrade, repair, and uninstall share the same state machine:

```bash
python3 mnemos_cli.py upgrade plan --json
python3 mnemos_cli.py upgrade apply --json      # takes a global snapshot first
python3 mnemos_cli.py doctor repair-all --json
python3 mnemos_cli.py uninstall --preserve-data --json
```

`uninstall` preserves data by default; actual deletion requires a freeze, a snapshot reference, and a second confirmation.

### Manual Install

```bash
# 1. Clone and install
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e .

# 2. Copy and edit the config
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# Edit main.json: wiki path + base_url, model, api_key_source for llm/embedding/reranker

# 3. Diagnose and verify
python3 mnemos_cli.py doctor
python3 mnemos_cli.py setup --dry-run --json
python3 scripts/verify_installation.py --api-smoke
```

### Build the Semantic Search Index (Optional Boost)

Once the Embedding/Reranker endpoints pass their smoke tests, build the vector index to improve recall on unfamiliar queries:

```bash
pip install -e ".[ml]"                          # install hnswlib and other extras
python3 scripts/build_embedding_index.py        # build the index
```

Without it, the system falls back to an in-memory index — everything still works.

### Command-Line Tool

```bash
mnemos setup                       # install/configure/verify entry point
mnemos init                        # interactive config wizard
mnemos doctor                      # system diagnostics (repair subcommands available)
mnemos status                      # system overview
mnemos health --json               # machine-readable health check (30 checks)
mnemos config                      # view/edit configuration

# Agent management
mnemos agent list                  # list locally available AI agents
mnemos agent install               # install adapter hooks + MCP integration
mnemos agent doctor                # diagnose agent status

# Background services
mnemos daemon start|stop|status    # daemon management
mnemos scheduler install-windows   # register Windows startup task

# Pipeline
mnemos sync status                 # sync status
mnemos distill status              # distillation queue status
mnemos import <path>               # import documents (PDF/Word/PPT/Excel/HTML/EBOOK)
mnemos search <query>              # context-aware search
mnemos wiki read <page>            # read a Wiki page

# Cognitive layer
mnemos observe run                 # run the Observation Engine (L3)
mnemos reflect manual              # trigger a Reflection manually (L4)
mnemos feedback stats              # feedback loop statistics (L5)
mnemos persona behavior-metrics    # persona consumption metrics
mnemos recap list                  # retrospective queue

# Scoring & governance
mnemos scorer status               # scorer status and mode
mnemos kg doctor                   # knowledge-graph diagnostics
mnemos dispute list                # dispute arbitration list
mnemos blindspot list              # knowledge blind spots
mnemos data inventory --json       # data-ownership inventory
mnemos backup create               # global snapshot backup
```

There are 57 top-level commands; the unlisted ones are mostly advanced/debug/experimental — run `python3 mnemos_cli.py <command> --help` for details.

## AI Agent Integration

### Option 1: MCP Protocol (Recommended, Universal)

Any MCP-capable AI agent can connect. The MCP server registers **57 tools** in 5 groups:

**core — high-frequency loop**

| Tool | Purpose |
|------|---------|
| `preflight_inject` | Load relevant experience before a task (KIA loop step 1) |
| `guard_check` | Mid-execution risk guard, incl. analysis-loop/repeat-read alerts (KIA loop step 2) |
| `wiki_search` | Search the knowledge base |
| `wiki_read` | Read a specific page |
| `document_process` | Process a user-specified document into the distillation pipeline |

**lifecycle — session capture**

| Tool | Purpose |
|------|---------|
| `capture_turn` | Report a conversation turn (< 200ms enqueue) |
| `capture_session` | Batch-report a whole session |
| `end_session` | Mark a session as ended |
| `capture_status` | Query capture-queue status |

**extended — knowledge & retrospectives**

| Tool | Purpose |
|------|---------|
| `knowledge_ingest` | User-fed knowledge ("remember this") |
| `knowledge_distill` | Trigger knowledge distillation |
| `wiki_build` / `wiki_write` | Trigger a Wiki build / write a page |
| `memory_write_project` / `memory_write_framework` / `memory_write_global` | Scoped memory writes |
| `memory_search` | Search memory by project/framework/global scope |
| `session_search` | Search historical sessions |
| `check_pending_recaps` | Check pending retrospectives |
| `recap_start` / `recap_submit` / `recap_finalize` / `recap_skip` / `recap_feedback` / `recap_status` / `recap_claim_owner` | Full structured three-question retrospective flow |
| `retrospective_list` | List available retrospective experience |
| `persona_summary` / `persona_update` / `persona_behavior_prompt` / `persona_behavior_metrics` / `persona_record_explicit_evidence` | Persona queries, updates, and evidence recording |

**auxiliary — system & search**

| Tool | Purpose |
|------|---------|
| `health_check` | System health snapshot (same check set as the CLI) |
| `self_diagnose` / `detect_sources` | Self-diagnosis / data-source connectivity |
| `configure_wiki` | Configure the Wiki path |
| `context_aware_search` | Context-aware search (persona-weighted + graph recall) |
| `knowledge_source_list` | Knowledge-source distribution stats |
| `signal_collect` | Trigger signal collection |
| `build_cognitive_state` | Build a cognitive-state snapshot |
| `agent_runtime_probe` | Host runtime-capability acceptance probe |

**advanced — decision & cognition**

| Tool | Purpose |
|------|---------|
| `intent_route` / `intent_correct` | Intent routing and correction |
| `predictive_push` / `push_feedback` / `delivery_display_ack` | Predictive push and delivery-feedback loop |
| `blindspot_check` | Blind-spot detection |
| `freshness_check` | Knowledge-freshness check |
| `observation_run` / `observation_search` | Observation Engine (L3) |
| `reflect_on_input` / `reflect_manually` / `reflection_feedback` / `reflection_pending` | Reflection (L4) and feedback (L5) |
| `record_decision` / `apply_outcome` | Decision recording and outcome backfill |
| `wiki_write` | Controlled Wiki writes |

Configuration example:

```json
{
  "mnemos": {
    "command": "mnemos",
    "args": ["mcp", "serve"]
  }
}
```

Security model: `mnemos agent install` issues each host an independent high-entropy launch capability (only a keyring reference is stored — never plaintext); every tool call re-validates revocation/expiry. Cross-agent/project capabilities require your explicit grant (`mnemos agent grant-mcp`); callers cannot self-assert identity or escalate privileges.

### Option 2: Adapter Hooks (Claude Code / Kimi / Crush)

Hooks are installed automatically by `mnemos setup` or `mnemos agent install` (Claude Code writes to `~/.claude/settings.json`). If something breaks, run `mnemos doctor repair`.

### Option 3: MCP-Only Integration (Codex / Hermes / Kiro / OpenCode / OpenClaw)

These 5 agents connect through JSON MCP configuration — no hooks needed; `mnemos setup` writes their MCP config and active policy automatically.

### Passive Ingestion Sources (Aider / Gemini CLI / Cursor / Windsurf)

These 4 tools don't support active integration, but the daemon's `raw_sync` service periodically parses their local conversation files, feeding the same capture → distillation pipeline.

Per-agent integration docs live in `docs/integrations/`.

## Relationship with Obsidian

Mnemos and [Obsidian](https://obsidian.md) complement each other — neither replaces the other.

- Mnemos' knowledge layer is **plain Markdown + YAML frontmatter** — not bound to any specific tool
- Obsidian is required at deploy time because:
  1. **Native compatibility**: Obsidian notes are Markdown — no export/conversion
  2. **Bidirectional links**: `[[page name]]` syntax builds the knowledge graph automatically
  3. **Graph view**: Obsidian's Graph View is your knowledge-graph visualization
  4. **Community ecosystem**: Dataview, Templater, and other plugins interoperate with Mnemos data
  5. **Local-first**: consistent with Mnemos' data-privacy policy — all knowledge stays on disk
- Division of labor: Obsidian handles **organization, visualization, and human editing**; Mnemos handles **automatic capture, raw projection, distillation, scoring, persona, and closed-loop evolution**. Humans create; AI operates.
- Dual-Vault design: the raw Vault (default `~/Documents/raw`) holds readable projections of original agent conversations from `raw_events.db`; the Mnemos Vault (default `~/Documents/mnemos`) holds the distilled knowledge base

### Data Ownership

- Everything lives on your local disk: Wiki/raw as plain Markdown, runtime state in local SQLite — nothing is uploaded to any server
- `mnemos data inventory --json` lists where each data category lives, estimated record counts, consumers, and export/freeze/delete policies
- `mnemos data export` produces a redacted export manifest; `delete` requires a prior freeze, a snapshot reference, and confirmation
- Mnemos does not collect, upload, or share any of your data

## Configuration

The authoritative runtime config is `~/.mnemos/configs/main.json` (unified across platforms; legacy YAML is migrated automatically).

Precedence: **code defaults < JSON config file < environment variables** (env wins).

Main supported environment variables:

| Variable | Config key | Description |
|----------|-----------|-------------|
| `MNEMOS_DIR` | — | Mnemos data root (default `~/.mnemos`) |
| `MNEMOS_WIKI_DIR` / `WIKI_DIR` | `wiki.vault_path` | Wiki knowledge-base directory |
| `MNEMOS_LLM_API_KEY` / `MNEMOS_LLM_BASE_URL` / `MNEMOS_LLM_MODEL` | `llm.*` | LLM (chat/distillation) endpoint |
| `MNEMOS_EMBEDDING_API_KEY` / `MNEMOS_EMBEDDING_BASE_URL` / `MNEMOS_EMBEDDING_MODEL` | `embedding.*` | Embedding (vector/semantic recall) endpoint |
| `MNEMOS_RERANKER_API_KEY` / `MNEMOS_RERANKER_BASE_URL` / `MNEMOS_RERANKER_MODEL` | `reranker.*` | Reranker (search reranking) endpoint |
| `MNEMOS_MULTIMODAL_API_KEY` / `MNEMOS_MULTIMODAL_BASE_URL` / `MNEMOS_MULTIMODAL_MODEL` | `multimodal.*` | Multimodal (image parsing) endpoint, optional |

Key configuration example:

```json
{
  "wiki": {
    "vault_path": "~/Documents/mnemos"
  },
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://your-llm-api.example/v1",
    "api_key_source": "env:MNEMOS_LLM_API_KEY",
    "model": "your-llm-model-id"
  },
  "embedding": {
    "enabled": true,
    "base_url": "https://your-embedding-api.example/v1",
    "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
    "model": "your-embedding-model-id",
    "use_rerank": true
  },
  "reranker": {
    "enabled": true,
    "base_url": "https://your-reranker-api.example/v1",
    "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
    "model": "your-reranker-model-id"
  },
  "trusted_push": {
    "mode": "off"
  },
  "delivery": {
    "preference": "balanced"
  }
}
```

- **API key management**: `api_key_source` prefers `keyring:REF` (system keyring); `env:VAR` is an explicitly accepted fallback. Multiple keys per endpoint are supported via `api_key_sources` with automatic cooldown on 429/5xx
- **Delivery preference**: `delivery.preference` is `quiet` / `balanced` (default) / `active`, controlling proactive-push frequency and cooldowns
- **Trusted push**: `trusted_push.mode` is `off` (default) / `shadow` / `enforce`

## Data Sources & Privacy

Persona data sources are entirely your choice. Only AI-conversation capture is on by default; everything else requires explicit opt-in:

| Source | Used for | Privacy level |
|--------|----------|---------------|
| AI conversations | Inferring focus depth, questioning tendency, perfection preference | Local storage only |
| Git commits | Inferring endurance patterns, innovation tendency | Statistics only, no code stored |
| Wiki interactions | Inferring domains of interest, learning paths | Page paths and action types only |
| File system | Inferring active projects and rhythm | Local processing only, never uploaded |

Persona signals carry scope/context (work/personal/study isolation); every persona assertion keeps a privacy level, expiry, supporting/rebutting evidence, and a revision policy. Persona strategy injection can be turned off entirely via `persona.strategy_injection_enabled=false`.

## Tech Stack

- **Language**: Python 3.10+
- **Storage**: Markdown files (knowledge base) + SQLite (~20 local databases: raw events / persona / scoring / graph / scheduling / ledgers)
- **Protocol**: MCP (Model Context Protocol) for AI agent integration
- **Distillation execution**: Mnemos calls LLM APIs directly (LLMApiChain ordered failover, vendor-agnostic)
- **Scoring algorithms**: ComplementNB + TfidfVectorizer + Bayesian posterior updates
- **Vector index**: hnswlib (optional, `.[ml]` extra; in-memory fallback otherwise)
- **Scheduling**: topological ordering + ThreadPoolExecutor parallelism
- **Document processing**: PDF / PPT / Excel / Word / HTML / EBOOK parsing
- **Key management**: system keyring preferred, env references as explicit fallback
- **Core dependencies**: requests, pyyaml, jsonschema, watchdog, numpy, openai, anthropic, keyring, pypdf, python-docx, openpyxl, python-pptx, pdfplumber, beautifulsoup4, markdownify, ebooklib, psutil

## Project Status

**Mnemos v2.0.0** — core pipeline production-ready, advanced capabilities continuously improving.

### Available Now

- [x] **Sync framework**: SyncEngine 8-step pipeline + 12 agent sources + append-only raw revisions
- [x] **Seven-layer distillation pipeline**: noise filter → value pre-judgment → LLM judgment → extraction → self-check → cross-agent linking → feedback loop
- [x] **Knowledge graph**: entity/relation management + confidence governance + context-aware queries
- [x] **Scoring loop**: COLD/WARM/HOT phases + 5 domain scorers + distill scorer + drift detection
- [x] **Cognitive chain**: Observation (L3) → Reflection (L4) → Feedback (L5) + cross-layer cognitive graph
- [x] **Trusted-push loop**: ProposalQueue → approval → append-only Journal → controlled writes (off/shadow/enforce)
- [x] **Retrospective consumption loop**: conclusions actually land in retrieval/policy/persona/scheduling/scoring; negative feedback revokes
- [x] **MCP server**: 57 tools covering knowledge base / ingestion / sessions / KIA / persona / decisions / cognition / system
- [x] **Agent Kit**: 8 host agents (Claude Code / Kimi / Crush / Codex / Hermes / Kiro / OpenCode / OpenClaw) + 4 passive ingestion sources
- [x] **Document processing**: PDF / PPT / Excel / Word / HTML / EBOOK parsing
- [x] **Semantic search**: vendor-agnostic Embedding/Reranker endpoints + vector index
- [x] **Optional multimodal**: images/screenshots parsed and ingested when configured

### In Progress

- [ ] **Scorer cold start**: WARM/HOT modes need accumulated training samples; matures naturally with use
- [ ] **Push precision**: improves continuously as feedback data accumulates
- [ ] **Web dashboard / local control center**: use the CLI, MCP, and config files for now
- [ ] **Obsidian plugin**: bidirectional sync and inline queries

## Acknowledgements

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — originator of the LLM Wiki pattern, Mnemos' core inspiration
- [Obsidian](https://obsidian.md) — the benchmark knowledge-management tool, Mnemos' recommended knowledge-base visualization

## License

[MIT License](LICENSE)

---

**Mnemos** (/ˈnɛmɒs/) — named after Mnemosyne, the Greek goddess of memory. Not just remembering for you, but — within auditable, configurable, degradable boundaries — letting your AI recall the right knowledge at the right moment and act on it.
