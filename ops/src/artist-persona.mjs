// artist-persona — detects @artist-slug mentions in user messages and
// loads that artist's 4-file memory directory as a sub-persona context
// block. the agent then answers in that artist's frame (per persona.md rules).
//
// Detection: anywhere in the message we find @<slug> where <slug> is a
// directory under /srv/oncall-memory/sandbox/artists/. Match the FIRST
// one only — if user mentions multiple, take the first.
//
// Output: markdown block to inject into the system prompt, BELOW the
// the agent persona and BEFORE the context aggregator. Includes the 4 files
// (sonic-profile / successes / failures / audience) trimmed to 1.5KB
// each so the total stays under ~6KB.

import fs from 'node:fs'
import path from 'node:path'

const MEMORY_DIR = process.env.ONCALL_MEMORY_DIR || '/srv/oncall-memory/sandbox'
const ARTISTS_DIR = path.join(MEMORY_DIR, 'artists')

let rosterSlugsCache = null
let rosterCacheLoadedAt = 0
const ROSTER_TTL_MS = 5 * 60 * 1000

function loadRosterSlugs() {
  if (rosterSlugsCache && Date.now() - rosterCacheLoadedAt < ROSTER_TTL_MS) {
    return rosterSlugsCache
  }
  try {
    rosterSlugsCache = fs.readdirSync(ARTISTS_DIR).filter((n) => !n.startsWith('.'))
    rosterCacheLoadedAt = Date.now()
  } catch {
    rosterSlugsCache = []
  }
  return rosterSlugsCache
}

// Return the first @slug found in the message, if it matches a known
// roster directory. null if no match.
export function detectArtistMention(message) {
  if (!message || typeof message !== 'string') return null
  // @slug pattern: @[a-z][a-z0-9-]+
  const matches = [...message.matchAll(/@([a-z][a-z0-9-]+)/gi)]
  if (matches.length === 0) return null
  const roster = loadRosterSlugs()
  for (const m of matches) {
    const slug = m[1].toLowerCase()
    if (roster.includes(slug)) return slug
  }
  return null
}

// Read up to 1500 chars from a memory file. Returns '' if missing or
// only contains the placeholder header line.
function readMemoryFile(slug, filename) {
  try {
    const text = fs.readFileSync(path.join(ARTISTS_DIR, slug, filename), 'utf-8').trim()
    // Skip empty placeholder files (the kind we seeded with just a header).
    const lines = text.split('\n').filter((l) => l.trim() && !l.trim().startsWith('>'))
    if (lines.length <= 2) return ''  // basically just the title + maybe one comment
    return text.slice(0, 1500)
  } catch {
    return ''
  }
}

export function buildArtistPersonaBlock(slug) {
  if (!slug) return ''
  const sonic = readMemoryFile(slug, 'sonic-profile.md')
  const successes = readMemoryFile(slug, 'successes.md')
  const failures = readMemoryFile(slug, 'failures.md')
  const audience = readMemoryFile(slug, 'audience.md')

  const sections = []
  sections.push(`# Sub-persona switch → @${slug}`)
  sections.push('')
  sections.push(`The user @-mentioned \`${slug}\`. Per the persona rules, switch to this artist's frame and knowledge. If the question is unrelated to \`${slug}\` , still answer in the principal's voice, but prefer \`${slug}\` -related context.`)

  if (sonic) {
    sections.push('')
    sections.push('## Sonic Profile')
    sections.push(sonic)
  }
  if (successes) {
    sections.push('')
    sections.push('## Successes (what worked)')
    sections.push(successes)
  }
  if (failures) {
    sections.push('')
    sections.push('## Failures (what did not work — avoid)')
    sections.push(failures)
  }
  if (audience) {
    sections.push('')
    sections.push('## Audience (listener profile)')
    sections.push(audience)
  }

  // If all 4 files are empty placeholders, return a friendly note rather
  // than fabricating context. The detect step still ran — so the agent knows
  // the user @-mentioned someone, but it should be transparent about the
  // empty profile.
  if (!sonic && !successes && !failures && !audience) {
    sections.push('')
    sections.push('(The artist 4-file memory set is are still blank templates. Tell the user honestly: "I have no accumulated knowledge of ' + slug + ' yet — feed the sonic-profile some data first.")')
  }

  return sections.join('\n')
}
