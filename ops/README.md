# soundlabel ops — the label's oncall sidecar

**A production sidecar pattern for the [Claude Agent SDK](https://docs.anthropic.com/en/api/agent-sdk/overview)** — the ops agent that runs my music-label platform, packaged as soundlabel's operator layer. A single-operator label needs someone on call at 3am; this is that someone. One Express process gives a web app an embedded oncall agent with sessions, live streaming, git-snapshot revert, daily briefs, proactive monitoring, and opt-in self-reflection.

```
POST /chat                     agent turn (SSE) — persona + live context + Agent SDK query
GET  /sessions /sessions/:id   session list / transcript / per-session commits
POST /revert                   roll back to any turn's git snapshot (refs/oncall-snapshots/*)
GET  /daily-brief              cached morning report, one generation per (date, user)
POST /quick-ask                short-form commentary with response caching
GET  /proactive-queue          nudges from the periodic watcher
POST /files/upload             attachments via in-memory cache (see below)
```

## The patterns worth stealing

**Context aggregation with a freshness budget** (`context-aggregator.mjs`) — every turn injects a snapshot of live business state (catalog, roster, recent batches, trend queue). Each source has a timeout: a stale-but-fast brief beats making the agent wait 8s on a slow query.

**A system prompt that defends the runtime** (`prompts/system.example.md`) — the prompt bans the Bash patterns that zombify a sidecar (`while true`, unbounded `until`-polling, `tail -f`), and teaches the one-shot-check-then-report alternative. Written after production incidents, not before.

**Cost-conscious autonomy** (`auto-reflection.mjs`) — the agent reflects on the last 24h every 6 hours, *opt-in only*, with the cost math right in the comments (~7K tokens/day). Junk-insight suppression is explicit: "if there is nothing worth saying, output NONE — junk insights are worse than silence."

**Judge your agent's chattiness like a metric** (`proactive-watch.mjs`) — nudges are deduped by key and queued, never pushed; the queue endpoint lets the UI decide when the human is interruptible.

**Session-turn git snapshots** (`server.mjs`) — every chat turn snapshots the working tree to `refs/oncall-snapshots/<session>/<ts>` and auto-commits after the turn, so any agent edit is revertible from the UI. The agent itself is told to never commit or push.

**Attachment workaround for OAuth-scoped tokens** (`server.mjs`) — the Files API needs an API key the OAuth sidecar token doesn't have; uploads live in an in-memory TTL cache and expand to base64 content blocks at chat time. Ephemeral by design.

**Persona with a heat dial** (`heat-modifier.mjs`, `persona/example.md`) — response temperature from ICE (git-log factual) to SPICY (acerbic but never ad hominem), appended as a per-turn override. The example persona shows the craft: direct, specific, never a yes-man.

**Artist sub-personas from file-based memory** (`artist-persona.mjs`) — `@slug` mentions load that entity's 4-file memory (`sonic-profile / successes / failures / audience`) as a sub-frame; empty memory is admitted, never fabricated.

## Run it

```bash
npm install
ANTHROPIC_API_KEY=... ONCALL_CWD=/path/to/your/repo npm start
# then: curl localhost:9833/health
```

Configuration is all env: `ONCALL_PROMPTS_DIR` (system prompt + optional sandbox extension), `ONCALL_PERSONA` path, `SANDBOX_BACKEND_URL` for the context sources, `ONCALL_MODEL`, `ONCALL_THINKING_BUDGET`.

## Provenance

Extracted 1:1 from the sidecar running my production music platform; business context sources, prompts, and personas are replaced with sanitized examples (`prompts/system.example.md`, `persona/example.md`) that preserve the design patterns without the business internals.

## License

MIT
