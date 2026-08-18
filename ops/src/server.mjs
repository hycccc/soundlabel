import express from 'express'
import fs from 'node:fs'
import path from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { randomUUID } from 'node:crypto'
import multer from 'multer'
import { query } from '@anthropic-ai/claude-agent-sdk'
import { buildContextBlock, loadPersona } from './context-aggregator.mjs'
import { generateBrief } from './daily-brief.mjs'
import { quickAsk } from './quick-ask.mjs'
import { startProactiveWatch, getQueue, clearQueueItem } from './proactive-watch.mjs'
import { startAutoReflection } from './auto-reflection.mjs'
import { detectArtistMention, buildArtistPersonaBlock } from './artist-persona.mjs'
import { heatModifier } from './heat-modifier.mjs'

const execFileP = promisify(execFile)

// In-memory attachment cache. Files API requires an API key; the OAuth token
// the sidecar runs with does not have that scope (returns 404). Workaround:
// hold the upload buffer in memory, hand the client a synthetic file_id, and
// when the client references it in /chat we expand to a base64 ContentBlock.
// 1h TTL is more than enough for a chat turn; we never persist to disk so a
// sidecar restart wipes pending attachments (acceptable — they were ephemeral
// chat context anyway).
const attachmentStore = new Map()  // id -> { buffer, mime, filename, kind, createdAt, userId }
const ATTACHMENT_TTL_MS = 60 * 60 * 1000
const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024  // base64 grows ~33% → ~13MB on the wire
function purgeAttachments() {
  const now = Date.now()
  for (const [id, rec] of attachmentStore) {
    if (now - rec.createdAt > ATTACHMENT_TTL_MS) attachmentStore.delete(id)
  }
}
function attachmentLookup(id, userId) {
  const rec = attachmentStore.get(id)
  if (!rec) return null
  if (rec.userId !== userId) return null  // ownership check
  return rec
}
setInterval(purgeAttachments, 5 * 60 * 1000).unref()

const PORT = Number(process.env.PORT ?? 3310)
const BIND = process.env.BIND ?? '127.0.0.1'
const DEFAULT_CWD = process.env.ONCALL_CWD ?? process.cwd()
const DEFAULT_MODEL = process.env.ONCALL_MODEL ?? 'claude-sonnet-4-6'
// adaptive lets Claude decide when to think; medium-ish budget for oncall
const DEFAULT_THINKING_BUDGET = Number(process.env.ONCALL_THINKING_BUDGET ?? 4000)

const PROMPTS_DIR = process.env.ONCALL_PROMPTS_DIR ?? path.join(process.cwd(), 'prompts')
function loadPrompt(name, fallback = '') {
  try { return fs.readFileSync(path.join(PROMPTS_DIR, name), 'utf8').trim() } catch { return fallback }
}
const ONCALL_SYSTEM_PROMPT = loadPrompt('system.md',
  'You are the oncall assistant for this project. Answer with evidence, stay concise.')

// Sandbox-only system prompt extension. Appended on top of the base prompt
// when the request comes from the sandbox host (Caddy injects
// X-Oncall-Mode: sandbox). Anything here is experimental, can break, and
// must NOT be promoted to base until validated. DB calls in this section
// MUST point at the sandbox postgres container, never prod.
const SANDBOX_EXTRA_PROMPT = loadPrompt('sandbox-extra.md')

const HOME = process.env.HOME || '/root'
const SESSIONS_DIR = path.join(
  HOME,
  '.claude/projects',
  '-' + DEFAULT_CWD.replace(/^\//, '').replace(/\//g, '-'),
)

// Persistent map: sessionId -> { userId, firstMessage, createdAt }.
// SDK session jsonl files don't carry our userId, so we keep ownership out-of-band.
const OWNERS_FILE = process.env.ONCALL_OWNERS_FILE ?? path.join(HOME, '.claude-oncall/session-owners.json')
const LEGACY_DEFAULT_USER = process.env.ONCALL_LEGACY_OWNER ?? 'yuchen'
const owners = new Map()
{
  let loaded = false
  try {
    const raw = fs.readFileSync(OWNERS_FILE, 'utf8')
    for (const [k, v] of Object.entries(JSON.parse(raw))) owners.set(k, v)
    loaded = true
  } catch {
    fs.mkdirSync(path.dirname(OWNERS_FILE), { recursive: true })
  }
  // First-run migration: any existing SDK session on disk without an owner
  // is retroactively assigned to LEGACY_DEFAULT_USER so we don't orphan
  // the history from before multi-user support.
  if (!loaded) {
    try {
      for (const f of fs.readdirSync(SESSIONS_DIR)) {
        if (!f.endsWith('.jsonl')) continue
        const id = f.slice(0, -6)
        if (owners.has(id)) continue
        const stat = fs.statSync(path.join(SESSIONS_DIR, f))
        owners.set(id, { userId: LEGACY_DEFAULT_USER, firstMessage: '', createdAt: stat.mtimeMs })
      }
    } catch { /* SESSIONS_DIR may not exist yet */ }
    if (owners.size) persistOwners()
  }
}
function persistOwners() {
  try {
    fs.writeFileSync(OWNERS_FILE, JSON.stringify(Object.fromEntries(owners), null, 2))
  } catch (e) { console.error('[oncall] persistOwners:', e?.message || e) }
}
function normUser(u) {
  return typeof u === 'string' && u.trim() ? u.trim() : null
}

const app = express()
app.use(express.json({ limit: '1mb' }))

// ── File uploads ───────────────────────────────────────────────────────────
// Multipart upload → Anthropic Files API. We hold the file in memory only
// (multer.memoryStorage) since it goes straight to Anthropic's CDN; nothing
// is persisted on disk. Cap at 30 MB to keep the round-trip snappy and stay
// well under the org's 100 GB Files quota even at high request volume.
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 30 * 1024 * 1024 },
})

// Map an uploaded MIME type to the Anthropic content-block flavour we'll
// generate at /chat time:
//  - `image`    → ImageBlockParam (image/jpeg, png, gif, webp)
//  - `document` → DocumentBlockParam (application/pdf ONLY — base64 source
//                  rejects every other media_type with a 400)
//  - `text`     → expand to a text block at /chat time, content =
//                  buffer.toString('utf8') wrapped in a fenced code block.
//                  Use this for txt/md/json/csv/log/code etc.
// Unknown types fall back to text — at worst the model sees gibberish.
function classifyMime(mime) {
  if (!mime) return 'text'
  if (mime.startsWith('image/')) return 'image'
  if (mime === 'application/pdf') return 'document'
  return 'text'
}

// busboy (multer's parser) decodes multipart filenames as latin-1 by default.
// Modern browsers send UTF-8 bytes, so a non-ASCII filename
// arrives as the byte sequence E6 96 B0 ... interpreted under latin-1, giving
// the mojibake "æ°å»ºèºäºº.xlsx". Re-encode to bytes and decode as UTF-8 to
// recover the real name. Safe to always apply: ASCII filenames are byte-
// identical between latin-1 and UTF-8.
function fixFilename(name) {
  if (!name) return 'upload'
  try { return Buffer.from(name, 'latin1').toString('utf8') } catch { return name }
}

app.post('/files/upload', upload.single('file'), async (req, res) => {
  const userId = normUser(req.body?.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (!req.file) { res.status(400).json({ error: 'file field required' }); return }
  if (req.file.size > ATTACHMENT_MAX_BYTES) {
    res.status(413).json({ error: 'file_too_large', detail: `max ${ATTACHMENT_MAX_BYTES / 1024 / 1024} MB` })
    return
  }
  const id = `att_${randomUUID()}`
  const mime = req.file.mimetype || 'application/octet-stream'
  const filename = fixFilename(req.file.originalname)
  attachmentStore.set(id, {
    buffer: req.file.buffer,
    mime,
    filename,
    kind: classifyMime(mime),
    userId,
    createdAt: Date.now(),
  })
  res.json({
    file_id: id,
    filename,
    size_bytes: req.file.size,
    mime_type: mime,
    kind: classifyMime(mime),
  })
})

// Single-user sidecar. Tracks in-flight queries so we can interrupt them.
const active = new Map()

// Buffered live runs: sessionId → { events[], subs: Set<res>, done: bool }
// Events are kept after disconnect so clients can reconnect mid-run.
const liveRuns = new Map()

app.get('/health', (_req, res) => {
  res.json({ ok: true, cwd: DEFAULT_CWD, model: DEFAULT_MODEL, active: active.size, sessionsDir: SESSIONS_DIR })
})

app.get('/sessions', async (req, res) => {
  const userId = normUser(req.query.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  try {
    const files = await fs.promises.readdir(SESSIONS_DIR).catch(() => [])
    const sessions = []
    for (const f of files) {
      if (!f.endsWith('.jsonl')) continue
      const id = f.slice(0, -6)
      const rec = owners.get(id)
      if (!rec || rec.userId !== userId) continue
      const full = path.join(SESSIONS_DIR, f)
      const stat = await fs.promises.stat(full).catch(() => null)
      if (!stat || stat.size < 200) continue
      const fallbackTitle = rec.firstMessage || (await readFirstUserText(full))
      if (!fallbackTitle) continue
      sessions.push({
        id,
        // `label` is user-editable; `title` is the auto-derived first message.
        // Frontend renders label || title.
        title: fallbackTitle,
        label: rec.label || null,
        topic: rec.topic || null,
        updatedAt: stat.mtimeMs,
        size: stat.size,
      })
    }
    sessions.sort((a, b) => b.updatedAt - a.updatedAt)
    res.json({ sessions })
  } catch (e) {
    res.status(500).json({ error: e?.message || String(e) })
  }
})

// PATCH /sessions/:id  body: { label?, topic? }
// Rename a thread or tag it with a topic (e.g. a short topic tag). Pure
// metadata — doesn't touch the underlying jsonl, just owners map.
app.patch('/sessions/:id', async (req, res) => {
  const { id } = req.params
  const userId = normUser(req.body?.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (!/^[a-f0-9-]{36}$/.test(id)) { res.status(400).json({ error: 'bad id' }); return }
  const rec = owners.get(id)
  if (!rec || rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  const label = typeof req.body?.label === 'string' ? req.body.label.trim().slice(0, 80) : undefined
  const topic = typeof req.body?.topic === 'string' ? req.body.topic.trim().slice(0, 40) : undefined
  if (label !== undefined) rec.label = label || null
  if (topic !== undefined) rec.topic = topic || null
  owners.set(id, rec)
  persistOwners()
  res.json({ id, label: rec.label, topic: rec.topic })
})

app.get('/sessions/:id/messages', async (req, res) => {
  const { id } = req.params
  const userId = normUser(req.query.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (!/^[a-f0-9-]{36}$/.test(id)) { res.status(400).json({ error: 'bad id' }); return }
  const rec = owners.get(id)
  if (!rec || rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  const file = path.join(SESSIONS_DIR, `${id}.jsonl`)
  try {
    const text = await fs.promises.readFile(file, 'utf8')
    const turns = replayTurns(text.split('\n'))
    res.json({ turns })
  } catch (e) {
    res.status(404).json({ error: e?.message || String(e) })
  }
})

app.delete('/sessions/:id', async (req, res) => {
  const { id } = req.params
  const userId = normUser(req.query.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (!/^[a-f0-9-]{36}$/.test(id)) { res.status(400).json({ error: 'bad id' }); return }
  const rec = owners.get(id)
  if (!rec || rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  try {
    await fs.promises.unlink(path.join(SESSIONS_DIR, `${id}.jsonl`))
    owners.delete(id); persistOwners()
    res.json({ ok: true })
  } catch (e) {
    res.status(404).json({ error: e?.message || String(e) })
  }
})

app.get('/sessions/:id/commits', async (req, res) => {
  const { id } = req.params
  if (!/^[a-f0-9-]{36}$/.test(id)) { res.status(400).json({ error: 'bad id' }); return }
  const userId = normUser(req.query.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  const rec = owners.get(id)
  if (rec && rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  try {
    const { stdout } = await execFileP('git', [
      'log', '--format=%h %s', `--grep=session: ${id}`, '-30',
    ], { cwd: DEFAULT_CWD })
    const commits = stdout.trim().split('\n').filter(Boolean).map(line => {
      const sp = line.indexOf(' ')
      return { sha: line.slice(0, sp), subject: line.slice(sp + 1) }
    })
    res.json({ commits })
  } catch (e) {
    res.status(500).json({ error: e?.message || String(e) })
  }
})

app.get('/snapshots', async (_req, res) => {
  try {
    const { stdout } = await execFileP('git', [
      'for-each-ref', '--sort=-committerdate',
      '--format=%(refname:short) %(committerdate:iso) %(subject)',
      'refs/oncall-snapshots/',
    ], { cwd: DEFAULT_CWD })
    res.json({ snapshots: stdout.trim().split('\n').filter(Boolean) })
  } catch (e) {
    res.status(500).json({ error: e?.message || String(e) })
  }
})

// GET /daily-brief — small/Qian's morning report. First request of the day
// hits the Anthropic API; subsequent requests on the same date read from
// /srv/oncall-memory/sandbox/daily-briefs/YYYY-MM-DD.md. ?force=1 to
// regenerate. Sandbox-only (prod oncall doesn't have the agent persona).
app.get('/daily-brief', async (req, res) => {
  const isSandbox = req.headers['x-oncall-mode'] === 'sandbox'
  if (!isSandbox) {
    return res.status(404).json({ error: 'daily-brief is sandbox-only' })
  }
  const force = req.query.force === '1'
  const heat = heatModifier(req.headers['x-xiaoqian-heat'])
  try {
    const out = await generateBrief({ force, heatSuffix: heat.suffix, heatBand: heat.band })
    res.json(out)
  } catch (e) {
    res.status(500).json({ error: e?.message || String(e) })
  }
})

// POST /quick-ask  body: { query, instruction?, includeContext? }
// Single-shot the agent commentary for inline UI tips (Generator prompt review,
// Library song commentary). Sandbox-only.
app.post('/quick-ask', async (req, res) => {
  if (req.headers['x-oncall-mode'] !== 'sandbox') {
    return res.status(404).json({ error: 'quick-ask is sandbox-only' })
  }
  const { query, instruction, includeContext } = req.body ?? {}
  if (!query || typeof query !== 'string') {
    return res.status(400).json({ error: 'query required' })
  }
  const heat = heatModifier(req.headers['x-xiaoqian-heat'])
  try {
    const out = await quickAsk({
      query: query.slice(0, 3000),  // cap input length
      instruction: typeof instruction === 'string' ? instruction.slice(0, 800) : undefined,
      includeContext: includeContext !== false,
      heatSuffix: heat.suffix,
      heatBand: heat.band,
    })
    res.json(out)
  } catch (e) {
    res.status(500).json({ error: e?.message || String(e) })
  }
})

app.post('/chat', async (req, res) => {
  // Caddy on the sandbox host injects X-Oncall-Mode: sandbox; prod the production host
  // does not. Used to gate experimental capabilities (multi-agent orchestration,
  // persistent memory, sandbox-only DB pointers) so prod oncall stays stable.
  const oncallMode = req.headers['x-oncall-mode'] === 'sandbox' ? 'sandbox' : 'production'

  // Sandbox: layer the system prompt as
  //   base (Claude Code preset)            ← injected by SDK
  //   + ONCALL_SYSTEM_PROMPT (operational rules)
  //   + the agent persona (voice + dispatch + memory rules)
  //   + Live context snapshot (catalog / trends / momentum, refreshed ≤60s)
  //   + SANDBOX_EXTRA_PROMPT (sandbox infra pointers)
  // Persona + context are sandbox-only — prod oncall stays neutral Claude.
  let effectiveSystemPrompt = ONCALL_SYSTEM_PROMPT
  let detectedArtist = null
  if (oncallMode === 'sandbox') {
    const persona = loadPersona()
    let context = ''
    try { context = await buildContextBlock() } catch (e) {
      console.warn(`[oncall] context aggregator failed: ${e.message}`)
    }
    // Detect @artist-slug in the current user message AND any
    // previously-mentioned slug carried over via topic field. We don't
    // re-walk past turns — letting the user "stay in" an artist context
    // is done by setting the session topic via PATCH /sessions/:id.
    const userMessage = (typeof req.body?.message === 'string' ? req.body.message : '') || ''
    detectedArtist = detectArtistMention(userMessage)
    if (!detectedArtist && sessionId) {
      const rec = owners.get(sessionId)
      if (rec?.topic && /^[a-z][a-z0-9-]+$/.test(rec.topic)) {
        // Session topic looks like an artist slug — try.
        detectedArtist = detectArtistMention('@' + rec.topic)
      }
    }
    const artistBlock = detectedArtist ? buildArtistPersonaBlock(detectedArtist) : ''
    const { heat, band, suffix: heatSuffix } = heatModifier(req.headers['x-xiaoqian-heat'])
    const sections = [ONCALL_SYSTEM_PROMPT]
    if (persona) sections.push(persona)
    if (artistBlock) sections.push(artistBlock)
    if (context) sections.push(`# Current snapshot (refreshed every minute)\n\n${context}`)
    if (heatSuffix) sections.push(heatSuffix)
    sections.push(SANDBOX_EXTRA_PROMPT)
    effectiveSystemPrompt = sections.join('\n\n---\n\n')
    console.log(`[oncall] /chat mode=${oncallMode} artist=${detectedArtist || '-'} heat=${heat}/${band} systemPromptChars=${effectiveSystemPrompt.length}`)
  } else {
    console.log(`[oncall] /chat mode=${oncallMode} systemPromptChars=${effectiveSystemPrompt.length}`)
  }
  const { message, sessionId, cwd, model, thinkingBudget, userId: rawUserId, attachments } = req.body ?? {}
  const userId = normUser(rawUserId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  // Allow empty message when attachments are present (user can send "just an image").
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0
  if ((typeof message !== 'string' || !message.trim()) && !hasAttachments) {
    res.status(400).json({ error: 'message or attachments required' })
    return
  }
  // Resuming someone else's session is forbidden.
  if (sessionId) {
    const rec = owners.get(sessionId)
    if (rec && rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  }

  // Before we run Claude, snapshot the working tree so we always have a
  // rollback point. Uses `git stash create` (does not touch working tree)
  // + `git update-ref` under a dedicated namespace.
  const snapRef = await snapshotRepo(cwd || DEFAULT_CWD, sessionId, message).catch(() => null)

  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache, no-transform')
  res.setHeader('Connection', 'keep-alive')
  res.setHeader('X-Accel-Buffering', 'no')
  res.flushHeaders?.()

  // Live run buffer — persists after client disconnect so they can reconnect.
  const run = { events: [], subs: new Set([res]), done: false }
  const send = (obj) => {
    run.events.push(obj)
    const data = `data: ${JSON.stringify(obj)}\n\n`
    for (const sub of run.subs) {
      try { sub.write(data) } catch { run.subs.delete(sub) }
    }
  }
  const heartbeat = setInterval(() => {
    for (const sub of run.subs) { try { sub.write(': hb\n\n') } catch { run.subs.delete(sub) } }
  }, 15000)

  if (snapRef) send({ type: 'snapshot', ref: snapRef })

  const budget = Number.isFinite(Number(thinkingBudget)) ? Number(thinkingBudget) : DEFAULT_THINKING_BUDGET

  // When attachments are present, switch from string-shorthand to streaming
  // input: query() accepts AsyncIterable<SDKUserMessage>, and the message's
  // `content` field is ContentBlockParam[] — this is the only path that
  // accepts ImageBlockParam / DocumentBlockParam alongside text.
  // Files API needs an API key the OAuth token doesn't have, so we expand
  // base64 from the in-memory cache instead. Plain string is still preferred
  // when no attachments — it preserves prompt-cache behaviour and resume
  // semantics.
  let promptInput
  if (hasAttachments) {
    const blocks = []
    const consumed = []
    for (const att of attachments) {
      if (!att?.file_id) continue
      const rec = attachmentLookup(att.file_id, userId)
      if (!rec) {
        console.warn(`[oncall] attachment ${att.file_id} not found / wrong owner`)
        continue
      }
      consumed.push(att.file_id)
      if (rec.kind === 'image') {
        blocks.push({
          type: 'image',
          source: { type: 'base64', media_type: rec.mime, data: rec.buffer.toString('base64') },
        })
      } else if (rec.kind === 'document') {
        // PDF only — Anthropic's base64 document source rejects everything else
        blocks.push({
          type: 'document',
          source: { type: 'base64', media_type: 'application/pdf', data: rec.buffer.toString('base64') },
          ...(rec.filename ? { title: rec.filename } : {}),
        })
      } else {
        // Text: inline into a text block. Keep the original filename + a
        // fenced wrapper so the model knows what it's looking at.
        const text = rec.buffer.toString('utf8')
        blocks.push({
          type: 'text',
          text: `User attached file: \`${rec.filename}\` (${rec.mime || 'text/plain'})\n\n\`\`\`\n${text}\n\`\`\``,
        })
      }
    }
    if (typeof message === 'string' && message.trim()) {
      blocks.push({ type: 'text', text: message })
    }
    if (!blocks.length) {
      res.status(400).json({ error: 'attachments expired or invalid' })
      return
    }
    console.log(`[oncall] /chat with ${blocks.length} content blocks (${consumed.length} attachments, sessionId=${sessionId || 'new'})`)
    promptInput = (async function* () {
      yield {
        type: 'user',
        message: { role: 'user', content: blocks },
        parent_tool_use_id: null,
      }
    })()
    // Free memory once the buffers have been encoded into the prompt.
    for (const id of consumed) attachmentStore.delete(id)
  } else {
    promptInput = message
  }

  const q = query({
    prompt: promptInput,
    options: {
      cwd: cwd || DEFAULT_CWD,
      resume: sessionId || undefined,
      model: model || DEFAULT_MODEL,
      permissionMode: 'bypassPermissions',
      systemPrompt: { type: 'preset', preset: 'claude_code', append: effectiveSystemPrompt },
      // The bundled SDK ships a musl-libc claude binary (small-distro target);
      // this host is glibc Debian and can't dynlink it ("required file not
      // found"). The host has /home/debian/.local/bin/claude (2.1.126,
      // glibc-built). String-prompt mode skips the binary; streaming-input
      // mode (used for image attachments) requires it. Override the path so
      // both modes work.
      pathToClaudeCodeExecutable: process.env.CLAUDE_BIN
        || 'claude',
      thinkingConfig: budget > 0
        ? { type: 'enabled', budgetTokens: budget }
        : { type: 'disabled' },
    },
  })

  // Track this query so /stop can interrupt it.
  let activeKey = sessionId || null
  const entry = { q, res }
  if (activeKey) { active.set(activeKey, entry); liveRuns.set(activeKey, run) }

  // On disconnect: remove subscriber but keep Claude running.
  // The auto-commit in `finally` will still fire, and the client can reconnect.
  // Do NOT remove from `active` here — the client may have aborted the SSE
  // *because* it's about to POST /stop; we need the entry to remain so /stop
  // can find it and call q.interrupt(). `finally` cleans up `active` once the
  // SDK loop actually ends.
  res.on('close', () => {
    if (res.writableEnded) return
    clearInterval(heartbeat)
    run.subs.delete(res)
  })

  let wasInterrupted = false
  let firstMsgSeen = false
  try {
    for await (const msg of q) {
      if (!firstMsgSeen) {
        firstMsgSeen = true
        console.log(`[oncall] /chat first SDK msg type=${msg.type}${msg.subtype ? ' subtype=' + msg.subtype : ''}`)
      }
      if (msg.type === 'system' && msg.subtype === 'init' && msg.session_id) {
        if (!activeKey) {
          activeKey = msg.session_id
          active.set(activeKey, entry)
          liveRuns.set(activeKey, run)
        } else if (activeKey !== msg.session_id) {
          active.delete(activeKey)
          liveRuns.delete(activeKey)
          activeKey = msg.session_id
          active.set(activeKey, entry)
          liveRuns.set(activeKey, run)
        }
        if (!owners.has(msg.session_id)) {
          owners.set(msg.session_id, {
            userId,
            firstMessage: message.slice(0, 80),
            createdAt: Date.now(),
          })
          persistOwners()
        }
      }
      if (msg.type === 'result' && msg.subtype && msg.subtype !== 'success') {
        wasInterrupted = true
      }
      send(msg)
    }
    send({ type: 'done' })
  } catch (err) {
    console.error('[oncall] /chat SDK loop error:', err?.message || err, err?.stack)
    send({ type: 'error', error: err?.message || String(err) })
  } finally {
    clearInterval(heartbeat)
    if (activeKey) active.delete(activeKey)
    run.done = true
    // Auto-commit any file changes this turn produced. Never throws.
    const commit = await autoCommit(cwd || DEFAULT_CWD, activeKey, message, wasInterrupted, userId).catch(() => null)
    if (commit) send({ type: 'commit', ...commit })
    // Flush remaining subs and close them
    for (const sub of run.subs) { try { sub.end() } catch {} }
    run.subs.clear()
    // Keep buffer for 5 min so late reconnects can see results, then clean up.
    if (activeKey) setTimeout(() => liveRuns.delete(activeKey), 300_000)
  }
})

// Reconnect to an ongoing run: replays buffered events then streams new ones.
// Returns JSON { active: false } if no live run exists.
app.get('/sessions/:id/live', async (req, res) => {
  const { id } = req.params
  if (!/^[a-f0-9-]{36}$/.test(id)) { res.status(400).json({ error: 'bad id' }); return }
  const userId = normUser(req.query.userId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  const rec = owners.get(id)
  if (rec && rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }

  const run = liveRuns.get(id)
  if (!run) { res.json({ active: false }); return }

  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache, no-transform')
  res.setHeader('Connection', 'keep-alive')
  res.setHeader('X-Accel-Buffering', 'no')
  res.flushHeaders?.()

  // Replay all buffered events so the client catches up instantly.
  res.write(`data: ${JSON.stringify({ type: 'reconnect' })}\n\n`)
  for (const evt of run.events) {
    res.write(`data: ${JSON.stringify(evt)}\n\n`)
  }

  if (run.done) { res.end(); return }

  // Subscribe to future events.
  run.subs.add(res)
  const hb = setInterval(() => { try { res.write(': hb\n\n') } catch { clearInterval(hb); run.subs.delete(res) } }, 15000)
  res.on('close', () => { clearInterval(hb); run.subs.delete(res) })
})

app.post('/stop', async (req, res) => {
  const { sessionId, userId: rawUserId } = req.body ?? {}
  const userId = normUser(rawUserId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (!sessionId) {
    res.status(400).json({ error: 'sessionId required' })
    return
  }
  const rec = owners.get(sessionId)
  if (rec && rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  const entry = active.get(sessionId)
  if (!entry) {
    res.json({ ok: false, reason: 'not-active' })
    return
  }
  try {
    await entry.q.interrupt?.()
    res.json({ ok: true })
  } catch (err) {
    res.status(500).json({ error: err?.message || String(err) })
  }
})

// ── Revert helpers ────────────────────────────────────────────────────────────

function detectDeploys(files) {
  const actions = []
  if (files.some(f => f.startsWith('apps/web/'))) actions.push('frontend-build')
  if (files.some(f => f.includes('services/pipeline-daemon/main.py'))) actions.push('daemon-restart')
  if (files.some(f => f.startsWith('apps/backend/'))) actions.push('backend-warn')
  return actions
}

async function runDeploy(action, cwd, send) {
  send({ type: 'revert_deploy', action })
  try {
    if (action === 'frontend-build') {
      await execFileP('pnpm', ['build:web'], { cwd, timeout: 120_000 })
      send({ type: 'revert_deploy_done', action })
    } else if (action === 'daemon-restart') {
      await execFileP('sudo', ['systemctl', 'restart', 'pipeline-daemon-platform-next'], { cwd })
      send({ type: 'revert_deploy_done', action })
    } else if (action === 'backend-warn') {
      send({ type: 'revert_deploy_warn', action, command: 'docker compose -f docker-compose.next.yml up -d --build backend' })
    }
  } catch (err) {
    send({ type: 'revert_deploy_error', action, message: err?.message || String(err) })
  }
}

app.post('/revert', async (req, res) => {
  const { type, sha, ref, sessionId, userId: rawUserId } = req.body ?? {}
  const userId = normUser(rawUserId)
  if (!userId) { res.status(400).json({ error: 'userId required' }); return }
  if (sessionId) {
    const rec = owners.get(sessionId)
    if (rec && rec.userId !== userId) { res.status(403).json({ error: 'not-yours' }); return }
  }

  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache, no-transform')
  res.setHeader('Connection', 'keep-alive')
  res.setHeader('X-Accel-Buffering', 'no')
  res.flushHeaders?.()

  const cwd = DEFAULT_CWD
  const send = (obj) => { try { res.write(`data: ${JSON.stringify(obj)}\n\n`) } catch {} }

  try {
    if (type === 'commit') {
      if (!sha || !/^[0-9a-f]{4,40}$/.test(sha)) {
        send({ type: 'revert_error', message: 'Invalid commit SHA' }); res.end(); return
      }
      let files = []
      try {
        const { stdout } = await execFileP('git', ['diff-tree', '--no-commit-id', '-r', '--name-only', sha], { cwd })
        files = stdout.trim().split('\n').filter(Boolean)
      } catch {}

      send({ type: 'revert_progress', message: `Reverting ${sha}…` })
      await execFileP('git', ['revert', '--no-commit', sha], { cwd })
      const revertMsg = `revert(oncall): undo ${sha}\n\nuser: ${userId}\nsession: ${sessionId || 'unknown'}`
      await execFileP('git', ['commit', '-m', revertMsg, '--no-verify'], { cwd })
      const { stdout: newSha } = await execFileP('git', ['rev-parse', '--short', 'HEAD'], { cwd })
      send({ type: 'revert_reverted', sha, newSha: newSha.trim(), files })
      for (const action of detectDeploys(files)) await runDeploy(action, cwd, send)

    } else if (type === 'snapshot') {
      if (!ref || typeof ref !== 'string' || !ref.startsWith('refs/oncall-snapshots/')) {
        send({ type: 'revert_error', message: 'Invalid snapshot ref' }); res.end(); return
      }
      let snapSha
      try {
        const { stdout } = await execFileP('git', ['rev-parse', ref], { cwd })
        snapSha = stdout.trim()
      } catch {
        send({ type: 'revert_error', message: `Snapshot not found: ${ref}` }); res.end(); return
      }

      // Stash commits have 2+ parents (index tree + optional untracked); regular commits have 1.
      const { stdout: parentLine } = await execFileP('git', ['log', '-1', '--format=%P', snapSha], { cwd })
      const parents = parentLine.trim().split(' ').filter(Boolean)
      const isStash = parents.length > 1
      const headAtSnap = isStash ? parents[0] : snapSha

      let changedFiles = []
      try {
        const { stdout: cur } = await execFileP('git', ['rev-parse', 'HEAD'], { cwd })
        const { stdout: df } = await execFileP('git', ['diff', '--name-only', headAtSnap, cur.trim()], { cwd })
        changedFiles = df.trim().split('\n').filter(Boolean)
      } catch {}

      send({ type: 'revert_progress', message: `Resetting to snapshot ${ref.replace('refs/oncall-snapshots/', '')}…` })
      await execFileP('git', ['reset', '--hard', headAtSnap], { cwd })

      if (isStash) {
        send({ type: 'revert_progress', message: 'Restoring uncommitted changes…' })
        await execFileP('git', ['stash', 'apply', snapSha], { cwd }).catch(() => {})
      }

      send({ type: 'revert_reverted', ref, files: changedFiles })
      for (const action of detectDeploys(changedFiles)) await runDeploy(action, cwd, send)

    } else {
      send({ type: 'revert_error', message: 'Unknown revert type' }); res.end(); return
    }

    send({ type: 'revert_done' })
  } catch (err) {
    send({ type: 'revert_error', message: err?.message || String(err) })
  } finally {
    res.end()
  }
})

async function autoCommit(cwd, sessionId, message, interrupted, userId) {
  // Bail fast if the working tree is clean relative to HEAD.
  const { stdout: dirty } = await execFileP('git', ['status', '--porcelain'], { cwd })
  if (!dirty.trim()) return null

  // Stage everything the turn touched, including new files.
  await execFileP('git', ['add', '-A'], { cwd })
  // Re-check in case everything was already ignored.
  const { stdout: staged } = await execFileP('git', ['diff', '--cached', '--stat'], { cwd })
  if (!staged.trim()) return null

  const sid = (sessionId || 'no-sid').slice(0, 8)
  const prefix = interrupted ? 'oncall[interrupted]' : 'oncall'
  const firstLine = message.split('\n')[0].slice(0, 72).replace(/\s+/g, ' ').trim() || '(empty)'
  const body = `turn triggered by: ${message.slice(0, 400)}\n\nuser: ${userId || 'unknown'}\nsession: ${sessionId || 'unknown'}\nauthored-by: claude-sonnet-4-6 via oncall sidecar`
  const msg = `${prefix}: ${firstLine}\n\n${body}`

  try {
    await execFileP('git', ['commit', '-m', msg, '--no-verify'], { cwd })
    const { stdout: sha } = await execFileP('git', ['rev-parse', '--short', 'HEAD'], { cwd })
    const { stdout: files } = await execFileP('git', ['diff', '--name-only', 'HEAD~1', 'HEAD'], { cwd })
    return { sha: sha.trim(), files: files.trim().split('\n').filter(Boolean), interrupted }
  } catch (err) {
    console.error('[oncall] autoCommit failed:', err?.message || err)
    return null
  }
}

async function snapshotRepo(cwd, sessionId, message) {
  // Include untracked files too (--include-untracked), so new files Claude
  // creates before we snapshot (unlikely, but safe) are captured; and so
  // any unstaged edits already in the tree are saved.
  const headRef = (await execFileP('git', ['rev-parse', 'HEAD'], { cwd })).stdout.trim()
  let snapSha = ''
  try {
    const { stdout } = await execFileP('git', ['stash', 'create', '-u',
      `oncall: before "${message.slice(0, 60).replace(/"/g, '')}"`,
    ], { cwd })
    snapSha = stdout.trim()
  } catch { /* no changes to stash */ }
  // If stash create returned empty, the working tree matches HEAD — just
  // point the snapshot ref at HEAD so there's still a record of the run.
  const targetSha = snapSha || headRef
  const ts = Math.floor(Date.now() / 1000)
  const sid = (sessionId || 'pre-init').slice(0, 8)
  const refName = `refs/oncall-snapshots/${sid}/${ts}`
  await execFileP('git', ['update-ref', refName, targetSha], { cwd })
  return refName
}

async function readFirstUserText(file) {
  // Read up to 256KB of header, scan for the first real user text.
  const fh = await fs.promises.open(file, 'r')
  try {
    const buf = Buffer.alloc(256 * 1024)
    const { bytesRead } = await fh.read(buf, 0, buf.length, 0)
    const text = buf.slice(0, bytesRead).toString('utf8')
    for (const line of text.split('\n')) {
      if (!line.trim()) continue
      let ev
      try { ev = JSON.parse(line) } catch { continue }
      if (ev.isSidechain) continue
      if (ev.type !== 'user' || !ev.message?.content) continue
      const c = ev.message.content
      if (typeof c === 'string') return c.slice(0, 80)
      if (Array.isArray(c)) {
        const first = c.find(p => p?.type === 'text' && p?.text)
        if (first) return String(first.text).slice(0, 80)
      }
    }
  } finally {
    await fh.close()
  }
  return ''
}

function replayTurns(lines) {
  const turns = []
  let current = null
  const pushAssistant = (ev) => {
    const blocks = Array.isArray(ev.message?.content) ? ev.message.content : []
    const text = blocks.filter(b => b?.type === 'text').map(b => b.text).join('\n')
    if (current?.role === 'assistant') {
      current.blocks.push(...blocks)
      if (text) current.text += (current.text ? '\n' : '') + text
    } else {
      current = { id: ev.uuid, role: 'assistant', text, blocks: [...blocks] }
      turns.push(current)
    }
  }
  for (const line of lines) {
    if (!line.trim()) continue
    let ev
    try { ev = JSON.parse(line) } catch { continue }
    if (ev.isSidechain) continue
    if (ev.type === 'user' && ev.message?.content) {
      const content = ev.message.content
      const blocks = Array.isArray(content)
        ? content
        : [{ type: 'text', text: String(content) }]
      const textBlocks = blocks.filter(b => b?.type === 'text')
      const toolResults = blocks.filter(b => b?.type === 'tool_result')
      if (toolResults.length && current?.role === 'assistant') {
        current.blocks.push(...toolResults)
      }
      if (textBlocks.length) {
        current = {
          id: ev.uuid,
          role: 'user',
          text: textBlocks.map(b => b.text).join('\n'),
          blocks: textBlocks,
        }
        turns.push(current)
      }
    } else if (ev.type === 'assistant' && ev.message?.content) {
      pushAssistant(ev)
    }
  }
  return turns
}

// GET /proactive-queue — sandbox-only feed of nudges the proactive-watch
// background scanner has emitted but the UI hasn't dismissed.
app.get('/proactive-queue', (req, res) => {
  if (req.headers['x-oncall-mode'] !== 'sandbox') {
    return res.status(404).json({ error: 'sandbox-only' })
  }
  res.json({ items: getQueue() })
})

// POST /proactive-queue/dismiss  body: { id }
app.post('/proactive-queue/dismiss', (req, res) => {
  if (req.headers['x-oncall-mode'] !== 'sandbox') {
    return res.status(404).json({ error: 'sandbox-only' })
  }
  const { id } = req.body ?? {}
  if (!id) return res.status(400).json({ error: 'id required' })
  clearQueueItem(id)
  res.json({ ok: true })
})

app.listen(PORT, BIND, () => {
  console.log(`[soundlabel-ops] listening on ${BIND}:${PORT}, cwd=${DEFAULT_CWD}, model=${DEFAULT_MODEL}`)
  // Start proactive scanner (15 min cadence by default). Persists state
  // and queue under /srv/oncall-memory/sandbox/.proactive-state.json.
  startProactiveWatch()
  // 6h auto-reflection — the agent thinks-while-you-sleep. Skipped if no
  // STEP2_ANTHROPIC_API_KEY (no point spending tokens on logging "skipped"
  // every cycle).
  startAutoReflection()
})
