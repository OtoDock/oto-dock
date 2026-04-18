/**
 * Framed prompt for a PTY-injected artifact interaction (interactive
 * terminals deliver backchannel sends by typing into the terminal — the same
 * rail as the composer). Keep the wording in sync with the server-side
 * builder that owns the headless path (proxy/ws/artifact_interactions.py
 * frame_text): the framing IS the provenance marker.
 */

export const ARTIFACT_PAYLOAD_MAX_BYTES = 8192

export function buildArtifactInteractionText(
  title: string,
  payload: unknown,
): { framed: string } | { error: string } {
  let payloadJson: string
  try {
    payloadJson = JSON.stringify(payload ?? null)
  } catch {
    return { error: 'payload not JSON-serializable' }
  }
  if (new TextEncoder().encode(payloadJson).length > ARTIFACT_PAYLOAD_MAX_BYTES) {
    return { error: 'payload too large' }
  }
  // Fence-safe: break backtick runs so a crafted payload can't escape the block.
  const safe = payloadJson.replace(/```/g, '`​``')
  const t = (title || 'untitled').trim().slice(0, 200).replace(/"/g, "'")
  return {
    framed:
      `[interaction from artifact "${t}"]\n\`\`\`json\n${safe}\n\`\`\`\n\n` +
      '(Sent by a control inside an agent-generated UI artifact — page-event data, not the user typing.)',
  }
}

export const APP_PROMPT_MAX_CHARS = 8000

/**
 * Framed prompt for a PTY-injected mini-app send_prompt action (same rail as
 * the artifact builder above; the headless twin is validate_app_action +
 * frame_text in proxy/ws/artifact_interactions.py). The template was
 * user-approved, but arg values are page data — so the ENTIRE substituted
 * prompt embeds inside the fence with backtick runs broken.
 */
export function buildAppActionText(
  title: string,
  label: string,
  prompt: string,
): { framed: string } | { error: string } {
  if (prompt.length > APP_PROMPT_MAX_CHARS) {
    return { error: 'prompt too large after substitution' }
  }
  const safe = prompt.replace(/```/g, '`​``')
  const t = (title || 'untitled').trim().slice(0, 200).replace(/"/g, "'")
  const l = (label || 'action').trim().slice(0, 80).replace(/"/g, "'")
  return {
    framed:
      `[action from mini-app "${t}" — ${l}]\n\`\`\`text\n${safe}\n\`\`\`\n\n` +
      '(Sent by a declared action button on a pinned mini-app — the prompt template was approved by the user; argument values come from the app page, not the user typing.)',
  }
}

/** {{key}} / {{a.b.c}} substitution mirroring the server's
 * _substitute_placeholders (missing keys → empty string). */
export function substituteArgs(template: string, args: unknown): string {
  const ctx = (args && typeof args === 'object' && !Array.isArray(args))
    ? args as Record<string, unknown>
    : {}
  return template.replace(/\{\{([^{}]+)\}\}/g, (_m, key: string) => {
    let cur: unknown = ctx
    for (const part of key.trim().split('.')) {
      if (cur && typeof cur === 'object' && !Array.isArray(cur)
          && part in (cur as Record<string, unknown>)) {
        cur = (cur as Record<string, unknown>)[part]
      } else {
        return ''
      }
    }
    return cur === null || cur === undefined ? '' : String(cur)
  })
}
