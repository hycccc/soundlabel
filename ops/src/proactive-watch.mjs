// proactive-watch — periodic scan for "things the agent should mention to the
// user without being asked". Scans every PROACTIVE_INTERVAL_MS, emits at
// most one bullet per scan, with cross-scan dedup so the user doesn't get
// the same nudge twice.
//
// Signals watched (cheap to query, no LLM call required):
//   - trending awaiting_review queue length grows ≥ 3
//   - any artist with no new release in ≥ 7 days
//   - sandbox pipeline daemon repeated failures on the same step
//   - momentum log appended in last hour (already toasted but worth recap)
//
// When something fires, write a small JSON payload to
// /srv/oncall-memory/sandbox/proactive-queue.json. Frontend polls this
// (or socket pushes — TODO: wire to broadcast() too). For now, a polling
// frontend nudge component reads + drops in toast queue, max
// 3/day per device.
//
// Daily cap is enforced on the frontend (per-device localStorage). This
// module just emits the candidate signals.

import fs from 'node:fs'
import path from 'node:path'

const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const PIPELINE_DIR = process.env.SANDBOX_PIPELINE_DIR || '/srv/music-files-sandbox/pipeline'
const QUEUE_FILE = path.join(MEMORY_DIR, 'proactive-queue.json')
const STATE_FILE = path.join(MEMORY_DIR, '.proactive-state.json')
const PROACTIVE_INTERVAL_MS = Number(process.env.PROACTIVE_INTERVAL_MS || 15 * 60 * 1000)
const BACKEND_BASE = process.env.SANDBOX_BACKEND_URL || 'http://localhost:3220'

function readJSON(p, fallback) {
  try { return JSON.parse(fs.readFileSync(p, 'utf-8')) } catch { return fallback }
}

function writeJSON(p, data) {
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true })
    fs.writeFileSync(p, JSON.stringify(data, null, 2), 'utf-8')
  } catch (e) {
    console.warn(`[proactive] write ${p} failed: ${e.message}`)
  }
}

function loadState() {
  return readJSON(STATE_FILE, { dedup: {}, lastScan: 0 })
}

function saveState(state) {
  // audit #21: prune dedup entries older than 24h so the map can't grow
  // unbounded across the daemon's lifetime (dedup window is only 6h).
  const cutoff = Date.now() - 24 * 60 * 60 * 1000
  if (state.dedup) {
    for (const [k, ts] of Object.entries(state.dedup)) {
      if (typeof ts === 'number' && ts < cutoff) delete state.dedup[k]
    }
  }
  writeJSON(STATE_FILE, state)
}

function enqueue(state, signal) {
  // Dedup within 6h so the same signal doesn't double-fire.
  const dedupKey = signal.dedupKey || signal.id
  const last = state.dedup[dedupKey] || 0
  if (Date.now() - last < 6 * 60 * 60 * 1000) return false
  state.dedup[dedupKey] = Date.now()

  const queue = readJSON(QUEUE_FILE, [])
  queue.push({ ...signal, ts: new Date().toISOString() })
  // Cap queue size to last 30 items
  while (queue.length > 30) queue.shift()
  writeJSON(QUEUE_FILE, queue)
  return true
}

async function fetchTrendStats() {
  try {
    const r = await fetch(`${BACKEND_BASE}/api/trends/stats/today`, {
      signal: AbortSignal.timeout(2000),
    })
    if (!r.ok) return null
    return await r.json()
  } catch {
    return null
  }
}

async function checkTrendingBacklog(state) {
  const stats = await fetchTrendStats()
  if (!stats) return
  const awaiting = stats.byStatus?.awaiting_review ?? 0
  if (awaiting >= 3) {
    enqueue(state, {
      id: `trending-backlog-${new Date().toISOString().slice(0, 10)}`,
      dedupKey: 'trending-backlog',
      severity: 'warning',
      emoji: '⏳',
      message: `${awaiting} trending items awaiting review`,
      detail: `Review the queue and decide publish/discard. Each autopilot item already spent generation credits; letting them sit wastes them.`,
      cta: { label: 'Review', href: '/trends/review' },
    })
  }
  const queued = stats.byStatus?.queued ?? 0
  if (queued >= 2) {
    enqueue(state, {
      id: `autopilot-queue-${new Date().toISOString().slice(0, 10)}`,
      dedupKey: 'autopilot-queue',
      severity: 'info',
      emoji: '🤖',
      message: `Autopilot has ${queued} adaptations queued`,
      detail: `They enter generating at ~60s each. Remaining daily budget ${stats.remainingAdoptBudget ?? '?'}/${stats.dailyAdoptLimit ?? '?'}.`,
    })
  }
}

async function checkPipelineFailures(state) {
  // Walk most recent 3 batches' _summary.json for failed steps.
  let batchNames = []
  try {
    batchNames = fs.readdirSync(PIPELINE_DIR).filter((n) => n.startsWith('batch-')).sort().slice(-3)
  } catch { return }
  for (const b of batchNames) {
    try {
      const s = JSON.parse(fs.readFileSync(path.join(PIPELINE_DIR, b, '_summary.json'), 'utf-8'))
      const failed = (s.songs || []).filter((x) => x.status === 'failed')
      if (failed.length >= 2) {
        enqueue(state, {
          id: `pipeline-batch-${b}`,
          dedupKey: `pipeline-batch-${b}`,
          severity: 'error',
          emoji: '⚠',
          message: `Batch \`${b}\` has ${failed.length} failed songs`,
          detail: failed.slice(0, 2).map((x) => `${x.song_key}: ${x.error || x.step || 'unknown'}`).join(' · '),
          cta: { label: 'View jobs', href: '/jobs' },
        })
      }
    } catch { /* missing summary, skip */ }
  }
}

async function checkArtistDormant(state) {
  try {
    const artistsDir = path.join(MEMORY_DIR, 'artists')
    const slugs = fs.readdirSync(artistsDir).filter((n) => !n.startsWith('.'))
    // For each slug, find latest successes.md mtime as a cheap "last touched"
    // proxy. If > 14 days, nudge once.
    const cutoff = Date.now() - 14 * 24 * 60 * 60 * 1000
    const stale = []
    for (const slug of slugs) {
      let mostRecent = 0
      for (const f of ['successes.md', 'failures.md', 'sonic-profile.md']) {
        try {
          const stat = fs.statSync(path.join(artistsDir, slug, f))
          if (stat.mtimeMs > mostRecent) mostRecent = stat.mtimeMs
        } catch { /* missing file */ }
      }
      if (mostRecent && mostRecent < cutoff) stale.push({ slug, daysQuiet: Math.floor((Date.now() - mostRecent) / (24 * 60 * 60 * 1000)) })
    }
    if (stale.length > 0) {
      const top = stale.sort((a, b) => b.daysQuiet - a.daysQuiet).slice(0, 3)
      enqueue(state, {
        id: `artist-dormant-${new Date().toISOString().slice(0, 10)}`,
        dedupKey: 'artist-dormant',
        severity: 'info',
        emoji: '👤',
        message: `${stale.length} artists inactive for 14+ days`,
        detail: top.map((s) => `${s.slug} (${s.daysQuiet}d)`).join(' · '),
      })
    }
  } catch { /* memory dir missing */ }
}

async function scanOnce() {
  const state = loadState()
  state.lastScan = Date.now()
  try {
    await Promise.all([
      checkTrendingBacklog(state),
      checkPipelineFailures(state),
      checkArtistDormant(state),
    ])
  } catch (e) {
    console.warn(`[proactive] scan crashed: ${e.message}`)
  }
  saveState(state)
}

export function startProactiveWatch() {
  if (process.env.PROACTIVE_WATCH_DISABLED === '1') return
  // First scan after 90s (let server settle), then on cadence.
  setTimeout(() => { void scanOnce() }, 90_000)
  setInterval(() => { void scanOnce() }, PROACTIVE_INTERVAL_MS)
}

export function getQueue() {
  return readJSON(QUEUE_FILE, [])
}

export function clearQueueItem(id) {
  const queue = readJSON(QUEUE_FILE, [])
  const out = queue.filter((x) => x.id !== id)
  writeJSON(QUEUE_FILE, out)
}
