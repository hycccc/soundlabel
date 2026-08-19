// label-bridge — the sidecar's window into the label workspace.
//
// The Python pipeline exports `state.json` after every batch (see
// src/soundlabel/state.py); this module reads it, renders the label block
// for the agent's system prompt, and writes batch reviews back as
// `batches/<id>/ops-review.json` — which `soundlabel batches` displays.
// The contract is files on the shared workspace volume: no SQL driver, no
// RPC, either side can be down without breaking the other.
//
// Reviews are heuristic by default (deterministic, computed from the batch
// manifest — free, works with zero API keys) and LLM-backed on request
// (`llm: true`), the same default/opt-in split the Python agents use.

import fs from 'node:fs'
import path from 'node:path'

export const WORKSPACE = process.env.SOUNDLABEL_WORKSPACE || ''
const BATCH_ID_RE = /^batch_[0-9a-f]{8}$/ // also a path-traversal guard

export function readState() {
  if (!WORKSPACE) return null
  try {
    return JSON.parse(fs.readFileSync(path.join(WORKSPACE, 'state.json'), 'utf8'))
  } catch {
    return null
  }
}

// One compact Markdown block for the system prompt. Kept stable across
// calls with the same state so the Anthropic prompt cache can hit.
export function buildLabelBlock() {
  const state = readState()
  if (!state) return ''
  const lines = ['# Label state (workspace snapshot)', '']
  if (state.artists?.length) {
    lines.push(`## Roster (${state.artists.length})`)
    for (const a of state.artists) {
      lines.push(`- \`${a.slug}\` ${a.name} [${a.language}]${a.sonic_profile ? ` — ${a.sonic_profile}` : ''}`)
    }
    lines.push('')
  }
  const t = state.tracks
  if (t) {
    lines.push('## Catalog')
    lines.push(`- released tracks: ${t.count}${t.avg_score != null ? `, avg score ${t.avg_score}` : ''}`)
    for (const r of t.recent ?? []) {
      const rec = state.room_reception?.[r.id]
      const room = rec ? ` (room ${rec.avg}×${rec.n})` : ''
      lines.push(`- ${r.id} ${r.score} ${r.artist} — ${r.title}${room}`)
    }
    lines.push('')
  }
  if (state.batches?.length) {
    lines.push(`## Recent batches`)
    for (const b of state.batches) {
      lines.push(`- ${b.id} [${b.status}] ${b.artist} via ${b.backend}`)
    }
  }
  return lines.join('\n').trim()
}

export function readManifest(batchId) {
  if (!WORKSPACE || !BATCH_ID_RE.test(batchId)) return null
  try {
    return JSON.parse(
      fs.readFileSync(path.join(WORKSPACE, 'batches', batchId, 'manifest.json'), 'utf8'),
    )
  } catch {
    return null
  }
}

function stepsByName(manifest) {
  const by = {}
  for (const s of manifest.steps ?? []) (by[s.step] ??= []).push(s)
  return by
}

// Deterministic review from the manifest plus the label state: what
// happened, why, and the one next action. No tokens spent; same inputs →
// same review. Room reception (when the workspace has it) outranks the
// critic's verdict for released tracks — the room heard it, the critic
// only measured it.
//
// The thresholds come from state.json's room_policy, exported by the Python
// side from agents/anr.py — the single source of the reception policy, so
// the A&R agent and this reviewer can never disagree about what "cold"
// means. The literals below are only the fallback for a missing policy key.
const DEFAULT_ROOM_POLICY = { min_scores: 2, cold_avg: 6.0, loved_avg: 8.0 }

export function heuristicReview(manifest, state = readState()) {
  const policy = { ...DEFAULT_ROOM_POLICY, ...(state?.room_policy ?? {}) }
  const by = stepsByName(manifest)
  const brief = by.brief?.[0]?.brief
  const score = by.score?.[0]
  const critic = by.critic?.[0]
  const genSteps = by.generate ?? []
  const genErrors = genSteps.filter((s) => s.error).map((s) => s.error)

  const notes = []
  if (brief?.style_tags?.length) notes.push(`brief: ${brief.theme ?? '?'} (${brief.style_tags.join(', ')})`)
  if (score && !score.gate_passed) notes.push(`gate FAILED: ${(score.gate_reasons ?? []).join('; ')}`)
  for (const e of genErrors) notes.push(`generate error: ${e}`)
  for (const r of critic?.reasons ?? []) notes.push(`critic: ${r}`)

  const trackId = by.catalog?.[0]?.track_id
  const room = trackId ? state?.room_reception?.[trackId] ?? null : null

  let headline
  let action
  const status = manifest.status
  if (status === 'released') {
    headline = `released — rank ${score?.rank ?? '?'}/10 (${score?.scorer ?? '?'}), critic accepted`
    action = 'queue for a listening-room session before promo'
    if (room) {
      notes.push(`room reception: ${room.avg}×${room.n}`)
      if (room.n >= policy.min_scores && room.avg < policy.cold_avg) {
        headline = `released, but the room is cold on it (${room.avg}×${room.n})`
        action = 'pull it from the promo queue — the room outvoted the critic'
      } else if (room.n >= policy.min_scores && room.avg >= policy.loved_avg) {
        headline = `released and the room loves it (${room.avg}×${room.n})`
        action = 'fast-track promo and open the next session with it'
      }
    }
  } else if (status === 'redo') {
    headline = `critic sent it back: ${critic?.reasons?.[0] ?? 'no reason recorded'}`
    action = 'rerun with the brief adjusted for the first critic reason'
  } else if (status === 'killed') {
    headline = `critic killed it: ${critic?.reasons?.[0] ?? 'no reason recorded'}`
    action = 'drop this direction for the artist; pick a different theme next batch'
  } else if (status === 'failed') {
    headline = `generation failed after ${genSteps.length} attempt(s)`
    action = `inspect the backend: ${genErrors.at(-1) ?? 'no error recorded'}`
  } else if (status === 'refused') {
    headline = 'cost guard refused the paid backend'
    action = 'rerun with --allow-paid if the spend is intended'
  } else {
    headline = `status: ${status}`
    action = 'inspect the manifest'
  }

  return {
    batch_id: manifest.batch_id,
    source: 'heuristic',
    status,
    headline,
    notes,
    action,
    created_at: Date.now() / 1000,
  }
}

// LLM review: same shape, written by a model that sees everything the
// heuristic sees — manifest, label state, this track's room reception, and
// the reception policy — plus the deterministic baseline review itself, so
// the paid path is never LESS informed than the free one. Falls back to the
// heuristic on any failure — a review request never errors just because a
// model call did.
export async function llmReview(manifest, { model, apiKey } = {}) {
  const state = readState()
  const fallback = heuristicReview(manifest, state)
  if (!apiKey) return { ...fallback, llm_error: 'no API key configured' }
  try {
    const { default: Anthropic } = await import('@anthropic-ai/sdk')
    const client = new Anthropic({ apiKey })
    const trackId = (manifest.steps ?? []).find((s) => s.step === 'catalog')?.track_id
    const room = trackId ? state?.room_reception?.[trackId] ?? null : null
    const response = await client.messages.create({
      model: model || 'claude-sonnet-4-6',
      max_tokens: 600,
      system:
        'You are the ops reviewer of an AI music label. Given a batch manifest ' +
        'and the label state, return STRICT JSON: {"headline": string (<=100 chars, ' +
        'what happened and the verdict), "notes": string[] (<=4, concrete observations), ' +
        '"action": string (the single next move)}. No prose outside the JSON. ' +
        'House policy: listening-room reception on a released track, at or above ' +
        'the policy min_scores, outranks the critic — cold (avg < cold_avg) means ' +
        'pull it from promo, loved (avg >= loved_avg) means fast-track it.',
      messages: [
        {
          role: 'user',
          content: `Label state:\n${buildLabelBlock() || '(none)'}\n\n` +
            `Reception policy: ${JSON.stringify(state?.room_policy ?? DEFAULT_ROOM_POLICY)}\n` +
            `This track's room reception: ${room ? JSON.stringify(room) : 'none yet'}\n\n` +
            `Deterministic baseline review (improve on it, do not contradict the policy):\n` +
            `${JSON.stringify({ headline: fallback.headline, notes: fallback.notes, action: fallback.action })}\n\n` +
            `Batch manifest:\n${JSON.stringify(manifest, null, 2)}`,
        },
      ],
    })
    const text = response.content.filter((b) => b.type === 'text').map((b) => b.text).join('')
    const parsed = JSON.parse(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1))
    return {
      batch_id: manifest.batch_id,
      source: 'llm',
      status: manifest.status,
      headline: String(parsed.headline ?? fallback.headline).slice(0, 200),
      notes: Array.isArray(parsed.notes) ? parsed.notes.slice(0, 4).map(String) : fallback.notes,
      action: String(parsed.action ?? fallback.action).slice(0, 300),
      created_at: Date.now() / 1000,
    }
  } catch (e) {
    return { ...fallback, llm_error: e?.message || String(e) }
  }
}

export function writeReview(batchId, review) {
  if (!WORKSPACE || !BATCH_ID_RE.test(batchId)) return null
  const file = path.join(WORKSPACE, 'batches', batchId, 'ops-review.json')
  fs.mkdirSync(path.dirname(file), { recursive: true })
  // atomic replace — `soundlabel batches` reads this on its own schedule
  fs.writeFileSync(`${file}.tmp`, JSON.stringify(review, null, 2))
  fs.renameSync(`${file}.tmp`, file)
  return file
}
