import { describe, it, expect } from 'vitest'

import { buildSections } from '@/components/workspace/sections'
import type { FileNode } from '@/api/agents'

// ─── buildSections users/ child selection ────────────────────────────────────
// Regression: "My Workspace" used to be users/children[0] — with an admin's
// unfiltered tree, another user's dir sorting first rendered as an EMPTY
// personal workspace. The section must belong to the logged-in username.

function dir(name: string, path: string, children: FileNode[] = []): FileNode {
  return { name, type: 'dir', path, size: 0, modified: '', children }
}

function file(name: string, path: string): FileNode {
  return { name, type: 'file', path, size: 1, modified: '' }
}

function tree(): FileNode[] {
  return [
    dir('users', 'users', [
      dir('alex', 'users/alex', [dir('workspace', 'users/alex/workspace')]),
      dir('jim', 'users/jim', [
        dir('workspace', 'users/jim/workspace', [file('notes.md', 'users/jim/workspace/notes.md')]),
        dir('context', 'users/jim/context'),
      ]),
    ]),
    dir('workspace', 'workspace'),
  ]
}

describe('buildSections users dir selection', () => {
  it('picks the users/ child matching the logged-in username', () => {
    const sections = buildSections(tree(), true, true, 'jim')
    const myWorkspace = sections.find((s) => s.key === 'my-workspace')
    expect(myWorkspace?.pathPrefix).toBe('users/jim/workspace')
    expect(myWorkspace?.nodes.map((n) => n.name)).toEqual(['notes.md'])
    expect(sections.find((s) => s.key === 'my-context')?.pathPrefix).toBe('users/jim/context')
  })

  it('falls back to children[0] when no username is supplied', () => {
    const sections = buildSections(tree(), true, true)
    expect(sections.find((s) => s.key === 'my-workspace')?.pathPrefix).toBe('users/alex/workspace')
  })

  it('falls back to children[0] when the username has no dir in the tree', () => {
    const sections = buildSections(tree(), true, true, 'nobody')
    expect(sections.find((s) => s.key === 'my-workspace')?.pathPrefix).toBe('users/alex/workspace')
  })

  it('ignores a FILE node that happens to share the username', () => {
    const t: FileNode[] = [
      dir('users', 'users', [
        file('jim', 'users/jim'),
        dir('jim2', 'users/jim2', [dir('workspace', 'users/jim2/workspace')]),
      ]),
    ]
    const sections = buildSections(t, true, true, 'jim')
    expect(sections.find((s) => s.key === 'my-workspace')?.pathPrefix).toBe('users/jim2/workspace')
  })
})
