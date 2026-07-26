// Phase G helpers (1.4.0): Explorer-style type-ahead matching and the
// per-section recursive search (flat matches + pruned tree).

import { describe, it, expect } from 'vitest'

import {
  emptyTypeAhead, pushChar, findMatch, TYPE_AHEAD_RESET_MS,
} from '../lib/typeAhead'
import {
  searchSection, pruneTree, expandedDirsOf, SEARCH_RESULT_CAP,
} from '../lib/workspaceSearch'
import type { FileNode } from '../api/agents'

describe('typeAhead', () => {
  const names = ['alpha.md', 'beta.txt', 'test.mp4', 'testing.md', 'toast.png']

  it('prefix-matches an accumulating buffer', () => {
    let st = emptyTypeAhead()
    st = pushChar(st, 't', 1000)
    expect(findMatch(names, st, -1)).toBe(2) // test.mp4
    st = pushChar(st, 'e', 1200)
    expect(findMatch(names, st, 2)).toBe(2) // still test.mp4 ("te")
    st = pushChar(st, 's', 1400)
    st = pushChar(st, 't', 1500)
    st = pushChar(st, 'i', 1600)
    expect(findMatch(names, st, 2)).toBe(3) // testing.md
  })

  it('is case-insensitive', () => {
    let st = emptyTypeAhead()
    st = pushChar(st, 'B', 1000)
    expect(findMatch(names, st, -1)).toBe(1)
  })

  it('resets the buffer after the inactivity window', () => {
    let st = pushChar(emptyTypeAhead(), 't', 1000)
    st = pushChar(st, 'o', 1000 + TYPE_AHEAD_RESET_MS + 1)
    // buffer restarted at "o" → no match ("o" prefixes nothing)... except none
    expect(findMatch(names, st, -1)).toBe(-1)
  })

  it('repeated single char cycles through matches with wrap', () => {
    let st = pushChar(emptyTypeAhead(), 't', 1000)
    const first = findMatch(names, st, -1)
    expect(first).toBe(2)
    st = pushChar(st, 't', 1100)
    expect(findMatch(names, st, first)).toBe(3) // testing.md
    st = pushChar(st, 't', 1200)
    expect(findMatch(names, st, 3)).toBe(4)     // toast.png
    st = pushChar(st, 't', 1300)
    expect(findMatch(names, st, 4)).toBe(2)     // wraps back to test.mp4
  })

  it('no match returns -1', () => {
    const st = pushChar(emptyTypeAhead(), 'z', 1000)
    expect(findMatch(names, st, -1)).toBe(-1)
  })
})

const f = (path: string): FileNode => ({
  name: path.split('/').pop()!, path, type: 'file', size: 1,
  modified: '2026-01-01T00:00:00Z',
})
const d = (path: string, children: FileNode[]): FileNode => ({
  name: path.split('/').pop()!, path, type: 'dir', size: 0,
  modified: '2026-01-01T00:00:00Z', children,
})

const ROOTS: FileNode[] = [
  d('workspace/projects', [
    d('workspace/projects/video', [
      f('workspace/projects/video/report-final.mp4'),
      f('workspace/projects/video/notes.md'),
    ]),
    f('workspace/projects/report-draft.docx'),
  ]),
  f('workspace/report.pdf'),
  f('workspace/readme.md'),
]

describe('searchSection', () => {
  it('finds matches recursively with section-relative parents', () => {
    const { matches, truncated } = searchSection(ROOTS, 'workspace', 'report')
    expect(truncated).toBe(false)
    const got = matches.map((m) => [m.node.name, m.parentRel])
    expect(got).toEqual([
      ['report-final.mp4', 'projects/video'],
      ['report-draft.docx', 'projects'],
      ['report.pdf', ''],
    ])
  })

  it('matches folders too and is case-insensitive', () => {
    const { matches } = searchSection(ROOTS, 'workspace', 'VIDEO')
    expect(matches.map((m) => m.node.path)).toEqual(['workspace/projects/video'])
  })

  it('empty query yields nothing', () => {
    expect(searchSection(ROOTS, 'workspace', '   ').matches).toEqual([])
  })

  it('caps results and reports truncation', () => {
    const many: FileNode[] = Array.from({ length: SEARCH_RESULT_CAP + 20 },
      (_, i) => f(`workspace/file-${i}.txt`))
    const { matches, truncated } = searchSection(many, 'workspace', 'file')
    expect(matches).toHaveLength(SEARCH_RESULT_CAP)
    expect(truncated).toBe(true)
  })
})

describe('pruneTree', () => {
  it('keeps matches plus ancestor folders only', () => {
    const pruned = pruneTree(ROOTS, 'notes')
    expect(pruned.map((n) => n.path)).toEqual(['workspace/projects'])
    const projects = pruned[0]
    expect(projects.children!.map((n) => n.path)).toEqual(['workspace/projects/video'])
    expect(projects.children![0].children!.map((n) => n.path)).toEqual([
      'workspace/projects/video/notes.md',
    ])
  })

  it('a matched folder keeps its full subtree', () => {
    const pruned = pruneTree(ROOTS, 'video')
    const video = pruned[0].children![0]
    expect(video.children!.map((n) => n.name)).toEqual([
      'report-final.mp4', 'notes.md',
    ])
  })

  it('does not mutate the source tree', () => {
    const before = JSON.stringify(ROOTS)
    pruneTree(ROOTS, 'notes')
    expect(JSON.stringify(ROOTS)).toBe(before)
  })

  it('expandedDirsOf lists every folder of the pruned tree', () => {
    const pruned = pruneTree(ROOTS, 'notes')
    expect(expandedDirsOf(pruned)).toEqual(
      new Set(['workspace/projects', 'workspace/projects/video']),
    )
  })
})
