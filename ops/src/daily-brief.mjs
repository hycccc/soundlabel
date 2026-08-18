// daily-brief — generates the "the agent morning report" once per (date, user)
// and caches it on disk. UI shows it in a modal at first daily app open.
//
// Source of truth for the brief content: Anthropic SDK direct call
// (NOT the Claude Code agent SDK — we don't need tools, we want a plain
// markdown response). Persona + context aggregator output go in as the
// system prompt with cache_control so subsequent same-day fetches by
// different users hit the API cache.
//
// Brief output format (the model is told to follow this):
//   - 5-8 bullets max
//   - Each starts with one of: 🌅 brief / 🔥 spike / ⚠ anomaly / 💡 suggestion / 📊 data
//   - One concrete actionable suggestion at the bottom (what to do today)
//   - No filler (no greetings, no "hope this helps")
//
// Cache: /srv/oncall-memory/sandbox/daily-briefs/YYYY-MM-DD.md
//   - One file per date, all users share the same brief (data is shared too)
//   - First request of the day pays the API call; rest read from disk
//   - Re-running with ?force=1 regenerates

import Anthropic from '@anthropic-ai/sdk'
import fs from 'node:fs'
import path from 'node:path'
import { buildContextBlock, loadPersona } from './context-aggregator.mjs'

const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const BRIEFS_DIR = path.join(MEMORY_DIR, 'daily-briefs')

function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function briefPath(date = todayStr()) {
  return path.join(BRIEFS_DIR, `${date}.md`)
}

function readCachedBrief(date = todayStr()) {
  try {
    const text = fs.readFileSync(briefPath(date), 'utf-8')
    return text
  } catch {
    return null
  }
}

function writeCachedBrief(date, text) {
  try {
    fs.mkdirSync(BRIEFS_DIR, { recursive: true })
    fs.writeFileSync(briefPath(date), text, 'utf-8')
  } catch (e) {
    console.warn(`[daily-brief] cache write failed: ${e.message}`)
  }
}

function getApiKey() {
  return process.env.STEP2_ANTHROPIC_API_KEY
    || process.env.ANTHROPIC_API_KEY
    || process.env.ANTHROPIC_ADMIN_API_KEY
    || ''
}

export async function generateBrief({ force = false, date = todayStr(), heatSuffix = '', heatBand = 'default' } = {}) {
  // Cache key includes heat band — different bands get different briefs.
  const cacheSuffix = heatBand && heatBand !== 'default' ? `-${heatBand}` : ''
  const cacheDate = date + cacheSuffix
  if (!force) {
    const cached = readCachedBrief(cacheDate)
    if (cached) return { content: cached, cached: true, heat: heatBand }
  }
  const apiKey = getApiKey()
  if (!apiKey) {
    return {
      content: '⚠ No ANTHROPIC API key configured (STEP2_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY); cannot generate the brief.',
      cached: false,
      error: 'no_api_key',
    }
  }

  const persona = loadPersona()
  const context = await buildContextBlock()
  const client = new Anthropic({ apiKey })

  const system = [
    {
      type: 'text',
      text: persona || 'You are the AI principal of this label.',
    },
    {
      type: 'text',
      text: `# Current snapshot\n\n${context}`,
      cache_control: { type: 'ephemeral' },
    },
  ]
  // Heat dial tone override — appended AFTER the cached blocks so it
  // doesn't bust the persona+context cache key. The suffix is short so
  // the uncached cost is negligible.
  if (heatSuffix) {
    system.push({ type: 'text', text: heatSuffix })
  }

  const userPrompt = `Generate today's brief. Rules:

- 5-8 bullets, one per line.
- Each starts with an emoji: 🌅 summary / 🔥 spike / ⚠ anomaly / 💡 suggestion / 📊 data
- 1-2 sentences each. Specifics first (numbers, artist names, track titles); no vague filler like "overall looking good".
- The last bullet must be 💡 one actionable suggestion (what to do today).
- No greetings or pleasantries.
- Skip sections with no data; never write junk lines like "no spikes today".
- Markdown output, no wrapping code block.`

  const t0 = Date.now()
  try {
    const response = await client.messages.create({
      model: 'claude-opus-4-7',
      max_tokens: 1024,
      thinking: { type: 'adaptive' },
      system,
      messages: [{ role: 'user', content: userPrompt }],
    })
    let textOut = ''
    for (const block of response.content) {
      if (block.type === 'text') textOut += block.text
    }
    textOut = textOut.trim()
    if (!textOut) {
      return { content: '⚠ The agent produced no text (possibly stuck thinking).', cached: false, error: 'empty' }
    }
    writeCachedBrief(cacheDate, textOut)
    return {
      content: textOut,
      cached: false,
      elapsedMs: Date.now() - t0,
      usage: response.usage,
    }
  } catch (e) {
    console.warn(`[daily-brief] API call failed: ${e.message}`)
    return { content: `⚠ Daily brief failed: ${e.message}`, cached: false, error: e.message }
  }
}
