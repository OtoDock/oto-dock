import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import InstallProgressBar from '@/components/chat/InstallProgressBar'
import { useInstallStore } from '@/store/installStore'
import { useChatStore } from '@/store/chatStore'

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

describe('InstallProgressBar shows the install of the computer the chat runs on', () => {
  const OTHER = 'machine-2'

  function installing(machine: string) {
    const s = useInstallStore.getState()
    s.begin({ machine_id: machine, agent: A })
    s.setPlan({ machine_id: machine, agent: A, mcps_to_install: ['blender'] })
    s.recordProgress({ machine_id: machine, agent: A, mcp: 'blender', phase: 'installing', pct: 40, message: '' })
  }

  function chatOn(chatId: string, target: string) {
    useChatStore.getState().beginWarmup(chatId, { agent: A, execution_target: target })
  }

  it("never shows another computer's install in a chat on a known computer", () => {
    chatOn('chat-a', M)
    installing(OTHER)
    render(<InstallProgressBar chatId="chat-a" machineId={OTHER} agent={A} />)
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.queryByText(/Installing/)).toBeNull()
  })

  it('shows nothing in a chat on the platform while a computer installs', () => {
    chatOn('chat-p', 'local')
    installing(OTHER)
    render(<InstallProgressBar chatId="chat-p" machineId={null} agent={A} />)
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.queryByText(/Installing/)).toBeNull()
  })

  it("shows the chat's own computer's install", () => {
    chatOn('chat-a', M)
    installing(M)
    render(<InstallProgressBar chatId="chat-a" machineId={null} agent={A} />)
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.getByText('Installing blender…')).toBeTruthy()
  })

  it("shows the agent's install while the chat's computer is still unknown", () => {
    chatOn('chat-new', '')
    installing(OTHER)
    render(<InstallProgressBar chatId="chat-new" machineId={null} agent={A} />)
    act(() => { vi.advanceTimersByTime(600) })
    expect(screen.getByText('Installing blender…')).toBeTruthy()
  })

  it('names the workspace copy and its size', () => {
    chatOn('chat-a', M)
    const s = useInstallStore.getState()
    s.begin({ machine_id: M, agent: A })
    s.recordProgress({ machine_id: M, agent: A, mcp: 'workspace files', phase: 'syncing', pct: 10, message: '830/8300 files', files_total: 8300 })
    render(<InstallProgressBar chatId="chat-a" machineId={null} agent={A} />)
    act(() => { vi.advanceTimersByTime(600) })
    act(() => { vi.advanceTimersByTime(31_000) })
    expect(screen.getByText('Syncing workspace files…')).toBeTruthy()
    expect(document.body.textContent).toContain("Copying 8,300 workspace files to this computer.")
  })
})
