import { describe, it, expect } from 'vitest'
import { buildOpenTarget, SETTINGS_KINDS } from '@/lib/openTarget'

// The route builder behind otodock.open: kinds map to the platform's own
// routes, ids are shape-checked, and nothing a page supplies ever reaches
// the router as a URL.
describe('buildOpenTarget', () => {
  const ok = (t: unknown) => {
    const r = buildOpenTarget(t, 'dev')
    if ('error' in r) throw new Error(r.error)
    return r
  }
  const err = (t: unknown) => {
    const r = buildOpenTarget(t, 'dev')
    return 'error' in r ? r.error : `built ${r.path}`
  }

  it('builds every kind from the app agent by default', () => {
    expect(ok({ kind: 'chat', id: 'abc-123' }).path).toBe('/chat/dev/abc-123')
    expect(ok({ kind: 'chat', id: 'task-9f', agent: 'ops' }).path).toBe('/chat/ops/task-9f')
    expect(ok({ kind: 'run', id: 'run-1' }).path).toBe('/runs/run-1')
    expect(ok({ kind: 'app', id: 'a1' }).path).toBe('/apps/a1')
    expect(ok({ kind: 'agent' }).path).toBe('/agents/dev')
    expect(ok({ kind: 'agent', agent: 'ceo' }).path).toBe('/agents/ceo')
    expect(ok({ kind: 'agent_settings', tab: 'triggers' }).path).toBe('/agents/dev/triggers')
    expect(ok({ kind: 'user_settings', tab: 'usage' }).path).toBe('/user-settings?tab=usage')
    expect(ok({ kind: 'user_settings', tab: 'integrations', provider: 'gmail-mcp' }).path)
      .toBe('/user-settings?tab=integrations&provider=gmail-mcp')
    expect(ok({ kind: 'file', path: 'reports/q3 review.md' }).path)
      .toBe('/chat/dev?ws=1&ws_preview=reports%2Fq3+review.md')
  })

  it('refuses unknown kinds, raw URLs and malformed ids', () => {
    expect(err(null)).toContain('object')
    expect(err('chat')).toContain('object')
    expect(err({ kind: 'url', url: 'https://x' })).toContain('unknown target kind')
    expect(err({ kind: 'chat' })).toBe('invalid chat id')
    expect(err({ kind: 'chat', id: '../admin' })).toBe('invalid chat id')
    expect(err({ kind: 'chat', id: 'a'.repeat(81) })).toBe('invalid chat id')
    expect(err({ kind: 'chat', id: 'x', agent: 'Not A Slug' })).toBe('invalid agent')
    expect(err({ kind: 'agent_settings', tab: 'billing' })).toBe('invalid agent settings tab')
    expect(err({ kind: 'user_settings', tab: 'admin' })).toBe('invalid user settings tab')
    expect(err({ kind: 'user_settings', tab: 'integrations', provider: 'http://x' })).toBe('invalid provider')
    expect(err({ kind: 'file', path: '/etc/passwd' })).toBe('invalid file path')
    expect(err({ kind: 'file', path: 'a/../b' })).toBe('invalid file path')
    expect(err({ kind: 'file', path: 'a\\b' })).toBe('invalid file path')
    expect(err({ kind: 'file', path: '' })).toBe('invalid file path')
  })

  it('names the settings kinds that get the first-use chip', () => {
    expect([...SETTINGS_KINDS].sort()).toEqual(['agent_settings', 'user_settings'])
  })
})
