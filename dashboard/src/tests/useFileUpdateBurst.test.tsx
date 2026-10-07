/**
 * A burst of `file_updated` frames for one agent runs the refetch once, after
 * it settles; frames for another agent are ignored; an unmount cancels the
 * pending refetch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useFileUpdateBurst } from '@/hooks/useFileUpdateBurst'
import { emitFileUpdate } from '@/lib/fileUpdates'

const frame = (agent: string, path: string) =>
  ({ type: 'file_updated', agent_slug: agent, rel_path: path, file_id: '', source: 'disk' }) as any

describe('useFileUpdateBurst', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('runs once per burst, after it settles', () => {
    const onBurst = vi.fn()
    renderHook(() => useFileUpdateBurst('agent-a', onBurst, 300))
    for (let i = 0; i < 20; i++) {
      emitFileUpdate(frame('agent-a', `workspace/f${i}.md`))
      vi.advanceTimersByTime(50)
    }
    expect(onBurst).not.toHaveBeenCalled()
    vi.advanceTimersByTime(300)
    expect(onBurst).toHaveBeenCalledTimes(1)
    emitFileUpdate(frame('agent-a', 'workspace/late.md'))
    vi.advanceTimersByTime(300)
    expect(onBurst).toHaveBeenCalledTimes(2)
  })

  it('ignores other agents and cancels on unmount', () => {
    const onBurst = vi.fn()
    const { unmount } = renderHook(() => useFileUpdateBurst('agent-a', onBurst, 300))
    emitFileUpdate(frame('agent-b', 'workspace/x.md'))
    vi.advanceTimersByTime(400)
    expect(onBurst).not.toHaveBeenCalled()
    emitFileUpdate(frame('agent-a', 'workspace/y.md'))
    unmount()
    vi.advanceTimersByTime(400)
    expect(onBurst).not.toHaveBeenCalled()
  })
})
