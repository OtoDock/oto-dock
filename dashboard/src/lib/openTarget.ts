/**
 * Navigation targets an app may ask the host to open (`otodock.open`). The
 * page names a KIND and an id; the host builds the route — never a URL from
 * the page, so generated content can only send the viewer where the
 * platform itself would. The destination page enforces its own access,
 * exactly as for a typed URL.
 */

export const OPEN_TARGET_KINDS = [
  'chat', 'run', 'app', 'agent', 'agent_settings', 'user_settings', 'file',
] as const
export type OpenTargetKind = (typeof OPEN_TARGET_KINDS)[number]

export const AGENT_SETTINGS_TABS = [
  'scheduled-tasks', 'triggers', 'notifications', 'meetings', 'conversations',
  'config', 'mcps', 'skills',
] as const
export const USER_SETTINGS_TABS = [
  'general', 'integrations', 'remote-machines', 'ai-engines', 'audio', 'usage',
] as const

/** Kinds that leave the agent's own surfaces for a settings page: they get
 * the first-use consent chip on top of the gates every kind passes. */
export const SETTINGS_KINDS: ReadonlySet<string> = new Set(['agent_settings', 'user_settings'])

// Ids are opaque tokens the platform minted (uuids, `task-<uuid>` chat ids,
// agent slugs); the shapes below bound them without pretending to validate.
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

export type OpenTargetRoute = { kind: OpenTargetKind; path: string; label: string }

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/**
 * Resolve a page-supplied target into a route. `agent` is the app's own
 * agent: the default for chat and file targets, which have no agent of
 * their own.
 */
export function buildOpenTarget(
  raw: unknown,
  agent: string,
): OpenTargetRoute | { error: string } {
  if (!raw || typeof raw !== 'object') return { error: 'target must be an object' }
  const t = raw as Record<string, unknown>
  const kindStr = str(t.kind)
  if (!(OPEN_TARGET_KINDS as readonly string[]).includes(kindStr)) {
    return { error: `unknown target kind ${JSON.stringify(kindStr)}` }
  }
  const kind = kindStr as OpenTargetKind
  const targetAgent = str(t.agent) || agent
  if (!SLUG_RE.test(targetAgent)) return { error: 'invalid agent' }

  switch (kind) {
    case 'chat': {
      const id = str(t.id)
      if (!ID_RE.test(id)) return { error: 'invalid chat id' }
      return { kind, path: `/chat/${targetAgent}/${id}`, label: 'a chat' }
    }
    case 'run': {
      const id = str(t.id)
      if (!ID_RE.test(id)) return { error: 'invalid run id' }
      return { kind, path: `/runs/${id}`, label: 'a task run' }
    }
    case 'app': {
      const id = str(t.id)
      if (!ID_RE.test(id)) return { error: 'invalid app id' }
      return { kind, path: `/apps/${id}`, label: 'an app' }
    }
    case 'agent':
      return { kind, path: `/agents/${targetAgent}`, label: 'an agent page' }
    case 'agent_settings': {
      const tab = str(t.tab)
      if (!(AGENT_SETTINGS_TABS as readonly string[]).includes(tab)) {
        return { error: 'invalid agent settings tab' }
      }
      return { kind, path: `/agents/${targetAgent}/${tab}`, label: `the agent's ${tab.replace('-', ' ')}` }
    }
    case 'user_settings': {
      const tab = str(t.tab)
      if (!(USER_SETTINGS_TABS as readonly string[]).includes(tab)) {
        return { error: 'invalid user settings tab' }
      }
      const params = new URLSearchParams({ tab })
      const provider = str(t.provider)
      if (provider) {
        if (!SLUG_RE.test(provider)) return { error: 'invalid provider' }
        params.set('provider', provider)
      }
      return { kind, path: `/user-settings?${params.toString()}`, label: `your ${tab.replace('-', ' ')} settings` }
    }
    case 'file': {
      const rel = str(t.path)
      if (!rel || rel.length > 512 || rel.startsWith('/') || rel.includes('\\')
          || rel.split('/').some((seg) => seg === '..' || seg === '')) {
        return { error: 'invalid file path' }
      }
      const params = new URLSearchParams({ ws: '1', ws_preview: rel })
      return { kind, path: `/chat/${targetAgent}?${params.toString()}`, label: 'a workspace file' }
    }
  }
  return { error: 'unknown target kind' }
}
