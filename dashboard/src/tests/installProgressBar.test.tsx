import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import InstallProgressBar from '@/components/chat/InstallProgressBar'
import { useInstallStore } from '@/store/installStore'

// The done-grace strip ("Install complete.") follows real installs only: a
// session start whose workspace sync moved files reports a "workspace files"
// row into the same lifecycle, and that alone ends with no strip.

const M = 'machine-1'
const A = 'agent-1'

function finishWith(mcps: string[], stillInstalling?: string[]) {
  const s = useInstallStore.getState()
  s.begin({ machine_id: M, agent: A })
  const installs = mcps.filter((m) => m !== 'workspace files')
  if (installs.length) s.setPlan({ machine_id: M, agent: A, mcps_to_install: installs })
  for (const mcp of mcps) {
    if (stillInstalling?.includes(mcp)) continue
    s.recordProgress({ machine_id: M, agent: A, mcp, phase: 'done', pct: 100, message: '' })
  }
  s.finish({ machine_id: M, agent: A, still_installing: stillInstalling })
}

beforeEach(() => {
  vi.useFakeTimers()
  useInstallStore.setState({ byKey: {} })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('InstallProgressBar done-grace strip', () => {
  it('stays hidden after a workspace sync alone', () => {
    render(<InstallProgressBar chatId={null} machineId={M} agent={A} />)
    act(() => finishWith(['workspace files']))
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.queryByText('Install complete.')).toBeNull()
  })

  it('goes without "Install complete." when the session started with tools still installing', () => {
    render(<InstallProgressBar chatId={null} machineId={M} agent={A} />)
    act(() => finishWith(['image-gen-mcp', 'blender'], ['blender']))
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.queryByText('Install complete.')).toBeNull()
    expect(screen.queryByText('100%')).toBeNull()
    expect(useInstallStore.getState().byKey[`${M}::${A}`]?.stillInstalling).toEqual(['blender'])
  })

  it('shows after an install, with or without a workspace sync', () => {
    render(<InstallProgressBar chatId={null} machineId={M} agent={A} />)
    act(() => finishWith(['workspace files', 'image-gen-mcp']))
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.getByText('Install complete.')).toBeTruthy()
  })
})
