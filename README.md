# soundlabel

[![ci](https://github.com/hycccc/soundlabel/actions/workflows/ci.yml/badge.svg)](https://github.com/hycccc/soundlabel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-1fa88c.svg)](LICENSE)

**An open-source framework for running an AI music label** — a catalog, a three-tier scoring stack, a multi-agent A&R loop, an oncall ops sidecar, and a generation pipeline with pluggable backends. Runnable today with zero API keys:

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
   livekit-server       scoring/ (built    ops/ (oncall      generation
                        into core)          sidecar, Node)    backend plugin
                                                              (mock included)
```

**In this repository** — the scoring stack ([`src/soundlabel/scoring/`](src/soundlabel/scoring)) and the ops sidecar ([`ops/`](ops)) used to live as separate repos (songscore, claude-oncall); they are merged here because they are parts of one system, not products of their own.

**Satellite components, standalone by design:**

| Component | Role in the framework |
|---|---|
| [musicgen-if-eval](https://github.com/hycccc/musicgen-if-eval) | the human-evaluation methodology sitting above all automated scoring |
| [audio-integrity-toolkit](https://github.com/hycccc/audio-integrity-toolkit) | the ingestion gate for any audio entering the catalog |

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

The full scoring stack is built in — `soundlabel score track.wav --lyrics lyrics.txt` runs the acceptance gate plus the multi-dimension scorer on any file, catalog or not, and `--judge` adds the anchored LLM aesthetic dimension (regression-tested against score-dispersion collapse in [`regression/`](regression)).

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

## Listening room (M2 — opt-in)

`pip install "soundlabel[rooms]"`, point `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` at any LiveKit server (self-hosted or cloud — bring your own, same as generation backends), and:

```bash
soundlabel room open release-night
soundlabel room token release-night yuchen --host      # host: publishes audio, drives playback
soundlabel room token release-night listener-1         # listener: hears + scores
```

Serve [`room/listen.html`](room/listen.html) (the reference client) and hand out tokens. The host plays a candidate file; it streams to every listener from the same playhead; listeners score 1-10 over the data channel. **The role split lives in the token, not in client-side goodwill** — listener tokens cannot publish audio, and there is a test for that. The sync/scoring wire protocol is four small JSON messages, documented in [`rooms.py`](src/soundlabel/rooms.py).

**The room feeds back (M8):** the session is part of the loop, not a demo. `soundlabel room queue` picks what to audition from the catalog (released tracks nobody has heard first); scores travel with the track id; the host clicks **export session JSON** and `soundlabel room ingest session.json` writes them into the catalog:

```
$ soundlabel room queue
trk_d51ebd34fc46   5.84  [unheard]      june-holiday  Calling Home From Somewhere New
trk_4ae3d7420d31   6.09  [heard 7.5×2]  june-holiday  An Apology That Arrived Too Late

$ soundlabel room ingest session-release-night.json
ingested 2 score(s) from room 'release-night'
  trk_d51ebd34fc46  room 7.5×2  Calling Home From Somewhere New
```

Human reception then shows up everywhere the machine score does: in `soundlabel catalog`, in the A&R agent's history, and in the state snapshot the ops sidecar injects into its system prompt. A listener re-scoring the same track overwrites their previous score — changing your mind is signal, padding the count is not. Queue and ingest are fully offline (no LiveKit, no extras); scores for unknown tracks are skipped and reported, never silently dropped.

**And the reception steers production (M9).** Room scores roll up from tracks to styles, and the A&R agent's rule set grows a third rule that outranks the other two: *follow the room*. A style the room is cold on (avg < 6 over 2+ scores) stops getting briefed; a style the room loves (avg ≥ 8 over 2+ scores) wins the next brief — even the dominant style anti-rut would have dropped, because measured demand beats presumed fatigue. One listener's mood is not a trend: both cuts require 2+ scores. The LLM A&R gets the same instruction in its prompt; the heuristic one has it in code, with tests for all three interactions.

## Ops sidecar (M6 — opt-in)

A single-operator label needs someone on call. [`ops/`](ops) is that someone: a Node sidecar for the [Claude Agent SDK](https://docs.anthropic.com/en/api/agent-sdk/overview), extracted from the ops agent that has run my production label since 2026 — oncall chat with git-snapshot revert, context aggregation with freshness budgets, daily briefs, a proactive watcher whose chattiness is treated as a metric, and opt-in self-reflection with the cost math in the comments.

```bash
cd ops && npm ci && npm start        # boots without an API key; /health tells you what's missing
```

It is deliberately framework-agnostic — point it at any working directory and give it a persona ([`ops/persona/`](ops/persona)). The patterns are documented in [`ops/README.md`](ops/README.md).

**Wired to the label (M7):** set `SOUNDLABEL_WORKSPACE` (docker compose does) and the sidecar reads the workspace directly — every chat turn's system prompt carries the roster, catalog stats, and recent batches, and `POST /label/review {"batchId": ...}` reviews a batch from its manifest and writes `batches/<id>/ops-review.json`, which `soundlabel batches` displays:

```
$ soundlabel batches
batch_c781e7af  released  june-holiday  mock
    ops[heuristic]: released — rank 6.09/10 (full), critic accepted
      - brief: an apology that arrived too late (pop)
      → queue for a listening-room session before promo
```

The two processes share no database — Python exports `state.json`, Node writes reviews back, and the files are the API. Reviews are deterministic-heuristic by default; `{"llm": true}` upgrades to a model-written review with the same shape (and falls back to the heuristic if the call fails). Same free-by-default/opt-in-spend split as `--llm` and `--allow-paid`.

Reviews also read the room: for a released track with 2+ room scores, cold reception (avg < 6) flips the review to *"pull it from the promo queue — the room outvoted the critic"*, and loved reception (avg ≥ 8) to *"fast-track promo"*. The room heard it; the critic only measured it.

## Deploy (M5)

```bash
docker compose run label demo
docker compose run label roster add ivy --profile "folk, ballad"
docker compose run label produce ivy
docker compose run label catalog
```

One box, one volume (`label-data` holds the catalog and batches), one human. `ANTHROPIC_API_KEY` in `.env` enables `--llm`. `docker compose up ops` starts the oncall sidecar alongside.

## Roadmap

- [x] **M0** — architecture, principles, roadmap
- [x] **M1** — core data model + scoring integration: SQLite catalog, gate → rank stack
- [x] **M3** — generation backend interface + mock provider + registry (`--allow-paid` cost guard)
- [x] **M4a** — the agent loop, heuristic edition: A&R brief (anti-rut, respects rejects) → generation → blind Critic verdict → release/redo/kill, with per-batch manifests
- [x] **M4b** — LLM-backed A&R and Critic agents: same interfaces, structurally blind, opt-in cost (`--llm`)
- [x] **M5** — single-operator deployment recipe (Dockerfile + docker compose, one box, one human)
- [x] **M2** — listening room: LiveKit room orchestration, role-split tokens, synced playback + live scoring reference client
- [x] **M6** — consolidation: the scoring stack (formerly [songscore](https://github.com/hycccc/songscore)) built into core as `soundlabel.scoring`; the oncall sidecar (formerly [claude-oncall](https://github.com/hycccc/claude-oncall)) merged as `ops/`
- [x] **M7** — label↔ops wiring: the sidecar reads the workspace (`state.json` in every system prompt, `GET /label/state`) and writes batch reviews back (`POST /label/review` → `ops-review.json` → `soundlabel batches`); file contract, no shared database
- [x] **M8** — the room feeds back: `room queue` picks unheard released tracks from the catalog, scores carry track ids, the host exports the session, `room ingest` writes human reception into the catalog — visible to `catalog`, the A&R agent, and the ops sidecar
- [x] **M9** — the reception steers production: room scores roll up to style level and the A&R agents follow the room — cold styles stop getting briefed, loved styles win the next brief over anti-rut; 2+ scores required, one listener's mood is not a trend

## Status

**All milestones shipped.** The loop runs end to end with zero API keys, upgrades to LLM agents with one flag, streams to a listening room with one more, and deploys with docker compose. Architecture extracted from a production system I've run since 2026. Issues and adapters welcome.

## License

MIT
