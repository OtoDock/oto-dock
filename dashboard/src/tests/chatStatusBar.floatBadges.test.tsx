/**
 * On a phone the status badges float over the chat's bottom right corner,
 * in the slot `ChatBottomPanel` holds, so the chat and the terminal never
 * move when one comes or goes. Several pills, or the terminal under them,
 * take the compact form; a tap opens a pill's list upward. Where the pane
 * holds no chat the badges keep their row, and the desktop its inline pills.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ComponentProps } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'

import ChatStatusBar, { type ActiveAgent } from '@/components/chat/ChatStatusBar'
import ChatComposerBar, { ChatBottomPanel } from '@/pages/agent/chat/ChatComposerBar'
import { badgePlacement, type BadgePane } from '@/lib/chat/badgeFloat'

// The composer's own input and key bar are not what these tests read.
vi.mock('@/components/chat/ChatInput', () => ({ default: () => null }))
vi.mock('@/components/chat/terminal/TerminalControlBar', () => ({ default: () => null }))

const lane = (id: string, description: string): ActiveAgent => ({ id: `delegate-${id}`, description, type: 'delegate', startTime: 0 })
const command = (id: string): ActiveAgent => ({ id, description: 'sleep 150', type: 'bgcommand', startTime: 0, background: true })

function Bar({ agents, meeting = false, terminal = false, slot = null }: {
  agents: ActiveAgent[]; meeting?: boolean; terminal?: boolean; slot?: HTMLElement | null
}) {
  return (
    <ChatStatusBar
      streaming={false} warming={false} startTime={null} thinkingActive={false} compressingActive={false}
      activeAgents={agents} meetingActive={meeting} mode="ask" model="claude-sonnet-5" costUsd={0}
      contextUsed={0} contextMax={0} onModeChange={() => {}} onModelChange={() => {}}
      badgeFloat={slot} terminalShown={terminal}
    />
  )
}

/** The bar inside a bottom panel, handed the panel's own slot (the composer
 *  bar's part) unless `float` is false (a pane with no chat). */
function renderPanel({ float = true, ...props }: Omit<Parameters<typeof Bar>[0], 'slot'> & { float?: boolean }) {
  const view = render(<ChatBottomPanel><Bar {...props} /></ChatBottomPanel>)
  const slot = view.container.querySelector<HTMLElement>('[data-badge-float]')!
  view.rerender(<ChatBottomPanel><Bar {...props} slot={float ? slot : null} /></ChatBottomPanel>)
  return { ...view, slot }
}

describe('the floated status badges', () => {
  it('one pill over the history sits in the slot with its label, the row is gone', () => {
    const { slot, container } = renderPanel({ agents: [lane('dyn-1', 'long lane → worker')] })
    const pill = within(slot).getByTestId('float-badge-delegated')
    expect(pill.textContent).toBe('1 Delegated')
    expect(pill.getAttribute('aria-label')).toBe('1 Delegated')
    // Only the pills take a tap: the slot passes every other one through.
    expect(slot.className).toContain('pointer-events-none')
    expect(pill.parentElement!.className).toContain('pointer-events-auto')
    // The slot is phone-only and the bar keeps no badge row of its own.
    expect(slot.className).toContain('md:hidden')
    expect(container.querySelector('.gap-2.mb-1\\.5.md\\:hidden')).toBeNull()
  })

  it('several pills and the terminal take the compact form', () => {
    const agents = [lane('dyn-1', 'a'), lane('dyn-2', 'b'), command('tuB')]
    const { slot, unmount } = renderPanel({ agents })
    expect(within(slot).getByTestId('float-badge-delegated').textContent).toBe('2')
    expect(within(slot).getByTestId('float-badge-delegated').getAttribute('aria-label')).toBe('2 Delegated')
    expect(within(slot).getByTestId('float-badge-commands').textContent).toBe('1')
    unmount()
    const term = renderPanel({ agents: [lane('dyn-1', 'a')], terminal: true })
    expect(within(term.slot).getByTestId('float-badge-delegated').textContent).toBe('1')
  })

  it('a meeting floats too, compact beside another pill', () => {
    const { slot, unmount } = renderPanel({ agents: [], meeting: true })
    expect(within(slot).getByTestId('float-badge-meeting').textContent).toBe('Meeting')
    unmount()
    const both = renderPanel({ agents: [lane('dyn-1', 'a')], meeting: true })
    expect(within(both.slot).getByTestId('float-badge-meeting').textContent).toBe('')
    expect(within(both.slot).getByTestId('float-badge-meeting').getAttribute('aria-label')).toBe('Meeting')
  })

  it('a tap opens the list with every item, a second tap closes it', () => {
    const { slot } = renderPanel({ agents: [lane('dyn-1', 'long lane → worker'), lane('dyn-2', 'review → critic')] })
    const pill = within(slot).getByTestId('float-badge-delegated')
    act(() => { fireEvent.click(pill) })
    const list = screen.getByRole('menu', { name: '2 Delegated' })
    expect(within(list).getAllByRole('menuitem').map((i) => i.textContent)).toEqual(['long lane → worker', 'review → critic'])
    expect(pill.getAttribute('aria-expanded')).toBe('true')
    // The list is portaled, never inside the slot whose stacking it would take.
    expect(slot.contains(list)).toBe(false)
    act(() => { fireEvent.click(pill) })
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('where the pane holds no chat the badges keep their row', () => {
    const { slot, container } = renderPanel({ agents: [lane('dyn-1', 'a')], float: false })
    expect(slot.childElementCount).toBe(0)
    const row = container.querySelector('.gap-2.mb-1\\.5.md\\:hidden')
    expect(row?.textContent).toContain('1 Delegated')
  })

  it('the desktop keeps its inline pills with their title', () => {
    const { container } = renderPanel({ agents: [lane('dyn-1', 'long lane → worker')] })
    const inline = container.querySelector('.hidden.md\\:contents')!
    expect(inline.textContent).toBe('1 Delegated')
    expect(inline.querySelector('[title]')?.getAttribute('title')).toBe('long lane → worker')
  })
})

describe('where the composer puts the badges', () => {
  const pane = (over: Partial<BadgePane> = {}): BadgePane => ({
    workspaceOpen: false, appsActive: false, projectsActive: false, sessionInteractive: false, showRichView: false, ...over,
  })
  const slot = document.createElement('div')

  it('floats them over the chat and the terminal, never over another view', () => {
    expect(badgePlacement(slot, pane())).toEqual({ float: slot, compact: false })
    expect(badgePlacement(slot, pane({ workspaceOpen: true })).float).toBeNull()
    expect(badgePlacement(slot, pane({ appsActive: true })).float).toBeNull()
    expect(badgePlacement(slot, pane({ projectsActive: true })).float).toBeNull()
    expect(badgePlacement(null, pane()).float).toBeNull()
  })

  it('takes the compact form over the terminal, not over its history', () => {
    expect(badgePlacement(slot, pane({ sessionInteractive: true })).compact).toBe(true)
    expect(badgePlacement(slot, pane({ sessionInteractive: true, showRichView: true })).compact).toBe(false)
  })

  // The real composer bar in a bottom panel: the wiring from the page's
  // state to the bar, which the bar's own tests above hand it directly.
  type ComposerProps = ComponentProps<typeof ChatComposerBar>
  function composer(view: Partial<BadgePane>) {
    const v = pane(view)
    const props = {
      viewedStreaming: false, warming: false, turnStartTime: null, thinkingActive: false, compressingActive: false,
      activeAgents: [lane('dyn-1', 'long lane → worker')], mode: 'ask', pendingEngineSwitch: null, model: 'claude-sonnet-5',
      modelCompound: '', billedCost: 0, toolCost: 0, costBilled: true, contextUsed: 0, contextMax: 0, cacheStats: undefined,
      meetingActive: false, permissionModes: undefined, agentLayerModels: [], modelGroups: [], interactiveAvailable: false,
      interactive: { interactiveMode: v.sessionInteractive, sessionInteractive: v.sessionInteractive, showRichView: v.showRichView },
      interactiveLocked: false, pickersLockReason: '', handleInteractiveToggle: () => {}, handleToggleRichView: () => {},
      isTaskChat: false, chatId: 'chat-1', agentName: 'pa', ws: { connected: true, sendPtyInput: () => {}, probeLiveness: () => {}, compactContext: () => {} },
      handleModeChange: () => {}, handleModelChange: () => {}, chatActiveLayer: null, effectiveLayer: 'claude-code-cli',
      supportsCompact: false, limitReached: false, limitReachedInfo: null, limitWarning: null, setLimitWarning: () => {},
      engineLabels: {}, draftInput: '', setDraftInput: () => {}, handleSend: () => {}, handleAbort: () => {},
      handleEditQueued: () => {}, handleEngage: () => {}, permissionPending: false, warmingUp: false, aborting: false,
      queuedMessages: [], editText: null, setEditText: () => {}, pendingImages: [], draftKey: 'k', pendingFiles: [],
      handleAddFiles: () => {}, handleAddFolder: () => {}, handleRemoveFile: () => {}, handleRetryFile: () => {},
      workspace: { state: { open: v.workspaceOpen, hasNewMessage: false }, toggleWorkspace: () => {}, closeWorkspace: () => {} },
      appsActive: v.appsActive, toggleApps: () => {}, setAppsOpen: () => {},
      projectsActive: v.projectsActive, isProjectChat: false, dockAvailable: false, setProjectsOpen: () => {},
      voice: {}, composerAnimates: false,
    } as unknown as ComposerProps
    const { container } = render(<ChatBottomPanel><ChatComposerBar {...props} /></ChatBottomPanel>)
    const floated = container.querySelector<HTMLElement>('[data-badge-float]')!
    return {
      floated: within(floated).queryByTestId('float-badge-delegated')?.textContent ?? null,
      row: container.querySelector('.gap-2.mb-1\\.5.md\\:hidden')?.textContent ?? null,
    }
  }

  it('the composer floats the pill over the history, labelled', () => {
    expect(composer({})).toEqual({ floated: '1 Delegated', row: null })
  })

  it('the composer floats it compact over the terminal', () => {
    expect(composer({ sessionInteractive: true })).toEqual({ floated: '1', row: null })
    expect(composer({ sessionInteractive: true, showRichView: true }).floated).toBe('1 Delegated')
  })

  it('the composer keeps the row over the workspace, an app or the projects', () => {
    for (const view of [{ workspaceOpen: true }, { appsActive: true }, { projectsActive: true }]) {
      expect(composer(view), JSON.stringify(view)).toEqual({ floated: null, row: '1 Delegated' })
    }
  })

  it('the history keeps a constant room for one row of pills below md', () => {
    // Mounting ChatMessages needs the whole chat: its source is the cheaper proof.
    const src = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      '../components/chat/ChatMessages.tsx'), 'utf8')
    expect(src).toMatch(/ref=\{contentRef\} className="[^"]*\bpb-9 md:pb-4\b/)
  })
})

describe("the status row's order", () => {
  /** The main row by what it holds, a run of chips counted once and the free
   *  space as 'space'. A box that grows holds its items at its start and its
   *  free space after them. `bar` is the status bar's own element, the main
   *  row its last child. `phone` drops what is hidden below md. */
  function rowOrder(bar: Element, { phone = false } = {}): string[] {
    const walk = (el: Element): string[] => {
      if (phone && el.classList.contains('hidden')) return []
      if (el.getAttribute('data-testid') === 'keys') return ['keys']
      if (el.querySelector('[data-testid="keys"]')) {
        return el.classList.contains('flex-1') ? [...Array.from(el.children).flatMap(walk), 'space'] : ['keys']
      }
      if (el.className.includes('md:contents') || el.textContent === 'Meeting') return ['chips']
      if (el.className === 'flex-1') return ['space']
      if (el.querySelector('[title^="Mode:"]')) return ['mode']
      if (el.querySelector('[title^="Model:"]')) return ['model']
      if (el.textContent?.endsWith('%')) return ['gauge']
      return [el.textContent ?? '?']
    }
    const names = Array.from(bar.lastElementChild!.children).flatMap(walk)
    return names.filter((n, i) => n !== 'chips' || names[i - 1] !== 'chips')
  }
  const keys = <div data-testid="keys">Esc Tab Enter</div>
  const work = [lane('dyn-1', 'long lane → worker'), command('tuB')]

  function Row({ terminal, slot = null, context = 0 }: { terminal: boolean; slot?: HTMLElement | null; context?: number }) {
    return (
      <ChatStatusBar
        streaming={false} warming={false} startTime={null} thinkingActive={false} compressingActive={false}
        activeAgents={work} meetingActive mode="ask" model="claude-sonnet-5" costUsd={0}
        contextUsed={context / 10} contextMax={context} onModeChange={() => {}} onModelChange={() => {}}
        interactiveActive={terminal} leftSlot={terminal ? keys : undefined} badgeFloat={slot} terminalShown={terminal}
      />
    )
  }

  it('a terminal chat keeps its keys at the far left, the chips right after them', () => {
    const { container } = render(<Row terminal />)
    // The same rule as with no terminal: the left group, the chips, the free space.
    expect(rowOrder(container.firstElementChild!)).toEqual(['keys', 'chips', 'space', 'mode', 'model'])
    // The key area takes the free width and positions the More keys panel.
    const area = screen.getByTestId('keys').parentElement!.parentElement!
    expect(area.className.split(' ')).toEqual(expect.arrayContaining(['relative', 'flex-1', 'min-w-0', 'mr-3']))
    // The first chip sits a little further from the last key than the keys from each other.
    expect(screen.getByTestId('keys').parentElement!.className).toBe('min-w-0 md:mr-1')
    // In a narrow window the keys scroll, a chip never wraps or shrinks.
    const block = container.querySelector('.hidden.md\\:contents')!
    expect(Array.from(block.children).map((c) => c.textContent)).toEqual(['Meeting', '1 Commands', '1 Delegated'])
    expect(block.className.split(' ')).toEqual(expect.arrayContaining(['*:shrink-0', '*:whitespace-nowrap']))
  })

  it('a chat with no terminal keeps the chips after the gauge', () => {
    const { container } = render(<Row terminal={false} context={1000} />)
    expect(rowOrder(container.firstElementChild!)).toEqual(['gauge', 'chips', 'space', 'mode', 'model'])
    // The chips in the row's own order, the Meeting chip first.
    const chips = Array.from(container.firstElementChild!.lastElementChild!.children)
      .flatMap((el) => el.className.includes('md:contents') ? Array.from(el.children) : [el])
      .filter((el) => el.className.includes('rounded-full'))
    expect(chips.map((c) => c.textContent)).toEqual(['Meeting', '1 Commands', '1 Delegated'])
  })

  it("a phone's row is the keys and the controls, its chips float or sit above", () => {
    const view = render(<ChatBottomPanel><Row terminal /></ChatBottomPanel>)
    const slot = view.container.querySelector<HTMLElement>('[data-badge-float]')!
    view.rerender(<ChatBottomPanel><Row terminal slot={slot} /></ChatBottomPanel>)
    expect(rowOrder(slot.nextElementSibling!, { phone: true })).toEqual(['keys', 'space', 'mode', 'model'])
    expect(Array.from(slot.querySelectorAll('[data-testid]')).map((p) => p.getAttribute('data-testid')))
      .toEqual(['float-badge-commands', 'float-badge-delegated', 'float-badge-meeting'])
    view.unmount()
    // Over the workspace, an app or the projects: the row above, then the keys.
    const { container } = render(<Row terminal />)
    const above = container.firstElementChild!.firstElementChild!
    expect(above.className).toContain('md:hidden')
    expect(Array.from(above.children).map((c) => c.textContent)).toEqual(['1 Commands', '1 Delegated', 'Meeting'])
    expect(rowOrder(container.firstElementChild!, { phone: true })).toEqual(['keys', 'space', 'mode', 'model'])
  })
})
