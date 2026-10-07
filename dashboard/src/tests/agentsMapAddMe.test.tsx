import { describe, it, expect, vi } from 'vitest'
import { buildAgentActions } from '@/components/agents-map/mapActions'

// ─── "Add me to this agent" on the agents map: a Shared-only agent takes
//     editor or above, so the admin joins as editor there, and as editor when
//     the map does not know the agent's mode; viewer only on an agent known
//     to have personal chats. ───────────────────────────────────────────────

function addMeRole(sharedOnly: boolean | undefined): string {
  const addMe = { mutate: vi.fn() }
  const actions = buildAgentActions({
    popup: { node: { slug: 'site', displayName: 'Site', grayed: true, sharedOnly } as any, x: 0, y: 0 },
    user: { sub: 'admin', role: 'admin', agent_roles: {} } as any,
    navigate: vi.fn() as any, setDefault: {} as any, refreshUser: vi.fn(async () => {}),
    popupPartners: [], nodeBySlug: new Map(), attemptUnlink: vi.fn(async () => {}),
    canCreateDepartments: true, setLinkFrom: vi.fn(), setMoveFrom: vi.fn(),
    updateAgent: {} as any, setNotice: vi.fn(), popupDept: undefined, qc: {} as any,
    addMe: addMe as any,
  })
  const action = actions.find((a) => a.key === 'add-me')!
  action.onClick()
  return addMe.mutate.mock.calls[0][0].role
}

describe('the map\'s "Add me"', () => {
  it('joins a Shared-only agent, or one of unknown mode, as editor', () => {
    expect(addMeRole(true)).toBe('editor')
    expect(addMeRole(undefined)).toBe('editor')
    expect(addMeRole(false)).toBe('viewer')
  })
})
