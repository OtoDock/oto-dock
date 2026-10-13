// The chat navigates that rebuild the path keep the overlay's params and
// nothing else of the current query.
import { describe, it, expect } from 'vitest'
import { withWorkspaceParams } from '@/lib/workspaceParams'

describe('withWorkspaceParams', () => {
  it('keeps an open overlay and its preview', () => {
    expect(withWorkspaceParams('/chat/a/c1', '?ws=1')).toBe('/chat/a/c1?ws=1')
    expect(withWorkspaceParams('/chat/a/c1', '?ws=1&ws_preview=users%2Fu%2Fworkspace%2Fx.md'))
      .toBe('/chat/a/c1?ws=1&ws_preview=users%2Fu%2Fworkspace%2Fx.md')
  })

  it('adds nothing when the overlay is closed and drops other params', () => {
    expect(withWorkspaceParams('/chat/a/c1', '')).toBe('/chat/a/c1')
    expect(withWorkspaceParams('/chat/a/c1', '?q=hello')).toBe('/chat/a/c1')
    expect(withWorkspaceParams('/chat/a/c1', '?q=hello&ws=1')).toBe('/chat/a/c1?ws=1')
  })
})
