# Mnemos - Local AI Memory and Decision Support

> **Local-first AI memory, knowledge, and decision-support system**
>
> 🇨🇳 [中文完整版](README.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/CI.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

**Mnemos** is a local-first AI Agent memory, knowledge, and decision-support system. It captures authorized conversation and file signals through Agent Kit, MCP, CLI, daemon services, and local source parsers; keeps canonical raw evidence; distills useful knowledge; builds Wiki/KG/search surfaces; and injects relevant context back into AI workflows.

As of 2026-07-11, the daemon PID file uses `mnemos.daemon_instance.v2` and the heartbeat uses `mnemos.daemon_heartbeat.v3`. `daemon status/stop` and strict health verify OS start/boot/executable facts, runtime-code fingerprint, the config-file byte hash, the canonical effective-config fingerprint, database identity, and the exact current service manifest. PID reuse, incomplete evidence, or effective-config drift caused by env/performance-tier changes fails closed without sending a signal, and `start` reports success only after the current instance heartbeat is durable.

The Capture → Amphora → Hephaestus → recap path now uses durable, revision-aware typed receipts. Capture reaches `done` only after a matching Amphora enqueue receipt; distillation is terminal only after a durable page or an explicit intentional-skip receipt; trusted proposals, partial results, retries, and write failures remain non-terminal. Audit historical gaps with `python3 scripts/reconcile_pipeline_receipts.py`; add `--apply` only after reviewing the plan and taking database backups.

As of 2026-07-11, recap consumption itself uses a durable fan-out outbox. Requested labels are mapped to canonical retrieval, policy, follow-up, persona, scheduler, and scoring consumers; a recap becomes `consumed` only when every required command has a committed or explicit intentional-skip receipt. `recap_feedback` runs a correction outbox that cancels or compensates committed effects, exposes partial/retryable state, and requires the latest `supersedes_event_id` for conflicting feedback. Audit production schemas with `python3 scripts/reconcile_recap_consumption.py --json`; use `--apply --json` only after stopping the daemon and reviewing the four-database backup scope.

Wiki projections have their own append-only lifecycle ledger. Every create, update, move, or delete produces a stable `page_id`, a causal `page_revision`, and a tombstone when applicable. EventBus closes a mutation only after the Knowledge Graph, Cognitive Graph, relation embeddings, Wiki search index, metrics, and MOC consumers each return a typed `ack` or `noop`; retries, deferrals, dead letters, daemon restarts, and out-of-order revisions remain visible. `scripts/rebuild_wiki_projection_state.py` is read-only by default and provides an explicit backup-and-rebuild path with full/incremental/isolated comparison and receipt reconciliation. See the [Wiki projection lifecycle contract](docs/WIKI_PROJECTION_LIFECYCLE.md).

Canonical raw turns now separate stable logical aliases from append-only immutable revisions. `raw_turns.current_revision_id` selects the current bytes, while superseded `raw_turn_revisions` remain addressable by `raw-revision:<revision_id>`. Capture handoffs, Amphora tasks, distilled fragments, and Wiki pages carry revision-plus-span provenance; durable edges protect referenced raw data from retention. `session_search` authorizes metadata before fetching canonical revision bodies, so RawIndex and Markdown projections are candidate hints rather than evidence authorities. `python3 scripts/reconcile_raw_revision_provenance.py` is dry-run by default; `--apply` backs up the database, records provable edges, and marks unprovable historical pages as `pending_rebuild` instead of inventing provenance.

Complete distillation no longer compresses long code blocks into head/tail excerpts or drops the fourth and later shell commands. Except for explicit private `[thinking]...[/thinking]` blocks, `clean_message_content()` preserves visible content and formatting. Standard and chunked extractors build canonical input with `lossless=True`: tiny total or per-message budgets record `budget_overflow_tokens` instead of invoking head/tail or message truncation, while private exclusions expose only type, span, and counts. WikiBuilder's plain-text fallback no longer takes a 500-character prefix; token pressure is handled by lossless `split_to_tokens()` chunks so first, middle, and tail evidence all reach extractor input. Chunk checkpoint hashes and `chunk_info` declare `lossless-visible-v1`; legacy unversioned checkpoints miss and are re-extracted instead of being reused.

Chunk recovery also requires the complete `mnemos.distill_execution_spec.v2`. The exact rendered prompt, output schema, extract/parse/quality code digests, explicit provider/model/backend routing, merge contract, all output-affecting effective settings, and the immutable `DistillInputSpec` hash determine `execution_spec_hash`. The model root must pass the `distill_output_v4` schema-owned conditional rules and typed runtime validator: skip is legal only with empty fragments/claims plus a non-empty reason and source-bound evidence, while knowledge/skill requires fragments, claims, a non-skip intent, behavior intent, and the complete 19-field `cognition_episode`; artifact, relation, and cognitive-action dependencies are conditional schema rules as well. The same validation runs before and after correction, on checkpoint save/load, and before formal write; both checkpoint save and reuse receive the full immutable input spec. `CheckpointAdmission` persists the input-spec hash, output-contract version, canonical-root hash, and judgment. Before any formal sink, the canonical episode revision, event, and projection outbox are atomically committed to the single `CognitiveStateStore`. `create_page` additionally requires an engine-issued `FragmentRouteCapability` bound to the root/input hashes and the post-admission fragment object references, so a direct caller cannot swap fragments after admission. Chunk provenance records cache hits, miss reasons, and field-level spec differences. Old schemas, missing root/admission, corrupt spec/payload metadata, or any effective-field change must miss; a failed run under a new spec cannot overwrite an older successful generation. `python3 scripts/audit_distill_output_contract.py --strict --json` verifies the release contract. `python3 scripts/reconcile_distill_execution_checkpoints.py --json` is read-only by default; after stopping the daemon, use explicit `--apply --backup-dir <dir> --json` to back up and migrate while retaining old rows as non-reusable history.

The core capture -> distill -> store path is usable in v2.0.0, but Mnemos is not a fully autonomous cognitive system and it does not force the trusted-push decision loop by default. Trusted Push is configurable: `off` keeps the legacy write path, `shadow` records proposals without formal Journal writes, and `enforce` requires ProposalQueue / Journal / Writer approval before formal writes. Formal Markdown write/delete/move fallback commits now require a typed receipt bound to the exact target, content hash and expected-existing hash; moves also bind the source and its hash. `python3 -m core.trust.static_scan` v4 uses AST callsite analysis rather than directory or whole-file marker allowances. The current denominator is 169 sinks: 143 exact non-formal/recovery registry entries, 17 receipt-dominated formal callsites, seven central-writer sinks, and two primitive sinks; unknown, stale, known-bypass, and forged guarded classifications fail closed. There is no local Web dashboard/control center yet; configuration and operations currently use the CLI, MCP tools, and `~/.mnemos/configs/main.json`.

Inspired by [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — letting LLMs incrementally build and maintain persistent knowledge bases. Mnemos goes one step further: **knowledge doesn't just get stored, it stays alive in decision-making.**

---

## The Problem We Solve

- You discuss a complex project with AI, come back two weeks later, and it's completely forgotten the context
- You keep hitting the same bugs because you can't remember past experiences
- You spend hours taking notes but can never find what you need when you need it
- Most of what you learn fades away, leaving you feeling like you've accomplished nothing
- You know you have knowledge gaps but have no idea where they are

**All these problems stem from one fact: human cognition is finite.**

Our brains are great for thinking and creating, but limited at memory and retrieval. Mnemos is built as a local memory layer that helps AI assistants recall durable, source-backed knowledge during work.

---

## What Mnemos Does

### 1. Permanent AI Memory
- Canonical raw capture for supported and authorized AI assistants, with fidelity explicitly reported as full, derived, or partial
- Cross-agent recall for supported sources and Agent Kit targets, subject to access control and source fidelity
- No more context window limitations, no more repeating yourself

### 2. Universal File Parser
- Import any file: PDF, Word, Excel, PowerPoint, Markdown, HTML, EPUB, MOBI
- Automatically extracts core content, key concepts, and important data
- Batch import entire folders

### 3. Automatic Knowledge Extraction
- Valuable conversations can be distilled into structured knowledge through the configured queue and LLM backend
- Generates structured Wiki pages, permanently stored in your local knowledge base
- Automatically builds connections between knowledge to form your personal knowledge graph

### 4. Proactive Knowledge Delivery
- Predicts what knowledge you need and pushes it to you proactively
- When you discuss a topic, surfaces relevant past knowledge when search/preflight/push gates match
- Based on the Ebbinghaus forgetting curve, reminds you to review before you forget

### 5. Shadow Pages
- While you're thinking about a problem, the system silently retrieves all relevant knowledge in the background
- Generates a "shadow page" containing information you might have forgotten
- Helps you fill blind spots in your thinking

### 6. Knowledge Gap Detection & Forced Retrospectives
- Automatically analyzes your knowledge graph to identify gaps
- Weekly auto-generated personal growth review reports
- Forced retrospectives: the system evaluates urgency across 5 dimensions and can open Obsidian or surface dialog reminders according to configured budgets

### 7. Closed-Loop Self-Evolution
- The more you use it, the better it understands you
- Knowledge quality improves through usage feedback
- Grows with you as your personal cognitive extension

---

## How Mnemos Differs

| Dimension | Common Second Brain | Mnemos |
|-----------|---------------------|--------|
| System Position | Knowledge storage & retrieval | Local AI memory, knowledge, and decision support |
| Automation | Semi-automatic (manual tagging) | Automated capture/distill/store where configured; high-risk writes, deletion, and enforce-mode decisions require explicit approval |
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
- [Obsidian](https://obsidian.md) — knowledge base (Required)
- Required model endpoints for LLM, Embedding, and Reranker. Each endpoint needs a model ID, API base URL, and API key.

> **Note**: Mnemos does not require a specific vendor. Any compatible endpoint can be used if you provide the model ID, API base URL, and API key. Setup smoke-tests LLM, Embedding, and Reranker separately; failed checks prompt again in interactive mode and fail in `--yes` mode. Reranker `base_url` may be either the service root or the full endpoint ending in `/rerank`.
>
> If Obsidian is not detected during setup, setup stops with an explanation: the raw Vault stores original Agent conversations, and the Mnemos Vault stores distilled cognitive knowledge; both must be openable and reviewable in Obsidian. Install Obsidian first, then rerun setup.

### One-Command Install (Recommended)

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
./setup.sh        # macOS / Linux
```

Windows PowerShell:

```powershell
.\setup.bat
```

The installer labels each prompt as `LLM`, `Embedding`, or `Reranker` so model IDs do not get entered into the wrong slot.

`setup.sh` / `setup.bat` automatically:
1. Checks Python >= 3.10
2. Installs dependencies
3. Verifies Obsidian is installed and confirms the Mnemos/raw Vault paths; if Obsidian is missing, explains why and stops deployment
4. Generates `~/.mnemos/configs/main.json`
5. Initializes standard wiki directory structure
6. Installs AI Agent active access (adapter hooks + MCP-only config/policy)
7. Starts background daemon
8. Configures or prints scheduler setup: macOS writes launchd; Linux prints a cron command with runtime environment variables; Windows calls `mnemos scheduler install-windows` and prints the manual command if registration fails
9. Runs deployment verification: model endpoints must be usable; installed targets must first pass static conformance and then prove runtime full power with authorization plus a recent MCP health/synthetic-safe completeness receipt; targets that are not installed are skipped

Non-interactive mode (macOS / Linux):
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
./setup.sh --yes
```

Non-interactive mode (Windows PowerShell):
```powershell
$env:MNEMOS_LLM_MODEL="your_llm_model_id"
$env:MNEMOS_LLM_BASE_URL="https://your-llm-api.example/v1"
$env:MNEMOS_LLM_API_KEY="your_llm_key"
$env:MNEMOS_EMBEDDING_MODEL="your_embedding_model_id"
$env:MNEMOS_EMBEDDING_BASE_URL="https://your-embedding-api.example/v1"
$env:MNEMOS_EMBEDDING_API_KEY="your_embedding_key"
$env:MNEMOS_RERANKER_MODEL="your_reranker_model_id"
$env:MNEMOS_RERANKER_BASE_URL="https://your-reranker-api.example/v1"
$env:MNEMOS_RERANKER_API_KEY="your_reranker_key"
.\setup.bat --yes
```

`--yes` never prompts for model settings. If any required endpoint is missing or fails smoke, setup exits with code 1.

### Manual Install

If you prefer manual configuration:

```bash
# Clone the repository
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos

# Install dependencies
pip install -e .

# Copy and edit configuration
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# Edit ~/.mnemos/configs/main.json with your paths and llm/embedding/reranker base_url, model, api_key_source=env:...
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key

# Verify model endpoints and run system diagnosis
# Doctor text, diagnostic JSON, distill status, and E2E dry-run paths are redacted by default; use --unsafe-debug only for private local debugging.
python3 verify_installation.py --api-smoke
mnemos doctor
```

Validation is hermetic by default. The quick, integration, system, heavy, diagnostics, and non-real-API full-score entrypoints create one `mnemos.hermetic_run_environment.v1` root that owns HOME, Mnemos/database/wiki, XDG, temporary, bytecode-cache, and artifact paths. The `system` layer is the OS-neutral system-test entrypoint shared by the Linux, macOS, and Windows CI matrix; workflows must not recreate it with shell-specific temporary-directory or environment syntax. Its manifest records `environment_hash`, `outside_write_count`, and `formal_state_diff`; API credentials are absent unless `run_full_score_gates.py --real-api` is explicitly selected. A supplied `--output-dir` is the sandbox root and must be absent or empty, so existing evidence is never cleared or reused. Health, status, distill status, and `scripts/verify_installation.py` are read-only by default; use `--write-probes` only for an explicit unique-file permission probe. Golden benchmark runs require an explicit output directory or the run-owned `MNEMOS_RUN_ARTIFACTS_DIR`, rather than a shared `~/.mnemos/benchmarks/golden/latest` directory.

Quality-debt validation separates development ratchets from release closure. `scripts/check_maintainability_budget.py --closure` tracks every broad catch by an exact AST fingerprint and requires time-bounded owner, telemetry, and removal metadata; parse failures, same-count replacement, expired acceptance, and a baseline that was not tightened after improvement fail closed. `scripts/check_zombie_code_policy.py --closure` applies the same rule to compatibility candidates. Accepted residual debt may pass local development checks only with `release_eligible=false`; strict full-score runs add `--closure --strict --json` and require zero residual debt. The vulture whitelist and its CI baseline are both fixed at zero.

Release certification is separate from a focused diagnostic run. `--strict --real-api` rejects `--only` and every skip selector. `mnemos.full_score_gates.v2` is release eligible only when the current canonical 44-gate manifest has identical expected, selected, and executed sets, no omitted gate, all required receipts pass, and the run is bound to a clean full Git commit. The denominator includes three strict maintainability/zombie/vulture zero-closure gates and the required-Desktop `docs.asset_manifest.strict` gate. Each receipt binds its stdout/stderr SHA-256; verify the artifact with `scripts/verify_full_score_certificate.py`. A successful partial run remains `certifying=false`. `scripts/audit_test_suite_denominator.py` currently proves all 480 pytest files belong to exactly one quick/integration/heavy layer, while `scripts/run_cognitive_behavior_scenarios.py` executes the behavior files promised by the scenario matrix.

Documentation and prompt assets use the canonical `mnemos.document_asset_manifest.v1` contract. The current denominator is 65/65 tracked Markdown files, 23/23 prompt/schema assets, and 25/25 Desktop system-map assets, with zero exclusions and zero unverified assets. Freshness and sensitive-data audits share tracked-file discovery; prompt entries bind exact hashes, real consumer symbols, and schema/inline output contracts; Desktop current contracts bind both current-state and repo anchors, while generated indexes bind the current commit. Verify it with `python3 scripts/audit_document_asset_manifest.py --strict --desktop-mode required --json`.

`core/kia/relation_evidence_schema.py` is the only DDL/version/hash authority for `knowledge_graph.db.relation_evidence`. `KnowledgeGraph` and `RelationManager` validate the existing columns, defaults, foreign key, index, registry version, and semantic hash before any constructor-side DDL. Preview with `python3 scripts/reconcile_relation_evidence_schema.py --json`; after stopping the daemon and confirming no missing evidence type, apply with `--apply --backup-dir <dir> --json`. The apply path creates and verifies a SQLite backup, migrates transactionally, preserves row counts, and refuses unknown or ambiguous data. `python3 scripts/audit_schema_registry.py --strict --json` enforces the single owner in local, pre-commit, CI, and full-score gates.

```bash
python3 scripts/run_tests.py quick
python3 scripts/run_tests.py integration
python3 scripts/run_tests.py system  # System tests only; OS-neutral hermetic CI entrypoint
python3 scripts/run_tests.py heavy
python3 scripts/audit_gate_hermeticity.py --suite diagnostics --strict --json --output-dir /tmp/mnemos-diagnostics-hermetic
python3 scripts/run_full_score_gates.py --strict --real-api
python3 scripts/verify_full_score_certificate.py /tmp/mnemos-full-score-release/full_score_gates.json
```

SQLite databases are stored as canonical plaintext `.db` files; whole-file SQLite encryption artifacts are removed. Sensitive values are controlled through field redaction, secret inventory checks, and `env:` / `keyring:` / `keyref:` references. `checks.sqlite_disk_budget` monitors `.db-wal`, Mnemos temp files, snapshots, and `raw_events.db` growth; WAL checkpoint and stale temp cleanup are safe repairs, while snapshot and raw event deletion require user confirmation.

### Verify System is Working

```bash
# Check distillation queue
python3 core/kia/amphora.py --list

# Check daemon status
python3 -m mnemos_daemon status

# Check inbox for new content
ls ~/Documents/mnemos/00-Inbox/

# Check persona
cat ~/Documents/mnemos/L5-Feedback/user-persona.md
mnemos calibrate

# Check scorer status
mnemos scorer status
```

### CLI Commands

```bash
mnemos init                       # Interactive setup wizard
mnemos doctor                     # System diagnosis
mnemos status                     # View system status
mnemos config                     # View/edit configuration

# Agent management
mnemos agent list                 # List available AI Agents
mnemos agent install              # Install adapter hooks + MCP-only active access
mnemos agent detect               # Detect installed Agents
mnemos agent doctor               # Diagnose Agent status

# Background services
mnemos daemon start               # Start background daemon
mnemos daemon stop                # Stop background daemon
mnemos daemon status              # View daemon status
mnemos scheduler install-windows  # Register Windows startup
mnemos scheduler uninstall-windows # Unregister Windows startup

# Event system
mnemos events stats               # View event queue statistics
mnemos events cleanup             # Clean up expired events

# Scoring system
mnemos scorer status              # View scorer status
mnemos scorer retrain             # Manual retraining trigger
mnemos scorer rollback            # Rollback to previous model

# Sync system
mnemos sync status                # View sync status
mnemos sync retry-failed          # Retry failed sync tasks

# Search & reports
mnemos search <query>             # Context-aware search
mnemos report generate            # Generate weekly persona report

# Other
mnemos calibrate                  # Launch persona calibration
mnemos mcp serve                  # Start MCP server
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
│  │ KGEventHandler       │  │ Event-driven updates             ││
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
│  SyncEngine (8-step pipeline) │ 12 Agent Sources │ FileIngestor │
│  TriggerSystem (Watchdog/Polling/Hybrid) │ AgentLifecycleMgr  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Design Principle: Quality Control

Mnemos **directly calls LLM APIs** for all distillation tasks, ensuring quality control and closed-loop processes.

**Rule: distillation execution stays in Mnemos, not the Agent.**

Why not delegate distillation to the Agent:
1. **Quality uncontrollable** — Agents may bypass Mnemos pipelines and process files independently, causing hard validation, knowledge graph construction, and Wiki ingestion to all fail
2. **Agreements unreliable** — Agent autonomous behavior cannot be forcibly constrained; "gentlemen's agreements" will inevitably be violated
3. **Closed-loop process** — only Mnemos executing itself guarantees the complete loop: raw material → distillation → hard validation → ingestion → knowledge graph

Mnemos uses **LLMApiChain** for ordered failover based on `llm.chain`, while keeping the primary / same-provider / cross-provider fields for compatibility and preserving additional backup nodes. It is managed centrally in `core/llm_config.py`.

---

## Integration with AI Agents

### Method 1: MCP Protocol (Recommended, Universal)

Any MCP-compatible AI Agent can connect. After connection, the Agent can use tools such as:

`mnemos agent install` issues a distinct high-entropy launch capability per host. Host configuration stores only a `0600` keyring reference; the plaintext capability is not persisted in config, backups, logs, or the authorization database. At stdio startup, Mnemos resolves an immutable server-side `PrincipalEnvelope` from `AgentAuthorizationStore` and revalidates revocation/expiry before every tool call. All 51 tools must match the shared policy registry. Callers cannot self-assert or expand identity with `agent`, `source_agent`, `allow_cross_agent`, or `authorized_agents`; `session_id` and `project` may only narrow the server grant.

Wiki/raw/search candidates require a complete ACL envelope and fail closed on missing, conflicting, or unproven provenance. `wiki_read` normalizes the path and authorizes frontmatter before reading the body. Heat, training, persona, search-session, click, reminder-cooldown, and push-history effects receive authorized results only. Cross-agent/project access is explicitly managed with `mnemos agent grant-mcp <agent> ...`; updating or revoking a grant immediately invalidates older launch capabilities, after which that host must be reinstalled.

- `wiki_search` — Search knowledge base
- `wiki_read` — Read specific wiki page
- `wiki_write` — Write to wiki page
- `knowledge_ingest` — Ingest user-provided knowledge
- `knowledge_import` — Import local file to knowledge base
- `knowledge_distill` — Trigger knowledge distillation
- `document_process` — Import documents through the single-owner path: canonical raw → capture outbox → Amphora → quality gate → Wiki; returns accepted/pending immediately, while `mode=parse` remains preview-only
- `capture_turn` — Report single conversation turn (< 200ms)
- `capture_session` — Batch report entire session
- `end_session` — Mark session as complete
- `capture_status` — Query capture queue status
- `session_search` — Search historical sessions
- `preflight_inject` — Load relevant experience before tasks
- `guard_check` — Risk guard during execution
- `persona_summary` — Get user persona summary
- `persona_behavior_prompt` — Get persona-driven behavior prompt
- `persona_update` — Trigger persona update
- `signal_collect` — Trigger signal collection
- `context_aware_search` — Context-aware search with persona weighting
- `intent_route` — Intent routing (recall/knowledge/task/chat)
- `blindspot_check` — Knowledge gap detection

`preflight_inject` and `guard_check` are high-frequency Agent entrypoints. The persona signal store uses a 2-second default SQLite connect/busy-wait budget. If the daemon temporarily holds a persona SQLite connection and `PreFlightInjector` cannot initialize, MCP should return a successful degraded response instead of a tool execution error: `preflight_inject` includes `degraded_reason`, and `guard_check` falls back to the default high-risk guard checklist.

Policy patches are matched only against the current task/subtype/context and an explicit project scope; patch content never proves its own trigger. Non-global patches require an exact project match. Candidates are ranked by task fit and matched trigger evidence, deduplicated, and capped by `policy_patch.max_active`. KIA responses expose `match_source=current_context`, `matched_triggers`, `task_fit_score`, `dedupe_key`, and `interruption_budget_ok`. Reflection key-point prose is explanation metadata, not trigger input. Audit stored triggers with `python3 scripts/reconcile_policy_patch_triggers.py --json`; `--apply --json` creates a database backup before changing rows.
The same boundary applies to reflection and persona metrics: when the persona store is temporarily unavailable, reflection continues with `persona_store=None`, and `persona_behavior_metrics` returns base behavior metrics with an empty `profile_usage` section.
`guard_alert` events emitted by `guard_check` are droppable telemetry. When the current process has no EventBus consumers, `publish_event` skips global EventBus initialization so a daemon-held `events.db` lock cannot turn the MCP response into a tool error.
- `freshness_check` — Knowledge freshness check
- `predictive_push` — Proactive knowledge recommendation
- `health_check` — Canonical 30-check health snapshot shared with the CLI
- `agent_runtime_probe` — Record a content-free runtime receipt from a fixed synthetic-safe host sample

### Method 2: Claude Code Hooks

Run `mnemos init` or `mnemos agent install` to install Claude hooks into `~/.claude/settings.json`.

### Method 3: Codex / Hermes / Kiro / OpenCode / OpenClaw

Codex, Hermes, Kiro, OpenCode, and OpenClaw use MCP-only active access plus passive local session sources. `setup.sh` and `mnemos agent install` configure their MCP server entries and active policy blocks by default.

Agent Kit v2 checks `codex`, `claude`, `hermes`, `opencode`, `openclaw`, `crush`, `kiro`, and `kimi`. Missing targets are `not_installed` and N/A. Installation, MCP/Policy, passive-source fidelity, and cognitive capability declarations yield only `conformance_ok`. Runtime `full_power` additionally requires content authorization, a recent authenticated call to the canonical `health_check`, and a valid fixed `mnemos.agent_runtime_probe.v1` completeness sample. Missing, stale, malformed, unauthorized, or check-set-mismatched receipts are strict failures; old reports are unknown rather than green. The receipt stores metadata only, never the sample text.

Continuous native capture is separately owned by the default-enabled `daemon.raw_sync` schedule derived from the same manifest. Watchdog, polling, and hybrid triggers accelerate a dirty source but are never the only capture gate. The daemon heartbeat carries a privacy-safe per-source discovery/capture/cursor/gap/error projection, and `python3 scripts/audit_agent_source_coverage.py --strict --json` independently verifies the active owner and Native-to-Raw coverage rather than treating installation or a one-off backfill as proof.

---

## Configuration

Runtime config file: `~/.mnemos/configs/main.json`.
Legacy `~/.mnemos/config.yaml` is only used as a migration source.

```json
{
  "wiki": {
    "vault_path": "~/Documents/mnemos",
    "subdirs": [
      "00-Inbox", "01-People", "02-Projects", "03-Tech",
      "04-Concepts", "05-MOCs", "06-Retrospectives", "07-Shadow",
      "99-Reports"
    ]
  },
  "storage": {
    "backend": "obsidian",
    "obsidian": {
      "vault_path": "~/Documents/raw"
    }
  },
  "daemon": {
    "services": {
      "capture_worker": true,
      "eventbus": true
    }
  },
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://your-llm-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_LLM_API_KEY",
    "model": "your-llm-model-id"
  },
  "embedding": {
    "enabled": true,
    "provider": "openai-compatible",
    "base_url": "https://your-embedding-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
    "model": "your-embedding-model-id"
  },
  "reranker": {
    "enabled": true,
    "provider": "openai-compatible",
    "base_url": "https://your-reranker-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
    "model": "your-reranker-model-id"
  }
}
```

---

## Tech Stack

- **Language**: Python 3.10+
- **Storage**: Markdown files (knowledge base) + SQLite (persona/scoring/graph/scheduler)
- **Protocol**: MCP (Model Context Protocol) for AI Agent integration
- **Model APIs**: Mnemos directly calls LLM, Embedding, and Reranker endpoints after setup smoke validation
- **Scoring**: ComplementNB + TfidfVectorizer + Bayesian posterior update
- **Clustering**: HDBSCAN → DBSCAN → K-Means fallback chain
- **Scheduling**: Topological sort + ThreadPoolExecutor parallel execution
- **Document Processing**: PDF / PPT / Excel / Word / HTML / EBOOK parsing
- **Core Dependencies**: requests, pyyaml, watchdog, numpy

---

## Project Status

Release security uses the machine-readable `mnemos.security_audit.v2` contract. Run
`python3 scripts/security_audit.py --strict --json`; Bandit, pip-audit, and health-security
results are normalized into typed findings, and the report derives counts, status, `ok`, and
the process exit code from those findings with the invariant `ok == (blocking_count == 0)`.
`python3 scripts/audit_release_privacy_security.py --strict --json` validates that contract
again before aggregating config, documentation, repository-literal, and diagnostic-redaction
checks. Any blocking finding stops release; warnings remain explicit non-blocking evidence.

**Mnemos v2.0.0**

Major updates in v2.0.0:
- [x] Adaptive scoring engine: COLD/WARM/HOT 3-stage + 6 subsystem scorers
- [x] 7-layer distillation pipeline
- [x] Knowledge graph expansion with Bayesian confidence
- [x] Persona decision hub: 3-layer radar + cross-validation + 14-dim timeline
- [x] Application layer: IntentRouter, predictive push, dispute arbitration, freshness alerts
- [x] Sync framework: 8-step pipeline + 12 agent sources
- [x] Agent Kit integration: 8 targets with static conformance separated from authorized, recent runtime receipts; missing proof cannot report `full_power`
- [x] KIA scheduler: topological parallel execution + auto-disable on failure
- [x] Incremental & deferred distillation

Planned:
- [ ] Web dashboard / local control center
- [ ] Obsidian plugin

---

## License

[MIT License](LICENSE)

**Mnemos** (/ˈnɛmɒs/) — from Greek mythology, the goddess of memory. Not just helping you remember, but helping AI assistants recall source-backed knowledge and act within auditable, configurable boundaries.
