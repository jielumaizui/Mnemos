# Mnemos Domain Context

## Gaps, blindspots, and preferences

**Knowledge Coverage Gap**: A missing, stale, structurally incomplete, or weakly connected asset inside an explicit knowledge scope. It is resolved only by an independent coverage recheck that names the exact gap revision. Avoid: user ignorance, cognitive defect, interaction preference.

**User Cognitive Blindspot**: A scoped, evidence-backed hypothesis that a current user goal or decision may omit a frame, option, time horizon, or relevant context. Accepting a challenge means willingness to explore; it does not confirm the hypothesis. Avoid: knowledge coverage gap, stable personality trait, confirmed defect.

**Shadow Blindspot Hypothesis**: A non-durable assistant inference produced from behavioral signals for possible exploration. It may support a challenge prompt, but it cannot enter the active user model, confirm a defect, or inherit authority from a user's reaction. Avoid: treating classifier output or challenge acceptance as a user cognitive blindspot.

**Interaction Preference**: A scoped and revocable signal about how an interaction should be conducted, such as depth, format, or interruption style. It expires or is superseded when later evidence changes the context. Avoid: cognitive blindspot, global persona truth, knowledge gap.

**Asset Scope**: The vault, project, session, or user boundary in which an asset is meaningful, including its purpose and principal when applicable. Avoid: a topic string used as a global identity.

**Cognitive Authority Evidence**: In current legacy Mnemos user-model semantics, an exact role-local quote resolved through a typed `SourceAuthorityCatalog` as `system_policy`, `explicit_user`, or `project_contract`. Only this evidence can admit, confirm, dismiss, or invalidate a legacy user-model asset. Its successor form remains subject to Design-Stage Adjudication. Avoid: caller metadata, assistant summaries, detached text, and low-authority external or quoted content.

**Independent User-Model Store**: An append-only revision and head state machine owned separately for user cognitive blindspots and interaction preferences. Read paths use immutable SQLite access and never initialize missing state. Avoid: embedding either asset in persona JSON or sharing the knowledge-coverage-gap tables.

**Typed Resolution Evidence**: Independent proof that the exact asset no longer applies. A Wiki title, a successful projection, or a user's willingness to explore is not sufficient by itself. Avoid: display-name similarity and status-only updates.

**Knowledge Form**: The explicit structural form of a distilled page, such as decision, problem-solution, heuristic, anti-pattern, methodology, or insight. It is separate from the page's entity type and is consumed by knowledge-coverage analysis. Avoid: inferring form from a broad `类型` value.

## Cognitive successor

**Design-Stage Adjudication**, **Typed Standing**, and **Quality-Preserving Simplicity** are working vocabulary for the current design process; every successor term below remains non-governing candidate language. A definition never adopts a constitution clause, denominator, owner, Interface, Seam, implementation, quality verdict, or cutover rule. Adopting an adjudication method would not automatically activate its referenced product-value or quality-policy candidates.

**Design-Stage Adjudication**: A reversible, constraint-first process that first classifies a claim and its Typed Standing, then compares strongest-fair candidates using frozen hard constraints, supporting and refuting evidence, uncertainty, falsification and independent challenge. Existing governing contracts still constrain actual operations on the legacy system, but do not automatically decide successor semantics or architecture. Avoid: old-contract supremacy, newest-proposal supremacy, reviewer voting, scalar correctness scores, or preserving a clause solely because it was once accepted.

**Typed Standing**: The exact kind of decision a source or role is allowed to make. Evidence may support or refute facts; the legitimate product owner chooses informed values; scoped constraints determine admissibility; architecture tests validate mechanisms; migration evidence proves conservation; exact authorization permits an action. None upgrades another. Avoid: treating user preference as empirical proof, a reviewer identity as architecture proof, or a correct design as execution permission.

**Quality-Preserving Simplicity**: A non-compensatory implementation preference. Tier 1 requires exact zero gaps over the approved capability and cognitive denominators plus safety, data, recovery, failure and operational obligations. Tier 2 requires an exact material-metric denominator, scenario/cohort coverage, a typed baseline registry, and verifier-derived candidate-baseline effect states against frozen SLO, non-inferiority, equivalence and meaningful-improvement bounds. A slice may complete after Tier 1 zero-gap plus SLO/non-inferiority for every applicable approved metric; only final cutover requires nonempty full performance and experience denominators, a predeclared meaningful improvement in each domain, and no material regression. Only candidates equivalent on all material cognitive, safety, data, recovery, performance and experience outcomes reach Tier 3, where at least two implementations or independent saturation evidence are assessed through exact-denominator typed facts for `K_API`, `K_AUTHORITY`, `K_PATH`, `K_CHANGE`, `K_LIFECYCLE` and `K_ABSTRACTION`. The final accepted implementation must improve at least one predeclared material complexity fact with no material complexity regression against an approved outcome-equivalent implementation baseline, and all negative oracles and local code-quality guardrails must pass. Missing required assets block, Pareto incomparability yields `SIMPLICITY_UNRESOLVED`, and style decides only after all remaining material facts are equivalent. Avoid: omitted or permanently deferred behavior, weaker gates, skipped evidence or persistence, cherry-picked metrics, self-authored metric states, scalar elegance scores, a god Interface, pass-through Modules, boundary-free Seams, deleted tests/runbooks, or complexity exported into configuration, schema, data, generated assets, dependencies, tests or operations.

**Effective Capability**: An intended atomic, externally observable user or system behavior whose principal, scope, inputs, outputs, state changes, effects, failure semantics, or service obligations are materially distinct. Its identity is independent of whether the current Mnemos realization is working, partial, broken, unreachable, or unknown. Avoid: file count, command count, implementation class, a broken realization used to erase capability intent, or an accidental buggy effect treated as the intended feature.

**Capability Archaeology Record**: The evidence-linked reconstruction of one Mnemos capability from every relevant entrance, contract, code path, configuration facet, schema, state/effect sink, test, document, history record, and operator workflow. It records intended behavior and current realization health separately, then maps candidate successor owner, Interface, oracle, migration, and disposition. Avoid: treating the current happy path, a capability seed, or one test as the complete feature.

**Realization Health**: The observed state of a particular implementation of an Effective Capability: working, partial, broken, unreachable, unknown, or superseded, with exact defect and evidence references. It does not decide whether the underlying capability belongs in the parity denominator. Avoid: `broken == unnecessary` and `working == correctly specified`.

**Legacy Parity Denominator**: The exact approved set of effective Mnemos capability intents that the successor must preserve equivalently, preserve through an adapter, or replace with a stronger approved contract before cutover, including valid capabilities whose current realization is incomplete or buggy. Avoid: the current 39 capability seeds, a sampled regression suite, a count-only equality claim, or deleting an intended feature because its legacy implementation is unhealthy.

**Cognitive Adequacy Denominator**: The exact approved set of net-new cognitive obligations that distinguishes the successor from a bug-free Mnemos, including independent world evidence, bounded deliberation, epistemic control, correction, agency, and human-AI complementarity. Avoid: treating legacy parity as proof of cognitive adequacy.

**User Context Model**: A scoped, revisable account of the user's explicit goals, values, preferences, constraints, experience, and cognitive load, admitted only through user-model authority. Avoid: external world truth, a universal personality label, or assistant inference promoted into a user fact.

**Epistemic World Model**: A revisable set of evidence-backed claims, alternatives, disputes, source dependencies, and unknowns about the external world. Avoid: user preference, user intent, or unverified model knowledge represented as truth.

**Evidence Admission**: The governed decision that evidence may enter a task's truth-relevant evidence set based on provenance, relevance, independence, freshness, risk, and uncertainty. Avoid: personalized ranking, presentation order, source-count voting, or user agreement.

**Personalized Presentation**: A revocable adaptation of language, order, depth, timing, and interaction load that does not remove material evidence, counterevidence, alternatives, or unknowns. Avoid: changing truth status or shrinking the task evidence denominator to match a preference.

**Typed Cognitive Workspace**: The bounded, task-scoped state of an active cognitive round, including goals, evidence, alternatives, conflicts, unknowns, budgets, progress, and stopping disposition. Avoid: long-term memory, an untyped agent transcript, or a decision snapshot presented as the deliberation process itself.

**Epistemic Expansion**: The active search for independent evidence, material counterevidence, alternative explanations, cross-domain relations, and decision-relevant unknowns beyond the user's observed information horizon. Avoid: merely retrieving more persona-aligned items or equating several dependent sources with independent support.

**Epistemic Control**: The governed assessment that chooses whether a cognitive round may accept, revise, seek information, propose an experiment, abstain, or escalate. Avoid: free-form self-reflection, self-certified confidence, or always continuing until a fluent answer appears.

**Complementarity Gain**: Evidence that collaboration improves a task-relevant outcome or appropriate reliance beyond the relevant human-only and system-only baselines without increasing manipulation or unacceptable cognitive load. Avoid: engagement, acceptance rate, dependence, or user agreement used as a proxy for truth or benefit.
