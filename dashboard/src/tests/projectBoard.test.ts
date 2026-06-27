import { describe, it, expect } from 'vitest'

import { parseBoard } from '@/components/projects/projectBoard'

const BOARD = `# Site redesign
Goal: Ship the new marketing site    Status: running

## Lanes
- [ ] Landing page — 2f1d8355-76a2-4bfe-837e-431144dadb81 — running
- [x] Design tokens — tokens lane — done
- [ ] Docs sweep

## Decisions
- Tailwind 4, no CSS modules

## Hand-offs
- tokens lane produced palette.json, landing consumes it
`

describe('parseBoard', () => {
  it('parses title, goal and status from the header', () => {
    const b = parseBoard(BOARD)
    expect(b.title).toBe('Site redesign')
    expect(b.goal).toBe('Ship the new marketing site')
    expect(b.status).toBe('running')
  })

  it('parses lanes with ref, status and done flag', () => {
    const b = parseBoard(BOARD)
    expect(b.lanes).toHaveLength(3)
    expect(b.lanes[0]).toEqual({
      name: 'Landing page',
      ref: '2f1d8355-76a2-4bfe-837e-431144dadb81',
      status: 'running',
      done: false,
    })
    expect(b.lanes[1].done).toBe(true)
    expect(b.lanes[2]).toEqual({ name: 'Docs sweep', ref: '', status: '', done: false })
  })

  it('parses decisions and hand-offs', () => {
    const b = parseBoard(BOARD)
    expect(b.decisions).toEqual(['Tailwind 4, no CSS modules'])
    expect(b.handoffs).toHaveLength(1)
  })

  it('is forgiving on empty / malformed input', () => {
    expect(parseBoard('')).toEqual({
      title: '', goal: '', status: '', lanes: [], decisions: [], handoffs: [],
    })
    expect(parseBoard('random text\n- not a lane').lanes).toEqual([])
  })
})
