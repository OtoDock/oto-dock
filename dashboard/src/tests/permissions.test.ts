import { describe, it, expect } from 'vitest'
import {
  AGENT_ROLES, CREATOR_TIER, EDITOR_TIER, EFFECTIVE_ROLES, OWNER_TIER, PLATFORM_BY_RANK, PLATFORM_RANK,
  PLATFORM_ROLES, ROLE, ROLE_RANK, WORKSPACE_TIER, actingRole, canEditAgent, canManageAgent, canWriteWorkspace,
  isAdmin, isCreatorOrAbove, meetsFloor, rank, roleBadge, roleLabel,
} from '@/lib/permissions'
import type { User } from '@/api/auth'

// The dashboard's read of the role vocabulary — the mirror of the proxy's
// auth/roles.py (the lock-step test on the proxy side pins the spellings;
// this pins the questions).
const user = (role: User['role'], agent_roles: User['agent_roles'] = {}): User => ({
  sub: 'u', email: 'u@x', name: 'U', role, agents: Object.keys(agent_roles), agent_roles,
})

describe('lib/permissions', () => {
  it('spells the members, the tiers and the ranks once', () => {
    expect(Object.values(ROLE).sort()).toEqual([...PLATFORM_ROLES, ...AGENT_ROLES].sort())
    expect(EFFECTIVE_ROLES).toEqual(['viewer', 'contributor', 'editor', 'manager', 'admin'])
    expect(PLATFORM_BY_RANK).toEqual(['member', 'creator', 'admin'])
    expect(OWNER_TIER).toEqual(['manager', 'admin'])
    expect(EDITOR_TIER).toEqual(['manager', 'editor', 'admin'])
    expect(WORKSPACE_TIER).toEqual(['manager', 'editor', 'contributor', 'admin'])
    expect(CREATOR_TIER).toEqual(['admin', 'creator'])
    expect(ROLE_RANK).toEqual({ viewer: 0, contributor: 1, editor: 2, manager: 3, admin: 4 })
    expect(PLATFORM_RANK).toEqual({ member: 0, creator: 1, admin: 2 })
  })

  it('answers the platform questions for a user or a bare word', () => {
    expect(isAdmin(user('admin'))).toBe(true)
    expect(isAdmin(user('creator'))).toBe(false)
    expect(isAdmin('admin')).toBe(true)
    expect(isAdmin(null)).toBe(false)
    expect(isAdmin(undefined)).toBe(false)
    expect(isCreatorOrAbove(user('creator'))).toBe(true)
    expect(isCreatorOrAbove(user('member'))).toBe(false)
    expect(isCreatorOrAbove('admin')).toBe(true)
    expect(isCreatorOrAbove(null)).toBe(false)
  })

  it('resolves the role a principal acts with: the admin, the row, else viewer', () => {
    expect(actingRole(user('admin'), 'a')).toBe(ROLE.ADMIN)
    expect(actingRole(user('admin', { a: 'viewer' }), 'a')).toBe(ROLE.ADMIN)
    expect(actingRole(user('member', { a: 'editor' }), 'a')).toBe(ROLE.EDITOR)
    expect(actingRole(user('member', { a: 'editor' }), 'b')).toBe(ROLE.VIEWER)
    expect(actingRole(null, 'a')).toBe(ROLE.VIEWER)
    expect(canManageAgent(user('member', { a: 'manager' }), 'a')).toBe(true)
    expect(canManageAgent(user('member', { a: 'editor' }), 'a')).toBe(false)
    expect(canManageAgent(user('admin'), 'zzz')).toBe(true)
    expect(canEditAgent(user('member', { a: 'editor' }), 'a')).toBe(true)
    expect(canEditAgent(user('member', { a: 'viewer' }), 'a')).toBe(false)
    expect(canEditAgent(user('member', { a: 'contributor' }), 'a')).toBe(false)
    expect(canEditAgent(null, 'a')).toBe(false)
    expect(canWriteWorkspace(user('member', { a: 'contributor' }), 'a')).toBe(true)
    expect(canWriteWorkspace(user('member', { a: 'editor' }), 'a')).toBe(true)
    expect(canWriteWorkspace(user('member', { a: 'viewer' }), 'a')).toBe(false)
    expect(canWriteWorkspace(user('member', { a: 'contributor' }), 'b')).toBe(false)
    expect(canWriteWorkspace(null, 'a')).toBe(false)
  })

  it('judges an action floor by rank; an unknown word ranks 0', () => {
    expect(rank('none')).toBe(0)
    expect(rank(undefined)).toBe(0)
    expect(meetsFloor({}, 'viewer')).toBe(true)
    expect(meetsFloor({ min_role: '' }, 'none')).toBe(true)
    expect(meetsFloor({ min_role: 'editor' }, 'viewer')).toBe(false)
    expect(meetsFloor({ min_role: 'editor' }, 'contributor')).toBe(false)
    expect(meetsFloor({ min_role: 'contributor' }, 'contributor')).toBe(true)
    expect(meetsFloor({ min_role: 'contributor' }, 'viewer')).toBe(false)
    expect(meetsFloor({ min_role: 'editor' }, 'editor')).toBe(true)
    expect(meetsFloor({ min_role: 'manager' }, 'editor')).toBe(false)
    expect(meetsFloor({ min_role: 'manager' }, 'admin')).toBe(true)
    expect(meetsFloor({ min_role: 'editor' }, undefined)).toBe(false)
    expect(meetsFloor({ min_role: 'owner' }, 'admin')).toBe(false) // an unknown floor is unreachable
  })

  it('wears one badge palette and one label per role', () => {
    expect(roleBadge('admin')).toContain('red')
    expect(roleBadge('editor')).toContain('amber')
    expect(roleBadge('contributor')).toContain('teal')
    expect(roleBadge('nobody')).toBe(roleBadge('viewer'))
    expect(roleLabel('manager')).toBe('Manager')
    expect(roleLabel('contributor')).toBe('Contributor')
    expect(roleLabel(undefined)).toBe('Viewer')
  })
})
