import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import type { Department } from '@/api/departments'
import { useMapPointers } from '@/components/agents-map/useMapPointers'

// The map's move into a department: the server says, per department, whether
// this person may place an agent there. One they may not is refused with the
// server's own sentence, and nothing is sent.

const REFUSAL = 'The sentence the server sends.'

function dept(id: string, canPlace: boolean): Department {
  return {
    id, name: id, created_by_sub: '', mode: 'down', reach: 'adjacent', position_hint: '',
    levels: [{ id: `${id}-l1`, rank: 0, name: 'Head' }, { id: `${id}-l2`, rank: 1, name: 'Team' }],
    members: [], can_edit: false, can_place: canPlace, place_refusal: canPlace ? '' : REFUSAL,
  }
}

function setup(departments: Department[]) {
  const mutate = vi.fn()
  const setNotice = vi.fn()
  const setMoveFrom = vi.fn()
  const setLevelPick = vi.fn()
  const ref = <T,>(v: T) => ({ current: v })
  const { result } = renderHook(() => useMapPointers({
    bagRef: ref(null), containerRef: ref(null), nodeBySlug: new Map(), qc: new QueryClient(),
    setLinkFrom: vi.fn(), setNotice, setMoveFrom, setLevelPick,
    updateAgent: { mutate } as unknown as Parameters<typeof useMapPointers>[0]['updateAgent'],
    departments, moveFromRef: ref('alpha'), enterStage: vi.fn(), exitStageRef: ref(() => {}),
    pageDeltaRef: ref(() => {}), setMenu: vi.fn(), setPopup: vi.fn(),
  }))
  return { result, mutate, setNotice, setMoveFrom, setLevelPick }
}

describe('the map moves an agent only where the person may place it', () => {
  it('shows the server sentence and sends nothing for a department it may not place in', () => {
    const s = setup([dept('theirs', false)])
    s.result.current.beginMoveTo('alpha', 'theirs', 10, 10)
    expect(s.mutate).not.toHaveBeenCalled()
    expect(s.setLevelPick).not.toHaveBeenCalled()
    expect(s.setMoveFrom).toHaveBeenCalledWith(null)
    expect(s.setNotice).toHaveBeenCalledWith(REFUSAL)
  })

  it('asks for the level as before where it may', () => {
    const s = setup([dept('mine', true)])
    s.result.current.beginMoveTo('alpha', 'mine', 10, 10)
    expect(s.mutate).not.toHaveBeenCalled()
    expect(s.setLevelPick).toHaveBeenCalledWith({ slug: 'alpha', departmentId: 'mine', x: 10, y: 10 })
  })
})
