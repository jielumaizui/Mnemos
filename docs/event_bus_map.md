# Mnemos Event Bus Map

_Generated from static AST analysis. Dynamic runtime-only events are annotated by the waiver file._

## Summary

- **Registered event types**: 32
- **Persistent event types**: 16
- **No-persist event types**: 16
- **Producer references found**: 52
- **Consumer references found**: 53
- **Unregistered observed events**: 23
- **Registered events with no producer/consumer**: 11

## Event Matrix

| Event | Registered | Persistent | NoPersist | Producers | Consumers | Notes |
|---|---|---|---|---|---|---|
| [blind_spot_detected](#blind_spot_detected) | yes |  | yes | 0 | 0 | ORPHANED |
| [capsule.due](#capsule-due) |  |  |  | 1 | 0 | unregistered |
| [capsule.overdue](#capsule-overdue) |  |  |  | 1 | 0 | unregistered |
| [cognition_episode_committed](#cognition_episode_committed) | yes | yes |  | 1 | 3 |  |
| [content_scored](#content_scored) | yes |  | yes | 0 | 0 | ORPHANED |
| [daily_credit_recovery](#daily_credit_recovery) |  |  |  | 1 | 0 | unregistered |
| [dispute_created](#dispute_created) | yes | yes |  | 0 | 0 | ORPHANED |
| [distill.request](#distill-request) | yes | yes |  | 0 | 0 | ORPHANED |
| [distill_complete](#distill_complete) | yes | yes |  | 2 | 3 |  |
| [distillation_progress](#distillation_progress) | yes |  | yes | 1 | 0 | no consumer |
| [dna.computed](#dna-computed) | yes |  | yes | 1 | 1 |  |
| [entity_discovered](#entity_discovered) | yes |  | yes | 0 | 0 | ORPHANED |
| [entropy.suggestions](#entropy-suggestions) | yes |  | yes | 2 | 2 |  |
| [feedback.prompt_due](#feedback-prompt_due) | yes |  | yes | 3 | 0 | no consumer |
| [feedback_loop](#feedback_loop) |  |  |  | 1 | 0 | unregistered |
| [git_commit_detected](#git_commit_detected) |  |  |  | 1 | 1 | unregistered |
| [guard_alert](#guard_alert) | yes |  | yes | 1 | 0 | no consumer |
| [immune.auto_fix](#immune-auto_fix) | yes |  | yes | 1 | 0 | no consumer |
| [immune.report](#immune-report) | yes | yes |  | 1 | 1 |  |
| [knowledge.deleted](#knowledge-deleted) |  |  |  | 1 | 1 | unregistered |
| [knowledge.ingested](#knowledge-ingested) | yes | yes |  | 1 | 3 |  |
| [knowledge.updated](#knowledge-updated) |  |  |  | 1 | 1 | unregistered |
| [knowledge_distilled](#knowledge_distilled) | yes | yes |  | 1 | 3 |  |
| [knowledge_needs_reinforcement](#knowledge_needs_reinforcement) | yes |  | yes | 2 | 1 |  |
| [knowledge_stale](#knowledge_stale) | yes | yes |  | 1 | 1 |  |
| [memory_synced](#memory_synced) | yes |  | yes | 0 | 0 | ORPHANED |
| [message.exchanged](#message-exchanged) |  |  |  | 1 | 1 | unregistered |
| [notes_synced](#notes_synced) |  |  |  | 1 | 1 | unregistered |
| [observation.updated](#observation-updated) | yes | yes |  | 1 | 2 |  |
| [page.created](#page-created) |  |  |  | 1 | 3 | unregistered |
| [page.modified](#page-modified) |  |  |  | 1 | 2 | unregistered |
| [page_accessed](#page_accessed) |  |  |  | 1 | 1 | unregistered |
| [page_created](#page_created) |  |  |  | 1 | 1 | unregistered |
| [periodic_cleanup](#periodic_cleanup) |  |  |  | 1 | 1 | unregistered |
| [periodic_persona_analysis](#periodic_persona_analysis) |  |  |  | 1 | 0 | unregistered |
| [periodic_stress_test](#periodic_stress_test) |  |  |  | 0 | 1 | unregistered |
| [persona.updated](#persona-updated) |  |  |  | 1 | 1 | unregistered |
| [polled](#polled) | yes | yes |  | 1 | 0 | no consumer |
| [profile_blindspot_detected](#profile_blindspot_detected) | yes |  | yes | 1 | 0 | no consumer |
| [profile_updated](#profile_updated) | yes |  | yes | 0 | 0 | ORPHANED |
| [reflection.completed](#reflection-completed) | yes | yes |  | 3 | 1 |  |
| [relation_conflicted](#relation_conflicted) | yes |  | yes | 0 | 0 | ORPHANED |
| [scheduler.daily](#scheduler-daily) | yes |  | yes | 1 | 2 |  |
| [scheduler.hourly](#scheduler-hourly) |  |  |  | 1 | 1 | unregistered |
| [session.end](#session-end) | yes | yes |  | 1 | 1 |  |
| [session.start](#session-start) | yes | yes |  | 1 | 2 |  |
| [session_completed](#session_completed) |  |  |  | 1 | 1 | unregistered |
| [signal.batch](#signal-batch) | yes | yes |  | 0 | 0 | ORPHANED |
| [skill_deviated](#skill_deviated) |  |  |  | 1 | 1 | unregistered |
| [skill_executed](#skill_executed) |  |  |  | 1 | 1 | unregistered |
| [system_alert](#system_alert) | yes | yes |  | 0 | 0 | ORPHANED |
| [task_completed](#task_completed) |  |  |  | 1 | 1 | unregistered |
| [wiki_page_accessed](#wiki_page_accessed) |  |  |  | 1 | 1 | unregistered |
| [wiki_page_updated](#wiki_page_updated) | yes | yes |  | 3 | 6 |  |
| [wiki_search_requested](#wiki_search_requested) | yes |  | yes | 0 | 0 | ORPHANED |

## blind_spot_detected {#blind_spot_detected}

**ORPHANED** - no producers or consumers detected.

## capsule.due {#capsule-due}

### Producers

- `core/kia/aion.py:391` `TimeCapsule::publish_due_events` (_publish_reminder_events())

### Consumers

_No consumers detected._

## capsule.overdue {#capsule-overdue}

### Producers

- `core/kia/aion.py:401` `TimeCapsule::publish_overdue_events` (_publish_reminder_events())

### Consumers

_No consumers detected._

## cognition_episode_committed {#cognition_episode_committed}

### Producers

- `dynamic-waiver` `core.cognitive.cognition_episode_dispatch.publish_cognition_episode_revision` (waiver)

### Consumers

- `core/cognitive/cognition_episode_dispatch.py:673` `CognitionEpisodeDispatchOwner::subscribe` (EventBus.subscribe())
- `core/cognitive/cognition_episode_dispatch.py:674` `CognitionEpisodeDispatchOwner::subscribe` (EventBus.subscribe())
- `core/cognitive/cognition_episode_dispatch.py:679` `CognitionEpisodeDispatchOwner::subscribe` (EventBus.subscribe())

## content_scored {#content_scored}

**ORPHANED** - no producers or consumers detected.

## daily_credit_recovery {#daily_credit_recovery}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

_No consumers detected._

## dispute_created {#dispute_created}

**ORPHANED** - no producers or consumers detected.

## distill.request {#distill-request}

**ORPHANED** - no producers or consumers detected.

## distill_complete {#distill_complete}

### Producers

- `core/hephaestus/distillation_engine.py:641` `DistillationEngine::_emit_distill_events` (EventBus.publish())
- `core/hephaestus/document_pipeline.py:1383` `DocumentDistillationPipeline::_emit_page_events` (publish_event())

### Consumers

- `core/cognitive_graph/updater.py:502` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())
- `core/kia/charon.py:298` `ConnectModule::handle_event` (PluggableModule.handle_event())
- `core/kia/cross_agent_linker.py:241` `CrossAgentLinker::handle_event` (PluggableModule.handle_event())

## distillation_progress {#distillation_progress}

### Producers

- `core/hephaestus_worker.py:1148` `HephaestusWorker::_emit_progress` (publish_event())

### Consumers

_No consumers detected._

## dna.computed {#dna-computed}

### Producers

- `core/kia/genos.py:412` `DNAEngine::compute_dna` (_emit_event())

### Consumers

- `mnemos_daemon.py:1095` `_register_kia_event_handlers` (EventBus.subscribe())

## entity_discovered {#entity_discovered}

**ORPHANED** - no producers or consumers detected.

## entropy.suggestions {#entropy-suggestions}

### Producers

- `core/kia/eris.py:209` `EntropyEngine::_on_knowledge_ingested` (_emit_event())
- `core/kia/eris.py:347` `EntropyEngine::scan` (_emit_event())

### Consumers

- `core/kia/hygieia.py:171` `KnowledgeImmuneSystem::handle_event` (PluggableModule.handle_event())
- `mnemos_daemon.py:1096` `_register_kia_event_handlers` (EventBus.subscribe())

## feedback.prompt_due {#feedback-prompt_due}

### Producers

- `daemon/event_handlers.py:106` `_publish_feedback_prompt` (EventBus.publish())
- `daemon/reflection_services.py:82` `run_feedback_prompt` (EventBus.publish())
- `integrations/apollon.py:972` `_run_feedback_prompt_on_session_end` (EventBus.publish())

### Consumers

_No consumers detected._

## feedback_loop {#feedback_loop}

### Producers

- `core/hephaestus/distillation_feedback.py:239` `DistillFeedbackLoop::_publish_feedback_event` (publish_event())

### Consumers

_No consumers detected._

## git_commit_detected {#git_commit_detected}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/persona/psyche.py:726` `SignalStore::handle_event` (PluggableModule.handle_event())

## guard_alert {#guard_alert}

### Producers

- `core/kia/aegis.py:1071` `InProcessGuard::_record_alert` (publish_event())

### Consumers

_No consumers detected._

## immune.auto_fix {#immune-auto_fix}

### Producers

- `core/kia/hygieia.py:902` `KnowledgeImmuneSystem::auto_fix` (_emit_event())

### Consumers

_No consumers detected._

## immune.report {#immune-report}

### Producers

- `core/kia/hygieia.py:861` `KnowledgeImmuneSystem::full_scan` (_emit_event())

### Consumers

- `mnemos_daemon.py:1094` `_register_kia_event_handlers` (EventBus.subscribe())

## knowledge.deleted {#knowledge-deleted}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/genos.py:285` `DNAEngine::handle_event` (PluggableModule.handle_event())

## knowledge.ingested {#knowledge-ingested}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/eris.py:188` `EntropyEngine::handle_event` (PluggableModule.handle_event())
- `core/kia/genos.py:278` `DNAEngine::handle_event` (PluggableModule.handle_event())
- `core/kia/hygieia.py:164` `KnowledgeImmuneSystem::handle_event` (PluggableModule.handle_event())

## knowledge.updated {#knowledge-updated}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/genos.py:278` `DNAEngine::handle_event` (PluggableModule.handle_event())

## knowledge_distilled {#knowledge_distilled}

### Producers

- `core/hephaestus/distillation_engine.py:1406` `_emit_knowledge_distilled` (publish_event())

### Consumers

- `core/cognitive_graph/updater.py:497` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())
- `core/kia/cross_agent_linker.py:241` `CrossAgentLinker::handle_event` (PluggableModule.handle_event())
- `daemon/wiki_projection_handlers.py:935` `register_wiki_projection_handlers` (EventBus.subscribe())

## knowledge_needs_reinforcement {#knowledge_needs_reinforcement}

### Producers

- `core/kia/stress_test.py:580` `StressTestEngine::_emit_stress_events` (_emit_event())
- `core/kia/stress_test.py:911` `StressTestEngine::_update_page_frontmatter` (_emit_event())

### Consumers

- `core/kia/stress_test.py:276` `StressTestEngine::handle_event` (PluggableModule.handle_event())

## knowledge_stale {#knowledge_stale}

### Producers

- `core/hephaestus/evolution_tracker.py:355` `TemporalEvolutionTracker::scan_all_pages` (publish_event())

### Consumers

- `daemon/entrypoint_support.py:514` `register_session_event_handlers` (EventBus.subscribe())

## memory_synced {#memory_synced}

**ORPHANED** - no producers or consumers detected.

## message.exchanged {#message-exchanged}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/chronos.py:672` `KnowledgeScheduler::trigger_event` (PluggableModule.handle_event())

## notes_synced {#notes_synced}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/persona/psyche.py:711` `SignalStore::handle_event` (PluggableModule.handle_event())

## observation.updated {#observation-updated}

### Producers

- `core/cognitive/observation_engine.py:767` `ObservationEngine::_export_projection` (publish_event())

### Consumers

- `core/cognitive_graph/updater.py:517` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())
- `daemon/entrypoint_support.py:513` `register_session_event_handlers` (EventBus.subscribe())

## page.created {#page-created}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/charon.py:298` `ConnectModule::handle_event` (PluggableModule.handle_event())
- `core/kia/chronos.py:666` `KnowledgeScheduler::trigger_event` (PluggableModule.handle_event())
- `core/kia/cross_agent_linker.py:241` `CrossAgentLinker::handle_event` (PluggableModule.handle_event())

## page.modified {#page-modified}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/charon.py:298` `ConnectModule::handle_event` (PluggableModule.handle_event())
- `core/kia/chronos.py:668` `KnowledgeScheduler::trigger_event` (PluggableModule.handle_event())

## page_accessed {#page_accessed}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/ixion.py:242` `CognitiveDecisionFlywheel::handle_event` (PluggableModule.handle_event())

## page_created {#page_created}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/stress_test.py:272` `StressTestEngine::handle_event` (PluggableModule.handle_event())

## periodic_cleanup {#periodic_cleanup}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/ixion.py:246` `CognitiveDecisionFlywheel::handle_event` (PluggableModule.handle_event())

## periodic_persona_analysis {#periodic_persona_analysis}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

_No consumers detected._

## periodic_stress_test {#periodic_stress_test}

### Producers

_No producers detected._

### Consumers

- `core/kia/stress_test.py:269` `StressTestEngine::handle_event` (PluggableModule.handle_event())

## persona.updated {#persona-updated}

### Producers

- `core/persona/delphi.py:544` `PersonaStore::save_persona` (publish_event())

### Consumers

- `core/cognitive_graph/updater.py:522` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())

## polled {#polled}

### Producers

- `core/sync_framework/sync_engine.py:973` `SyncEngine::sync_session` (publish_event())

### Consumers

_No consumers detected._

## profile_blindspot_detected {#profile_blindspot_detected}

### Producers

- `core/kia/stress_test.py:590` `StressTestEngine::_emit_stress_events` (_emit_event())

### Consumers

_No consumers detected._

## profile_updated {#profile_updated}

**ORPHANED** - no producers or consumers detected.

## reflection.completed {#reflection-completed}

### Producers

- `daemon/event_handlers.py:52` `_publish_reflection_completed` (EventBus.publish())
- `daemon/event_handlers.py:211` `_publish_observation_reflection` (EventBus.publish())
- `integrations/apollon.py:935` `_run_reflection_on_session_end` (EventBus.publish())

### Consumers

- `core/cognitive_graph/updater.py:512` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())

## relation_conflicted {#relation_conflicted}

**ORPHANED** - no producers or consumers detected.

## scheduler.daily {#scheduler-daily}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/eris.py:192` `EntropyEngine::handle_event` (PluggableModule.handle_event())
- `core/kia/hygieia.py:168` `KnowledgeImmuneSystem::handle_event` (PluggableModule.handle_event())

## scheduler.hourly {#scheduler-hourly}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/charon.py:303` `ConnectModule::handle_event` (PluggableModule.handle_event())

## session.end {#session-end}

### Producers

- `integrations/active_bridge.py:280` `main` (_publish())

### Consumers

- `daemon/entrypoint_support.py:511` `register_session_event_handlers` (EventBus.subscribe())

## session.start {#session-start}

### Producers

- `integrations/active_bridge.py:253` `main` (_publish())

### Consumers

- `core/kia/chronos.py:670` `KnowledgeScheduler::trigger_event` (PluggableModule.handle_event())
- `daemon/entrypoint_support.py:512` `register_session_event_handlers` (EventBus.subscribe())

## session_completed {#session_completed}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/persona/psyche.py:685` `SignalStore::handle_event` (PluggableModule.handle_event())

## signal.batch {#signal-batch}

**ORPHANED** - no producers or consumers detected.

## skill_deviated {#skill_deviated}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/ixion.py:240` `CognitiveDecisionFlywheel::handle_event` (PluggableModule.handle_event())

## skill_executed {#skill_executed}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/ixion.py:238` `CognitiveDecisionFlywheel::handle_event` (PluggableModule.handle_event())

## system_alert {#system_alert}

**ORPHANED** - no producers or consumers detected.

## task_completed {#task_completed}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/kia/ixion.py:236` `CognitiveDecisionFlywheel::handle_event` (PluggableModule.handle_event())

## wiki_page_accessed {#wiki_page_accessed}

### Producers

- `dynamic-waiver` `runtime_adapter` (waiver)

### Consumers

- `core/persona/psyche.py:705` `SignalStore::handle_event` (PluggableModule.handle_event())

## wiki_page_updated {#wiki_page_updated}

### Producers

- `core/wiki_projection_publisher.py:79` `publish_wiki_mutation` (publish_event())
- `core/wiki_projection_publisher.py:89` `publish_wiki_mutation` (EventBus.publish())
- `core/wiki_projection_publisher.py:159` `publish_unpublished_mutations` (publish_event())

### Consumers

- `core/cognitive_graph/updater.py:507` `CognitiveGraphUpdater::subscribe` (EventBus.subscribe())
- `daemon/wiki_projection_handlers.py:936` `register_wiki_projection_handlers` (EventBus.subscribe())
- `daemon/wiki_projection_handlers.py:939` `register_wiki_projection_handlers` (EventBus.subscribe())
- `daemon/wiki_projection_handlers.py:942` `register_wiki_projection_handlers` (EventBus.subscribe())
- `daemon/wiki_projection_handlers.py:945` `register_wiki_projection_handlers` (EventBus.subscribe())
- `daemon/wiki_projection_handlers.py:948` `register_wiki_projection_handlers` (EventBus.subscribe())

## wiki_search_requested {#wiki_search_requested}

**ORPHANED** - no producers or consumers detected.

## Anomalies

### Unregistered events observed in code

- `capsule.due`
- `capsule.overdue`
- `daily_credit_recovery`
- `feedback_loop`
- `git_commit_detected`
- `knowledge.deleted`
- `knowledge.updated`
- `message.exchanged`
- `notes_synced`
- `page.created`
- `page.modified`
- `page_accessed`
- `page_created`
- `periodic_cleanup`
- `periodic_persona_analysis`
- `periodic_stress_test`
- `persona.updated`
- `scheduler.hourly`
- `session_completed`
- `skill_deviated`
- `skill_executed`
- `task_completed`
- `wiki_page_accessed`

### Registered events with no producers or consumers

- `memory_synced`
- `content_scored`
- `entity_discovered`
- `relation_conflicted`
- `profile_updated`
- `blind_spot_detected`
- `dispute_created`
- `system_alert`
- `wiki_search_requested`
- `distill.request`
- `signal.batch`

### Registered events with no detected consumers

- `distillation_progress`
- `polled`
- `feedback.prompt_due`
- `guard_alert`
- `profile_blindspot_detected`
- `immune.auto_fix`

