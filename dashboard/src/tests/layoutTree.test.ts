import { describe, expect, it } from 'vitest'
import { FILE_HEADS, HEAD, HEADS, USER_SUBDIRS, headOf, isPersonal, scopeWorkspace, userOf } from '../lib/layout/tree'
import { BOOTSTRAP_OS, DISPLAY_SERVER, SATELLITE_OS } from '../lib/hostOs/os'

describe('the agent tree mirror', () => {
  it('spells the four heads and the two user subdirs', () => {
    expect(HEADS).toEqual(['users', 'workspace', 'knowledge', 'config'])
    expect(FILE_HEADS).toEqual(['workspace', 'knowledge', 'users'])
    expect(USER_SUBDIRS).toEqual(['workspace', 'context'])
    expect(HEAD.CONTEXT).toBe('context')
  })

  it('answers the head of an agent-relative path (a bare users is a head, a leading slash is not stripped)', () => {
    expect(headOf('users/alice/workspace/a.md')).toBe('users')
    expect(headOf('users')).toBe('users')
    expect(headOf('workspace')).toBe('workspace')
    expect(headOf('config/agent.md')).toBe('config')
    expect(headOf('usersx/a')).toBe('')
    expect(headOf('/users/a')).toBe('')
    expect(headOf('')).toBe('')
  })

  it('tells a personal path from a shared one (a bare users is not personal)', () => {
    expect(isPersonal('users/alice')).toBe(true)
    expect(isPersonal('users/')).toBe(true)
    expect(isPersonal('users')).toBe(false)
    expect(isPersonal('workspace/a')).toBe(false)
  })

  it('names the person whose tree a path lies in', () => {
    expect(userOf('users/alice/workspace')).toBe('alice')
    expect(userOf('/users/alice')).toBe('alice')
    expect(userOf('users')).toBe('')
    expect(userOf('workspace/x')).toBe('')
  })

  it('composes the default save folder', () => {
    expect(scopeWorkspace('alice')).toBe('users/alice/workspace')
    expect(scopeWorkspace('')).toBe('workspace')
  })
})

describe('the host OS mirror', () => {
  it('spells the three vocabularies', () => {
    expect(SATELLITE_OS).toEqual(['linux', 'darwin', 'windows'])
    expect(BOOTSTRAP_OS).toEqual(['linux', 'macos', 'windows'])
    expect(DISPLAY_SERVER).toEqual(['x11', 'wayland', 'quartz', 'windows', 'none'])
  })
})
