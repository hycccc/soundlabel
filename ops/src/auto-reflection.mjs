// auto-reflection — every 6h the sidecar wakes the agent to "look at the last
// 24h" and write a reflection note. Output lands in
// /srv/oncall-memory/sandbox/reflections/auto-YYYY-MM-DD-HH.md and, if
// anything actionable surfaces, gets enqueued onto the proactive-queue
// so the frontend XiaoqianNudges picks it up.
//
// This is the "thinking partner that doesn't sleep" piece — even if the
// user doesn't open the app, when they DO open it the daily brief +
// nudges already have synthesised takeaways.
//
// Dedup: if no API key, silently skips (we'd burn the cron without
// output). One run per hour-bucket — re-launching the sidecar inside
// the same 6h window doesn't re-run.
//
// Cost: ~1500 input tokens (persona + context) + ~300 output tokens per
// run = ~4 runs/day = ~7K tokens/day total. Negligible.

import Anthropic from '@anthropic-ai/sdk'
import fs from 'node:fs'
import path from 'node:path'
import { buildContextBlock, loadPersona } from './context-aggregator.mjs'

const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const REFLECTIONS_DIR = path.join(MEMORY_DIR, 'reflections')
const PROACTIVE_QUEUE = path.join(MEMORY_DIR, 'proactive-queue.json')
const AUTO_REFLECT_INTERVAL_MS = Number(process.env.AUTO_REFLECT_INTERVAL_MS || 6 * 60 * 60 * 1000)

function getApiKey() {
  return process.env.STEP2_ANTHROPIC_API_KEY
    || process.env.ANTHROPIC_API_KEY
    || ''
}

function bucketKey() {
  const d = new Date()
  // 6-hour buckets: 00/06/12/18 (UTC) so cron drift doesn't double-run.
  const hr = Math.floor(d.getUTCHours() / 6) * 6
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}-${String(hr).padStart(2, '0')}`
}

function reflectionPath(bucket = bucketKey()) {
  return path.join(REFLECTIONS_DIR, `auto-${bucket}.md`)
}

function enqueueNudge(payload) {
  try {
    fs.mkdirSync(MEMORY_DIR, { recursive: true })
    let queue = []
    try { queue = JSON.parse(fs.readFileSync(PROACTIVE_QUEUE, 'utf-8')) } catch { /* not yet */ }
    queue.push({ ...payload, ts: new Date().toISOString() })
    while (queue.length > 30) queue.shift()
    fs.writeFileSync(PROACTIVE_QUEUE, JSON.stringify(queue, null, 2), 'utf-8')
  } catch (e) {
    console.warn(`[auto-reflect] queue write failed: ${e.message}`)
  }
}

async function reflectOnce() {
  const bucket = bucketKey()
  if (fs.existsSync(reflectionPath(bucket))) return  // already done this 6h bucket
  const apiKey = getApiKey()
  if (!apiKey) return  // silently skip when no key — won't pollute logs

  const persona = loadPersona()
  const context = await buildContextBlock()
  const client = new Anthropic({ apiKey })

  const system = [
    { type: 'text', text: persona || 'You are the operations agent.' },
    { type: 'text', text: `# Current snapshot\n\n${context}`, cache_control: { type: 'ephemeral' } },
  ]

  const userPrompt = `No user is present. This is your own 6-hourly self-reflection.

Task: from the last 24h of data in the snapshot above, find 0-3 **informative** insights. 1-2 sentences each.

If there is **nothing worth saying, output only "NONE"** — never pad. Junk insights are worse than silence.

What counts:
- Anomaly signals (spikes / crashes / repeated failures / long droughts)?
- Hidden cross-data correlations (artist A gaining traffic ∧ artist B getting rejects)?
- An actionable suggestion for what the operator should do today?

Format (markdown, no preamble):
\`\`\`
- 🔥 [finding 1]
- ⚠ [finding 2]
- 💡 [suggestion]
\`\`\`

Or, if nothing:
\`\`\`
NONE
\`\`\``

  try {
    const t0 = Date.now()
    const response = await client.messages.create({
      model: 'claude-opus-4-7',
      max_tokens: 800,
      thinking: { type: 'adaptive' },
      system,
      messages: [{ role: 'user', content: userPrompt }],
    })
    let text = ''
    for (const block of response.content) if (block.type === 'text') text += block.text
    text = text.trim()
    const elapsed = Date.now() - t0

    // Persist regardless (so we know we ran, even on "NONE")
    fs.mkdirSync(REFLECTIONS_DIR, { recursive: true })
    const header = `# Auto-reflection · ${bucket}\n\n_generated ${new Date().toISOString()} (${elapsed}ms)_\n\n`
    fs.writeFileSync(reflectionPath(bucket), header + text + '\n', 'utf-8')

    // If meaningful, surface to nudge queue. "NONE" = explicit no-op.
    if (text && text.replace(/[\s\n]/g, '') !== 'NONE') {
      // Pull the first non-empty bullet as the nudge headline.
      const firstBullet = text.split('\n').map((l) => l.trim()).find((l) => l.startsWith('-'))
      if (firstBullet) {
        const cleaned = firstBullet.replace(/^[-*]\s*/, '').slice(0, 200)
        enqueueNudge({
          id: `auto-reflect-${bucket}`,
          dedupKey: `auto-reflect-${bucket}`,
          severity: 'info',
          emoji: '🧠',
          message: '6h auto-reflection',
          detail: cleaned,
        })
      }
    }
  } catch (e) {
    console.warn(`[auto-reflect] failed: ${e.message}`)
  }
}

let reflectRunning = false
async function reflectOnceGuarded() {
  // audit (overlap): don't let a slow 6h run overlap the next tick.
  if (reflectRunning) return
  reflectRunning = true
  try { await reflectOnce() } finally { reflectRunning = false }
}

export function startAutoReflection() {
  // OPT-IN: auto-reflection spends Anthropic tokens unattended every 6h.
  // Off unless AUTO_REFLECT_ENABLED=1 — same safety principle as the trend
  // autopilot (nothing spends money/tokens on a timer by default). It also
  // no-ops without an API key, but the explicit gate keeps it dormant in
  // every container until you turn it on.
  if (process.env.AUTO_REFLECT_ENABLED !== '1') return
  // First run 5 min after startup (let everything settle), then every 6h.
  setTimeout(() => { void reflectOnceGuarded() }, 5 * 60 * 1000)
  setInterval(() => { void reflectOnceGuarded() }, AUTO_REFLECT_INTERVAL_MS)
}
