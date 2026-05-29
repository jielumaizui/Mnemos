# Mnemos - Your Second Brain that Never Forgets

> **Local Decision Brain & Behavior-Driven System**
>
> 🇨🇳 [中文完整版](README.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/CI.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

**Mnemos** is a fully automated AI Agent knowledge operating system. It takes AI conversations and user files as signal sources, requiring zero manual operation to complete knowledge acquisition, distillation, scoring, decision-making, and evolution.

From conversation records to knowledge入库, from persona inference to strategy injection, from freshness alerts to forced retrospectives — the entire pipeline runs automatically. **You just chat normally.**

Inspired by [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — letting LLMs incrementally build and maintain persistent knowledge bases. Mnemos goes one step further: **knowledge doesn't just get stored, it stays alive in decision-making.**

---

## The Problem We Solve

- You discuss a complex project with AI, come back two weeks later, and it's completely forgotten the context
- You keep hitting the same bugs because you can't remember past experiences
- You spend hours taking notes but can never find what you need when you need it
- Most of what you learn fades away, leaving you feeling like you've accomplished nothing
- You know you have knowledge gaps but have no idea where they are

**All these problems stem from one fact: human cognition is finite.**

Our brains are great for thinking and creating, but terrible for memory and retrieval. Mnemos was built to be your **second brain** — one that never forgets.

---

## What Mnemos Does

### 1. Permanent AI Memory
- Complete, lossless recording of all conversations with all AI assistants
- Cross-AI memory sharing: what you discussed with Claude is available to GPT
- No more context window limitations, no more repeating yourself

### 2. Universal File Parser
- Import any file: PDF, Word, Excel, PowerPoint, Markdown, HTML, EPUB, MOBI
- Automatically extracts core content, key concepts, and important data
- Batch import entire folders

### 3. Automatic Knowledge Extraction
- Every conversation is automatically distilled into structured knowledge within seconds
- Generates structured Wiki pages, permanently stored in your local knowledge base
- Automatically builds connections between knowledge to form your personal knowledge graph

### 4. Proactive Knowledge Delivery
- Predicts what knowledge you need and pushes it to you proactively
- When you discuss a topic, automatically surfaces relevant past knowledge
- Based on the Ebbinghaus forgetting curve, reminds you to review before you forget

### 5. Shadow Pages
- While you're thinking about a problem, the system silently retrieves all relevant knowledge in the background
- Generates a "shadow page" containing information you might have forgotten
- Helps you fill blind spots in your thinking

### 6. Knowledge Gap Detection & Forced Retrospectives
- Automatically analyzes your knowledge graph to identify gaps
- Weekly auto-generated personal growth review reports
- Forced retrospectives: the system evaluates urgency across 5 dimensions and automatically opens Obsidian when thresholds are met

### 7. Closed-Loop Self-Evolution
- The more you use it, the better it understands you
- Knowledge quality improves through usage feedback
- Grows with you as your personal cognitive extension

---

## How Mnemos Differs

| Dimension | Common Second Brain | Mnemos |
|-----------|---------------------|--------|
| System Position | Knowledge storage & retrieval | Local Decision Brain & Behavior-Driven System |
| Automation | Semi-automatic (manual tagging) | Fully automatic (acquire → score → distill → store → decide, zero manual) |
| Knowledge Flow | You → System → You search | System → AI Agent → Real-time decision assist |
| Quality Assurance | Deduplication (if any) | 7-layer distillation pipeline + adaptive scoring + 3 self-checks |
| Adaptability | Fixed rules | Cold-start rules → Bayesian adaptation → behavior feedback loop |
| User Modeling | None | 3-layer persona radar (energy/cognitive/value), drives decision strategy |
| Knowledge Lifecycle | Manual or none | Score-driven auto-evolution, freshness alerts, forced retrospective loop |
| Module Coupling | Monolithic | Hot-pluggable design, enable on demand |

---

## Quick Start

### Prerequisites
- Python >= 3.10
- An AI Agent (Claude Code, Hermes, OpenClaw, OpenCode, Codex, etc.)
- (Optional) [Memos](https://github.com/usememos/memos) instance — raw material entry
- (Optional) [Obsidian](https://obsidian.md) — knowledge base visualization

> **Note**: Mnemos is designed around the premise that you are already using an AI Agent. Without an AI Agent, core values (persona-driven, knowledge loading, decision assist) cannot be realized.

### One-Command Install (Recommended)

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
./setup.sh        # macOS / Linux
setup.bat         # Windows
```

`setup.sh` / `setup.bat` automatically:
1. Checks Python >= 3.10
2. Installs dependencies
3. Auto-detects Memos server (if running)
4. Auto-detects Obsidian Vault (if found)
5. Generates `~/.mnemos/configs/main.json`
6. Initializes standard wiki directory structure
7. Installs AI Agent hooks
8. Starts background daemon
9. Configures system scheduler (launchd / cron)

Non-interactive mode (for CI):
```bash
./setup.sh --yes
```

Skip Memos/Obsidian detection:
```bash
./setup.sh --skip-memos --skip-obsidian
```

### Manual Install

If you prefer manual configuration:

```bash
# Clone the repository
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos

# Create virtual environment (avoids PEP 668 system restrictions)
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Copy and edit configuration
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# Edit ~/.mnemos/configs/main.json with your paths

# Run system diagnosis
python3 mnemos_cli.py doctor
```

> **Note**: If you used `setup.sh` for installation, dependencies are installed in `.venv`. Use `.venv/bin/python mnemos_cli.py ...` for subsequent commands.

### Verify System is Working

```bash
# Check distillation queue
python3 core/kia/amphora.py --list

# Check daemon status
python3 -m mnemos_daemon status

# Check inbox for new content
ls ~/Documents/Obsidian\ Vault/wiki/00-Inbox/

# Check persona
python3 mnemos_cli.py persona summary

# Check scorer status
python3 mnemos_cli.py scorer status
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Application Layer — Decision Output                            │
│  IntentRouter │ ApplicationHub │ ContextAwareSearch            │
│  PredictivePush │ BlindspotDiscovery │ DisputeResolver         │
│  FreshnessAlert │ WeeklyReport │ ForcedRetrospective            │
├─────────────────────────────────────────────────────────────────┤
│  Knowledge Layer — Understanding & Modeling                     │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐│
│  │ Knowledge Graph      │  │ User Persona                     ││
│  │ EntityManager        │  │ 3-layer radar + cross-validation ││
│  │ RelationManager      │  │ Dialogue strategy + context iso  ││
│  │ EvolutionTracker     │  │ 14-dim evolution timeline        ││
│  │ ContextAwareQuery    │  │ Blindspot detection + calibration││
│  └──────────────────────┘  └──────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────┤
│  Scoring & Distillation — Quality Assurance                     │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐│
│  │ Adaptive Scoring     │  │ 7-layer Distillation Pipeline    ││
│  │ COLD/WARM/HOT 3-stage│  │ Noise→Judge→LLM→Extract→Check→ ││
│  │ 6 subsystem scorers  │  │ Link→Feedback                    ││
│  │ Feedback loop + drift│  │ PromptBuilder + TokenBudget      ││
│  └──────────────────────┘  └──────────────────────────────────┘│
├─────────────────────────────────────────────────────────────────┤
│  Sync Layer — Data Ingestion                                    │
│  SyncEngine (8-step pipeline) │ 5 Agent Sources │ FileIngestor │
│  TriggerSystem (Watchdog/Polling/Hybrid) │ AgentLifecycleMgr  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Design Principle: Reuse at Source

Mnemos **does not directly call any LLM API**. All LLM-required tasks (knowledge distillation, document parsing) are delegated to your local AI Agent via **AgentDelegate**.

**Rule: whoever launched Mnemos, gets the distillation tasks.**

Supported host agents:

| Adapter | Agent | Mechanism | Status |
|---------|-------|-----------|--------|
| Apollon | Claude Code | Hooks (settings.json) | Full |
| Caduceus | Hermes | Poll + Inbox | Full |
| Typhon | OpenClaw | SQLite + Hooks | Full |
| Musae | OpenCode | JSON Config + Hooks | Full |
| Daedalus | Codex | File-based + Windows .bat | Full |

This design avoids:
1. **Duplicate API Key configuration** — your Agent already has one
2. **Model version inconsistency** — distillation uses the same model you interact with
3. **Vendor lock-in** — supports any Agent following the file-delegation protocol

---

## Integration with AI Agents

### Method 1: MCP Protocol (Recommended, Universal)

Any MCP-compatible AI Agent can connect. After connection, the Agent can use tools such as:

- `wiki_search` — Search knowledge base
- `knowledge_distill` — Trigger knowledge distillation
- `preflight_inject` — Load relevant experience before tasks
- `guard_check` — Risk guard during execution
- `persona_summary` — Get user persona summary
- `context_aware_search` — Context-aware search with persona weighting
- `health_check` — System health check

### Method 2: Claude Code Hooks

Run `python3 mnemos_cli.py init` to automatically install hooks into `~/.claude/settings.json`.

### Method 3: Other Agents

Each agent connects via its own adapter mechanism (Poll / SQLite / JSON Config / File-based), automatically collecting conversation records without extra configuration.

---

## Configuration

Runtime config file: `~/.mnemos/configs/main.json`.
Legacy `~/.mnemos/config.yaml` is only used as a migration source.

```json
{
  "wiki": {
    "vault_path": "~/Documents/Obsidian Vault/wiki"
  },
  "memos": {
    "enabled": true,
    "api_url": "https://your-memos-instance.com",
    "token": ""
  },
  "daemon": {
    "services": {
      "capture_worker": true,
      "l1_sync": false,
      "event_bus": true
    }
  }
}
```

---

## Tech Stack

- **Language**: Python 3.10+
- **Storage**: Markdown files (knowledge base) + SQLite (persona/scoring/graph/scheduler)
- **Protocol**: MCP (Model Context Protocol) for AI Agent integration
- **Distillation**: AgentDelegate (reuse at source, no direct LLM API calls)
- **Scoring**: ComplementNB + TfidfVectorizer + Bayesian posterior update
- **Clustering**: HDBSCAN → DBSCAN → K-Means fallback chain
- **Scheduling**: Topological sort + ThreadPoolExecutor parallel execution
- **Document Processing**: PDF / PPT / Excel / Word / HTML / EBOOK parsing
- **Core Dependencies**: requests, pyyaml, watchdog, numpy

---

## Project Status

**Mnemos v2.0.0**

Major updates in v2.0.0:
- [x] Adaptive scoring engine: COLD/WARM/HOT 3-stage + 6 subsystem scorers
- [x] 7-layer distillation pipeline
- [x] Knowledge graph expansion with Bayesian confidence
- [x] Persona decision hub: 3-layer radar + cross-validation + 14-dim timeline
- [x] Application layer: IntentRouter, predictive push, dispute arbitration, freshness alerts
- [x] Sync framework: 8-step pipeline + 5 agent sources
- [x] KIA scheduler: topological parallel execution + auto-disable on failure
- [x] Incremental & deferred distillation

Planned:
- [ ] Vector index (hnswlib + Embedding API)
- [ ] Web Dashboard
- [ ] Obsidian plugin

---

## License

[MIT License](LICENSE)

**Mnemos** (/ˈnɛmɒs/) — from Greek mythology, the goddess of memory. Not just helping you remember, but making your AI automatically know when to recall and how to act.
