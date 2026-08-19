// context-aggregator — assembles the "what's happening right now" block
// that gets injected into the sidecar's system prompt every turn the agent wakes.
//
// Pulls from (all read-only, all best-effort):
//   - backend /api/master/stats               (catalog snapshot)
//   - backend /api/trends/stats/today         (trending pipeline state)
//   - backend /api/metrics/leaderboard        (24h momentum tracks)
//   - /srv/music-files-sandbox/pipeline/*/state.json  (recent batch crit avgs)
//   - /srv/oncall-memory/sandbox/reflections/momentum.md  (recent alerts)
//   - Postgres direct: sandbox Artist count + recent active set
//
// Each source has a hard 1.5s timeout. Anything that throws is silently
// dropped — empty section header is fine; we'd rather render a partial
// brief than make the agent wait 8s on a slow query.
//
// Output: one Markdown block, < ~3KB, designed to be cache-friendly inside
// the Anthropic system-prompt cache window. Same call within 60s gets the
// same string (memoised) to maximise prompt cache hits.

import fs from 'node:fs'
import path from 'node:path'

const BACKEND_BASE = process.env.SANDBOX_BACKEND_URL || 'http://localhost:3220'
const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const PIPELINE_DIR = process.env.SANDBOX_PIPELINE_DIR || '/srv/music-files-sandbox/pipeline'
const FETCH_TIMEOUT_MS = 1500
const CACHE_TTL_MS = 60_000

let cached = { at: 0, block: '' }

async function fetchJSON(url, fallback = null) {
  const ac = new AbortController()
  const t = setTimeout(() => ac.abort(), FETCH_TIMEOUT_MS)
  try {
    const r = await fetch(url, { signal: ac.signal })
    if (!r.ok) return fallback
    return await r.json()
  } catch {
    return fallback
  } finally {
    clearTimeout(t)
  }
}

function fmtBig(n) {
  if (n === null || n === undefined) return '—'
  const num = Number(n)
  if (!Number.isFinite(num)) return String(n)
  const sign = num < 0 ? '-' : ''
  const abs = Math.abs(num)
  if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(2)}b`
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}m`
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)}k`
  return String(Math.round(num))
}

function tailFile(absPath, maxLines = 8) {
  try {
    const content = fs.readFileSync(absPath, 'utf-8')
    const lines = content.split('\n').filter(Boolean)
    return lines.slice(-maxLines).join('\n')
  } catch {
    return ''
  }
}

async function listRecentBatches(limit = 5) {
  try {
    const entries = fs.readdirSync(PIPELINE_DIR)
      .filter((n) => n.startsWith('batch-'))
      .sort()
      .slice(-limit)
    const out = []
    for (const name of entries) {
      // Prefer the cross-batch reflection (Sprint 3.2) — it has avg score,
      // grade mix, and the agent's lesson. Fall back to _summary.json, then bare.
      let reflection = null
      try {
        reflection = JSON.parse(fs.readFileSync(path.join(PIPELINE_DIR, name, `${name}_reflection.json`), 'utf-8'))
      } catch { /* none */ }
      let summary = null
      try {
        summary = JSON.parse(fs.readFileSync(path.join(PIPELINE_DIR, name, '_summary.json'), 'utf-8'))
      } catch { /* none */ }
      out.push({ name, reflection, summary })
    }
    return out
  } catch {
    return []
  }
}

function summarizeBatch(b) {
  if (b.reflection) {
    const r = b.reflection
    const grades = r.grades ? Object.entries(r.grades).map(([g, n]) => `${g}×${n}`).join(' ') : ''
    let line = `- \`${b.name}\` — scored ${r.coverage?.scored ?? '?'}/${r.coverage?.total ?? '?'}`
    if (r.avg_overall != null) line += `, avg ${r.avg_overall}`
    if (grades) line += ` (${grades})`
    if (r.showcase_count) line += `, showcase ${r.showcase_count}`
    if (r.lesson) line += `\n    💡 ${r.lesson}`
    return line
  }
  const s = b.summary
  if (!s) return `- ${b.name} (no summary)`
  const songs = s.songs || []
  const succeeded = songs.filter((x) => x.status === 'success').length
  const failed = songs.filter((x) => x.status === 'failed').length
  return `- \`${b.name}\` — ${songs.length} songs, ${succeeded} ✓ ${failed} ✗`
}

function listRosterSlugs() {
  try {
    return fs.readdirSync(path.join(MEMORY_DIR, 'artists')).filter((n) => !n.startsWith('.'))
  } catch { return [] }
}

export async function buildContextBlock() {
  // Memoise within TTL — same call within 60s reuses the string. Lets
  // Anthropic's prompt cache hit on the system block.
  if (Date.now() - cached.at < CACHE_TTL_MS && cached.block) {
    return cached.block
  }

  const [catalog, trends, leaderboard, batches, roster] = await Promise.all([
    fetchJSON(`${BACKEND_BASE}/api/master/stats`),
    fetchJSON(`${BACKEND_BASE}/api/trends/stats/today`),
    fetchJSON(`${BACKEND_BASE}/api/metrics/leaderboard?metric=plays&range=24h&limit=5`),
    Promise.resolve(listRecentBatches(5)),
    Promise.resolve(listRosterSlugs()),
  ])

  const lines = []
  lines.push(`# Current snapshot（${new Date().toISOString()}）`)
  lines.push('')

  // ── Catalog ──
  if (catalog) {
    lines.push('## Catalog (sandbox)')
    if (typeof catalog.total === 'number') lines.push(`- total tracks: ${catalog.total}`)
    if (typeof catalog.releasedCount === 'number') lines.push(`- released: ${catalog.releasedCount}`)
    if (Array.isArray(catalog.byStatus)) {
      const counts = catalog.byStatus.map((s) => `${s.status}:${s.count}`).join(' / ')
      lines.push(`- status mix: ${counts}`)
    }
    lines.push('')
  }

  // ── Roster ──
  if (roster.length > 0) {
    lines.push(`## Roster (${roster.length} artists)`)
    lines.push(roster.map((s) => `\`${s}\``).join(' · '))
    lines.push('')
  }

  // ── Recent batches ──
  if (batches.length > 0) {
    lines.push('## Last 5 batches')
    for (const b of batches) lines.push(summarizeBatch(b))
    lines.push('')
  }

  // ── Trending pipeline ──
  if (trends) {
    lines.push('## Trend engine state')
    lines.push(`- fetched in 24h: ${trends.total24h ?? 0}`)
    if (trends.byPlatform) {
      const ps = Object.entries(trends.byPlatform).map(([p, n]) => `${p}:${n}`).join(' ')
      lines.push(`- platform mix: ${ps}`)
    }
    if (trends.byStatus) {
      const ss = Object.entries(trends.byStatus).map(([s, n]) => `${s}:${n}`).join(' ')
      lines.push(`- status mix: ${ss}`)
    }
    lines.push(`- adaptation budget: ${trends.remainingAdoptBudget ?? '—'}/${trends.dailyAdoptLimit ?? '—'} remaining today`)
    const awaiting = trends.byStatus?.awaiting_review ?? 0
    if (awaiting > 0) lines.push(`- ⚠ **${awaiting} trending derivatives awaiting review** (/trends/review)`)
    lines.push('')
  }

  // ── 24h momentum ──
  if (leaderboard?.entries?.length > 0) {
    lines.push('## 24h traffic spikes — top 5')
    for (const e of leaderboard.entries.slice(0, 5)) {
      const sign = Number(e.delta) > 0 ? '+' : ''
      lines.push(`- ${e.title}${e.artist ? ` / ${e.artist}` : ''} — ${sign}${fmtBig(e.delta)} (total ${fmtBig(e.total)})`)
    }
    lines.push('')
  }

  // ── Momentum / reflection tail ──
  const momentum = tailFile(path.join(MEMORY_DIR, 'reflections/momentum.md'), 5)
  if (momentum.trim()) {
    lines.push('## Recent momentum spikes')
    lines.push('```')
    lines.push(momentum)
    lines.push('```')
    lines.push('')
  }

  const block = lines.join('\n').trim()
  cached = { at: Date.now(), block }
  return block
}

// Persona loader — read once at server boot, refresh via setInterval daily.
// Appends the catchphrase library as a compact "catchphrase reference" section so
// the agent's voice stays consistent across contexts (critic reject, momentum
// spike, dormant artist, etc.). The library is a reference, not a script
// — the model picks/varies, doesn't recite verbatim.
let personaCache = ''
let personaLoadedAt = 0
const PERSONA_PATH = path.resolve(
  process.env.ONCALL_PERSONA_PATH
    || new URL('../persona/example.md', import.meta.url).pathname,
)
const CATCHPHRASE_PATH = path.resolve(
  process.env.ONCALL_CATCHPHRASE_PATH
    || new URL('../persona/catchphrases.json', import.meta.url).pathname,
)
const PERSONA_TTL_MS = 24 * 60 * 60 * 1000

function buildCatchphraseSection() {
  try {
    const raw = JSON.parse(fs.readFileSync(CATCHPHRASE_PATH, 'utf-8'))
    const lines = ['# Catchphrase reference (adapt to context, never recite verbatim)']
    for (const [tag, phrases] of Object.entries(raw)) {
      if (tag.startsWith('_')) continue  // skip _doc
      if (!Array.isArray(phrases) || phrases.length === 0) continue
      lines.push(`- **${tag}**: ${phrases.map((p) => `"${p}"`).join(' / ')}`)
    }
    return lines.length > 1 ? lines.join('\n') : ''
  } catch {
    return ''
  }
}

export function loadPersona() {
  if (personaCache && Date.now() - personaLoadedAt < PERSONA_TTL_MS) {
    return personaCache
  }
  try {
    const base = fs.readFileSync(PERSONA_PATH, 'utf-8')
    const catchphrases = buildCatchphraseSection()
    personaCache = catchphrases ? `${base}\n\n---\n\n${catchphrases}` : base
    personaLoadedAt = Date.now()
  } catch (e) {
    console.warn(`[oncall] persona file unreadable at ${PERSONA_PATH}: ${e.message}`)
    personaCache = ''
  }
  return personaCache
}
