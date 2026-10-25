// The satellite update banner's elapsed counter starts at 0 and counts up by
// the second; it never shows a negative number in the first second.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import MachineUpdateBanner from '@/components/chat/MachineUpdateBanner'
import { useMachineUpdateStore } from '@/store/machineUpdateStore'

beforeEach(() => {
  vi.useFakeTimers()
  useMachineUpdateStore.setState({ byMachine: {} })
})
afterEach(() => { vi.useRealTimers() })

describe('MachineUpdateBanner', () => {
  it('reads 0s in the first second and counts from there', () => {
    act(() => useMachineUpdateStore.getState().beginUpdate({ machine_id: 'm1', machine_name: 'lab', to_version: '0.5.149' }))
    render(<MachineUpdateBanner machineId="m1" />)
    expect(screen.getByText('0s')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(2050) })
    expect(screen.getByText('2s')).toBeInTheDocument()
  })
})
