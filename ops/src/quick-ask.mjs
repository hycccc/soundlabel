// quick-ask — on-demand short-form responses from the agent for inline UI tips
// (Generator prompt review, Library song commentary, etc.).
//
// Distinct from /chat: no streaming, no tool use, no agent loop. Just a
// single Anthropic SDK call with persona + context + the caller-supplied
// query. Returns plain markdown text. ~1-3s typical.
//
// Caching: same exact (query + date) returns cached output to avoid
// burning tokens on the same UI scenario. Keyed by sha256(persona +
// context + query) so context staleness doesn't poison.

import Anthropic from '@anthropic-ai/sdk'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { buildContextBlock, loadPersona } from './context-aggregator.mjs'

const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const CACHE_DIR = path.join(MEMORY_DIR, '.quick-ask-cache')

const inflightCache = new Map() // hash → promise (in-process dedup)

function ensureCacheDir() {
  try { fs.mkdirSync(CACHE_DIR, { recursive: true }) } catch { /* noop */ }
}

function getApiKey() {
  return process.env.STEP2_ANTHROPIC_API_KEY
    || process.env.ANTHROPIC_API_KEY
    || process.env.ANTHROPIC_ADMIN_API_KEY
    || ''
}

export async function quickAsk({ query, instruction, includeContext = true, maxTokens = 700, useCache = true, heatSuffix = '', heatBand = 'default' } = {}) {
  if (!query || typeof query !== 'string') {
    return { content: '', error: 'query required' }
  }
  const apiKey = getApiKey()
  if (!apiKey) {
    return { content: '⚠ No ANTHROPIC API key configured; commentary unavailable.', error: 'no_api_key' }
  }

  const persona = loadPersona()
  const context = includeContext ? await buildContextBlock() : ''

  // Cache key: persona + context (date-stamped via context) + query +
  // instruction + heat band. Lives ~1 day before context block rotates.
  // Heat band is part of the key so spicy/ice variants don't collide.
  const hash = crypto
    .createHash('sha256')
    .update([persona, context, instruction || '', query, heatBand].join('\n'))
    .digest('hex')
    .slice(0, 24)

  if (useCache) {
    ensureCacheDir()
    const cacheFile = path.join(CACHE_DIR, `${hash}.txt`)
    try {
      const cached = fs.readFileSync(cacheFile, 'utf-8')
      if (cached) return { content: cached, cached: true }
    } catch { /* not cached yet */ }

    // In-flight dedup so 5 simultaneous identical asks share one API call.
    if (inflightCache.has(hash)) {
      return await inflightCache.get(hash)
    }
  }

  const run = (async () => {
    const client = new Anthropic({ apiKey })
    const systemBlocks = [
      { type: 'text', text: persona || 'You are the AI principal of this label.' },
    ]
    if (context) {
      systemBlocks.push({ type: 'text', text: `# Current snapshot\n\n${context}`, cache_control: { type: 'ephemeral' } })
    }
    if (heatSuffix) {
      systemBlocks.push({ type: 'text', text: heatSuffix })
    }
    const userBlock = [
      instruction || 'Based on the persona + context above, give a short judgement of the input below.',
      '',
      'Input:',
      query,
      '',
      'Rules:',
      '- Keep it between 50-200 characters.',
      '- No hedging: state the risks / improvements directly.',
      '- No filler like greetings or "hope this helps".',
      '- If there is nothing to flag, say "looks fine" — never pad.',
    ].join('\n')

    try {
      const response = await client.messages.create({
        model: 'claude-opus-4-7',
        max_tokens: maxTokens,
        thinking: { type: 'adaptive' },
        system: systemBlocks,
        messages: [{ role: 'user', content: userBlock }],
      })
      let textOut = ''
      for (const block of response.content) {
        if (block.type === 'text') textOut += block.text
      }
      textOut = textOut.trim()
      if (useCache && textOut) {
        try {
          fs.writeFileSync(path.join(CACHE_DIR, `${hash}.txt`), textOut, 'utf-8')
        } catch { /* noop */ }
      }
      return { content: textOut, cached: false, usage: response.usage }
    } catch (e) {
      return { content: `⚠ Commentary failed: ${e.message}`, error: e.message }
    }
  })()

  if (useCache) inflightCache.set(hash, run)
  try {
    return await run
  } finally {
    if (useCache) inflightCache.delete(hash)
  }
}
