# soundlabel

[![ci](https://github.com/hycccc/soundlabel/actions/workflows/ci.yml/badge.svg)](https://github.com/hycccc/soundlabel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-1fa88c.svg)](LICENSE)

**An open-source framework for running an AI music label** — a catalog, a three-tier scoring stack, a multi-agent A&R loop, and a generation pipeline with pluggable backends. Runnable today with zero API keys:

```bash
pip install "soundlabel @ git+https://github.com/hycccc/soundlabel.git"
soundlabel demo
```

```
── demo label ─────────────────────────────────────
signed: june-holiday (June Holiday)

[batch_0ef2b399] status: released
  gate: pass
  rank: 5.82/10 (lite)
  critic: accept — rank 5.82/10 via lite; meets bar for 'wistful' pop release

[batch_ccc5bbee] status: released
  critic: accept — meets bar for 'late-night' ballad release
── catalog ───────────────────────────────────────
trk_4ae3d7420d31   5.82  june-holiday  An Apology That Arrived Too Late
trk_ce480943e058   5.88  june-holiday  A Promise Kept Quietly
```

That's the full loop: the A&R agent reads the artist's profile and catalog history and writes a brief; the mock backend synthesizes audio; the gate and rank score it; the Critic judges it **blind** and either releases it into the catalog or sends it back.

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

## Design principles — enforced in code, not in prose

1. **Generation is a plugin.** Core ships a mock backend (instant, free, synthesized audio) and a provider interface ([`backends/base.py`](src/soundlabel/backends/base.py)); adapters for commercial APIs are yours to write. No vendor coupling in core.
2. **Every track is scored before a human hears it.** Rules gate, models rank, judges annotate — humans decide. `Catalog.add_track` *requires* a score and a verdict; an unscored track cannot exist in the catalog.
3. **The Critic is structurally blind.** Its input type is [`BlindBrief`](src/soundlabel/brief.py) — creative intent only, no field through which generation parameters can reach it. Swap in an LLM critic and the blindness survives, because it lives in the type system, not in a prompt. There is a test for this.
4. **Nothing spends money on a timer by default.** Any backend with a nonzero `cost_estimate` is refused unless the operator passes `--allow-paid` for that run — a rule learned in production. There is a test for this too.
5. **Zero copyrighted data.** Demo artists and demo audio are synthesized; the reference-song ingestion of the production system is intentionally out of scope.

Two smaller lessons baked in: track ids are **content-addressed** (`sha256` of the audio, so identity survives moves between environments — a location-derived id once cost me a cross-environment remap), and every batch writes a **step-by-step manifest** so failures are debuggable without log archaeology.

## Using it beyond the demo

```bash
soundlabel -w ./mylabel init
soundlabel -w ./mylabel roster add nova --name "Nova Lin" --profile "electronic, rnb"
soundlabel -w ./mylabel produce nova           # A&R writes the brief for you
soundlabel -w ./mylabel catalog
```

Install with `pip install "soundlabel[scoring] @ ..."` to pull in [songscore](https://github.com/hycccc/songscore); the scoring stack detects it and upgrades from the built-in lite ranker to the calibrated multi-dimension scorer automatically.

Writing a backend is one class:

```python
from soundlabel.backends import register
from soundlabel.backends.base import GenerationBackend, GenerationResult

class MyBackend(GenerationBackend):
    name = "mine"
    def cost_estimate(self, brief):
        return 0.5                       # nonzero → pipeline requires --allow-paid
    def generate(self, brief, out_dir):
        ...                              # call your API, download the audio
        return GenerationResult(audio_path=path, backend=self.name, params={...}, cost=0.5)

register("mine", MyBackend)
```

## LLM agents (M4b — opt-in)

`pip install "soundlabel[llm]"`, set `ANTHROPIC_API_KEY`, and add `--llm` to `produce`. The heuristic A&R and Critic are swapped for Claude-backed agents with **identical interfaces** — the Critic still receives a `BlindBrief`, so blindness survives the intelligence upgrade. Nothing selects these agents by default; token spend is opt-in per run, same philosophy as `--allow-paid`.

What a blind LLM Critic verdict looks like on a real run (the "3AM laundromat" theme is from the LLM A&R's own brief; the Critic saw only intent + measurements):

> *redo — Intent-fit failure is concentrated in one measurable, fixable dimension: spectral balance. A 6629 Hz centroid is extremely bright for a brief that explicitly asks for "hushed... quietly aching but warm" … Dynamics are the keeper: crest 19.6 dB scoring a full 1.000 means nothing has been crushed — that is the hardest thing to recover once lost. Do not touch the compression on the next pass. … Two corrective mix moves — low-mid restoration plus ambience widening — plausibly move this into release territory without rewriting a note. That is a redo, not a kill.*

## Deploy (M5)

```bash
docker compose run label demo
docker compose run label roster add ivy --profile "folk, ballad"
docker compose run label produce ivy
docker compose run label catalog
```

One box, one volume (`label-data` holds the catalog and batches), one human. `ANTHROPIC_API_KEY` in `.env` enables `--llm`.

## Roadmap

- [x] **M0** — architecture, principles, roadmap
- [x] **M1** — core data model + scoring integration: SQLite catalog, gate → rank stack, songscore auto-upgrade
- [x] **M3** — generation backend interface + mock provider + registry (`--allow-paid` cost guard)
- [x] **M4a** — the agent loop, heuristic edition: A&R brief (anti-rut, respects rejects) → generation → blind Critic verdict → release/redo/kill, with per-batch manifests
- [x] **M4b** — LLM-backed A&R and Critic agents: same interfaces, structurally blind, opt-in cost (`--llm`)
- [x] **M5** — single-operator deployment recipe (Dockerfile + docker compose, one box, one human)
- [ ] **M2** — listening room: LiveKit room orchestration, synced playback, live scoring UI

## Status

M1/M3/M4/M5 shipped and CI-tested; the loop runs end to end with zero API keys, and upgrades to LLM agents with one flag. Architecture extracted from a production system I've run since 2026. M2 (the listening room) is the last milestone — open an issue if it matters to you.

## License

MIT
