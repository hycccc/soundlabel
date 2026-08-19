// heat-modifier — turns a 0-100 "heat" dial into a tone-modifying
// system-prompt suffix. The dial is set client-side (localStorage) and
// passed via X-Ops-Heat header. Out-of-range values clamp to 50.
//
// Tone bands:
//   0-19   ice — pure facts, no editorial, like git log lines
//   20-39  cool — concise, fewer adjectives
//   40-59  default — matches the base persona voice
//   60-79  warm — opinionated, willing to push back
//   80-100 spicy — bluntly critical, leans acerbic

const BANDS = [
  {
    max: 19,
    label: 'ice',
    suffix: `# Heat = ICE\n\nOverride for this turn:\n- Pure facts, zero affect.\n- Short sentences, git-log style.\n- No opinions, description only.`,
  },
  {
    max: 39,
    label: 'cool',
    suffix: `# Heat = COOL\n\nOverride for this turn:\n- More restrained than usual.\n- Cut adjectives, no banter.\n- State opinions without elaborating.`,
  },
  {
    max: 59,
    label: 'default',
    suffix: '',
  },
  {
    max: 79,
    label: 'warm',
    suffix: `# Heat = WARM\n\nOverride for this turn:\n- Blunter than usual; call risks without softening.\n- If the user's direction is wrong, say so and give an alternative.\n- Occasional wit.`,
  },
  {
    max: 100,
    label: 'spicy',
    suffix: `# Heat = SPICY\n\nOverride for this turn:\n- Acerbic. Say the sharpest true thing, unfiltered.\n- A dumb idea can be called "a dumb idea" — with reasons.\n- Never ad hominem: critique the work and decisions, not the person.`,
  },
]

export function heatModifier(rawHeat) {
  let n = parseInt(String(rawHeat ?? '50'), 10)
  if (!Number.isFinite(n)) n = 50
  if (n < 0) n = 0
  if (n > 100) n = 100
  const band = BANDS.find((b) => n <= b.max) ?? BANDS[2]
  return { heat: n, band: band.label, suffix: band.suffix }
}
