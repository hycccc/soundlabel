# soundlabel

**An open-source framework for running an AI music label** — real-time listening rooms, a three-tier scoring stack, a multi-agent A&R loop, and a generation pipeline with pluggable backends. 🚧 *Milestone 0: architecture locked, components landing.*

This is the open framework edition of a platform I've run in production since 2026: a single-operator label where songs are generated, scored, reviewed in live listening rooms, and released — with AI agents doing A&R, production, and criticism. The framework keeps that architecture and strips everything proprietary: **no copyrighted reference data, no hardwired generation vendor** — bring your own backend.

## Architecture

```
                       ┌─────────────────────────────┐
                       │        soundlabel core       │
                       │  catalog · artists · batches │
                       └──────┬───────────┬──────────┘
        ┌─────────────────────┤           ├─────────────────────┐
   listening rooms       scoring stack        agent loop     pipeline
   (LiveKit rooms,     (rules → reward      (A&R → Producer  (orchestrated
   presence, synced     model → LLM judge,   → Critic, with   steps, retries,
   playback, live       human eval on top)   independent      batch manifests)
   scoring)                                  Critic scoring)
        │                     │                   │                │
        ▼                     ▼                   ▼                ▼
   livekit-server        [songscore]        [claude-oncall]   generation
                                            pattern            backend plugin
                                                               (mock included)
```

**Satellite components, already public:**

| Component | Role in the framework |
|---|---|
| [songscore](https://github.com/hycccc/songscore) | the scoring stack: DSP rules + reward-model hook + calibrated LLM judge |
| [musicgen-if-eval](https://github.com/hycccc/musicgen-if-eval) | the human-evaluation methodology sitting above all automated scoring |
| [audio-integrity-toolkit](https://github.com/hycccc/audio-integrity-toolkit) | the ingestion gate for any audio entering the catalog |
| [claude-oncall](https://github.com/hycccc/claude-oncall) | the agent-sidecar pattern the A&R loop builds on |

## Design principles

1. **Generation is a plugin.** The framework ships a mock backend (instant, free, synthesized audio) and a provider interface; adapters for commercial APIs are yours to write. No vendor coupling in core.
2. **Every track is scored before a human hears it.** Rules gate, models rank, judges annotate — humans decide. The Critic agent scores blind: it never sees the Producer's parameters, preventing knowledge contamination.
3. **Nothing spends money on a timer by default.** Generation credits and LLM tokens are only spent on explicit action or opt-in automation — a rule learned in production.
4. **Zero copyrighted data.** Demo artists, demo tracks, and demo audio are synthesized; the reference-song ingestion of the production system is intentionally out of scope.

## Roadmap

- [x] **M0** — architecture, principles, roadmap (this README)
- [ ] **M1** — core data model + scoring integration: catalog service with songscore as the acceptance/ranking layer
- [ ] **M2** — listening room: LiveKit room orchestration, synced playback, live scoring UI
- [ ] **M3** — generation backend interface + mock provider + one reference adapter shape
- [ ] **M4** — the agent loop: A&R brief → Producer run → blind Critic verdict → release/redo, on the claude-oncall sidecar pattern
- [ ] **M5** — single-operator deployment recipe (docker compose, one box, one human)

## Status

Architecture extracted from a running production system; code lands milestone by milestone as each layer is decoupled and sanitized. Watch the repo or open an issue if a milestone matters to you.

## License

MIT
