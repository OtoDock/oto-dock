/**
 * Every notice between the chat and the composer has the composer's width:
 * one column (`COMPOSER_COLUMN` in `components/ui/classes`), the inset the composer keeps
 * from the screen's edges and its cap, at every screen width. A notice that
 * says why the composer is locked says it once, above it: the locked box
 * repeats nothing.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ComponentProps } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({ apiFetch: vi.fn(), inputProps: [] as Record<string, unknown>[] }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
vi.mock('@/components/chat/ChatInput', () => ({
  default: (props: Record<string, unknown>) => { h.inputProps.push(props); return null },
}))
vi.mock('@/components/chat/terminal/TerminalControlBar', () => ({ default: () => null }))

import ChatBanners from '@/pages/agent/chat/ChatBanners'
import ChatComposerBar, { ChatBottomPanel } from '@/pages/agent/chat/ChatComposerBar'
import { COMPOSER_COLUMN, COMPOSER_INSET } from '@/components/ui/classes'
import { noteServerUpdating } from '@/lib/session/serverInfo'

const LOCK = 'A phone call is writing in this chat. You can write here when it ends.'
const here = path.dirname(fileURLToPath(import.meta.url))
const source = (rel: string) => readFileSync(path.resolve(here, rel), 'utf8')

type BannerProps = ComponentProps<typeof ChatBanners>

/** The stack with every notice it can show at once. */
const ALL_ON: BannerProps = {
  sessionExecutionTarget: 'local', sessionFallbackReason: null, offlineMachineName: '',
  engineNotice: { message: 'This agent has no engine you can use.' } as BannerProps['engineNotice'],
  chatId: 'c1', pendingEngineSwitch: { layer: 'codex-cli', model: 'gpt-x' }, targetMismatch: null,
  phoneCallActive: true, platformUpdating: true, lockSentence: LOCK,
  viewedStreaming: false, warming: false, ws: {} as BannerProps['ws'], agentName: 'a', mode: 'ask', model: '',
  chatActiveLayer: null, selectedLayer: null, interactive: {} as BannerProps['interactive'],
  preWarmedRef: { current: null }, preWarmPath: '', layers: undefined,
  engineSwitchBusy: false, engineSwitchError: null,
  handleEngineSwitchConfirm: () => {}, handleEngineSwitchCancel: () => {},
}

function stack(props: Partial<BannerProps> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter><QueryClientProvider client={qc}><ChatBanners {...ALL_ON} {...props} /></QueryClientProvider></MemoryRouter>,
  )
}

/** The column a notice sits in: its nearest ancestor that is the composer's
 *  column, and that column's parent the composer's inset. */
function columnOf(el: HTMLElement): HTMLElement | null {
  let at = el.parentElement
  while (at && at.className !== COMPOSER_COLUMN) at = at.parentElement
  return at && at.parentElement?.className === COMPOSER_INSET ? at : null
}

/** A width rule of the notice's own: a cap, side margins or side padding
 *  past the box's own, or any of them changing with the screen. */
const OWN_WIDTH = /^(?:[a-z0-9]+:)?(?:max-w-|mx-|ml-|mr-|w-(?!full)|px-[4-9])|^(?:sm|md|lg|xl):/

const WIDTHS = [390, 720, 1280]

beforeEach(() => {
  h.apiFetch.mockReset()
  h.apiFetch.mockImplementation(async () => new Response('{}', { status: 404 }))
  h.inputProps.length = 0
})
afterEach(() => {
  noteServerUpdating(false)
  vi.unstubAllGlobals()
})

describe("the notices above the composer have the composer's width", () => {
  it.each(WIDTHS)('at %i px every notice of the stack sits in the one column, with no width of its own', (width) => {
    vi.stubGlobal('innerWidth', width)
    const { container } = stack()
    const notices = [
      screen.getByTestId('platform-updating-notice'), screen.getByTestId('phone-call-notice'),
      screen.getByTestId('engine-switch-banner'), screen.getByTestId('chat-lock-notice'),
      screen.getByText('This agent has no engine you can use.').closest('div') as HTMLElement,
    ]
    const columns = new Set(notices.map(columnOf))
    expect(columns.size).toBe(1)
    const column = [...columns][0]
    expect(column).not.toBeNull()
    expect(container.firstElementChild?.firstElementChild).toBe(column)
    // Each notice's box, and anything between it and the column, keeps no
    // width of its own.
    for (const notice of notices) {
      for (let el: HTMLElement | null = notice; el && el !== column; el = el.parentElement) {
        const own = el.className.split(/\s+/).filter((c) => OWN_WIDTH.test(c))
        expect(own, el.outerHTML.slice(0, 120)).toEqual([])
      }
    }
  })

  it('the composer and the notices take the width from one place', () => {
    const input = source('../components/chat/ChatInput.tsx')
    expect(input).toContain('className={`${COMPOSER_INSET} pb-composer-safe`}')
    expect(input).toContain('<div className={COMPOSER_COLUMN}>')
    expect(source('../pages/agent/chat/ChatBanners.tsx')).toContain('<ChatLockNotice sentence={lockSentence} />')
    // The lock is part of the stack, not a line of the page's own.
    expect(source('../pages/agent/AgentChat.tsx')).not.toContain('<ChatLockNotice')
  })

  it.each(WIDTHS)("at %i px the usage limit's notice sits in the composer's column", (width) => {
    vi.stubGlobal('innerWidth', width)
    composer({ limitReached: true })
    const notice = screen.getByTestId('limit-banner')
    expect(notice.className.split(/\s+/)).toEqual(expect.arrayContaining(COMPOSER_COLUMN.split(' ')))
    expect(notice.parentElement?.className).toBe(COMPOSER_INSET)
    expect(notice.className.split(/\s+/).filter((c) => /^m[xlr]-[1-9]/.test(c))).toEqual([])
  })
})

describe('a locked composer says why once, above it', () => {
  it('the lock is one notice in the stack', () => {
    stack({ platformUpdating: false, phoneCallActive: false, engineNotice: null, pendingEngineSwitch: null })
    expect(screen.getAllByText(LOCK)).toHaveLength(1)
    expect(screen.getByTestId('chat-lock-notice')).toHaveAttribute('role', 'status')
  })

  it('no lock, no notice', () => {
    stack({ platformUpdating: false, phoneCallActive: false, engineNotice: null, pendingEngineSwitch: null,
            lockSentence: '' })
    expect(screen.queryByTestId('chat-lock-notice')).toBeNull()
  })

  it('the locked box is disabled and repeats nothing', () => {
    composer({ composerLockReason: LOCK })
    expect(last()).toMatchObject({ disabled: true, placeholder: '' })
  })

  it('the usage limit is said once too', () => {
    composer({ limitReached: true })
    expect(last()).toMatchObject({ disabled: true, placeholder: '' })
  })

  it('an open composer keeps its hint', () => {
    composer({})
    expect(last()?.disabled).toBe(false)
    expect(String(last()?.placeholder)).toMatch(/^Type a message/)
  })
})

type ComposerProps = ComponentProps<typeof ChatComposerBar>

/** The props the composer's box got last. */
const last = () => h.inputProps[h.inputProps.length - 1]

function composer(extra: Partial<ComposerProps>) {
  const props = {
    viewedStreaming: false, warming: false, turnStartTime: null, thinkingActive: false, compressingActive: false,
    activeAgents: [], mode: 'ask', pendingEngineSwitch: null, model: 'claude-sonnet-5',
    modelCompound: '', billedCost: 0, toolCost: 0, costBilled: true, contextUsed: 0, contextMax: 0, cacheStats: undefined,
    meetingActive: false, permissionModes: undefined, agentLayerModels: [], modelGroups: [], interactiveAvailable: false,
    interactive: { interactiveMode: false, sessionInteractive: false, showRichView: false },
    interactiveLocked: false, pickersLockReason: '', handleInteractiveToggle: () => {}, handleToggleRichView: () => {},
    isTaskChat: false, chatId: 'chat-1', agentName: 'pa', ws: { connected: true, sendPtyInput: () => {}, probeLiveness: () => {}, compactContext: () => {} },
    handleModeChange: () => {}, handleModelChange: () => {}, chatActiveLayer: null, effectiveLayer: 'claude-code-cli',
    supportsCompact: false, limitReached: false, limitReachedInfo: null, limitWarning: null, setLimitWarning: () => {},
    engineLabels: {}, draftInput: '', setDraftInput: () => {}, handleSend: () => {}, handleAbort: () => {},
    handleEditQueued: () => {}, handleEngage: () => {}, permissionPending: false, warmingUp: false, aborting: false,
    queuedMessages: [], editText: null, setEditText: () => {}, pendingImages: [], draftKey: 'k', pendingFiles: [],
    handleAddFiles: () => {}, handleAddFolder: () => {}, handleRemoveFile: () => {}, handleRetryFile: () => {},
    workspace: { state: { open: false, hasNewMessage: false }, toggleWorkspace: () => {}, closeWorkspace: () => {} },
    appsActive: false, toggleApps: () => {}, setAppsOpen: () => {},
    projectsActive: false, isProjectChat: false, dockAvailable: false, setProjectsOpen: () => {},
    voice: {}, composerAnimates: false, ...extra,
  } as unknown as ComposerProps
  return render(<ChatBottomPanel><ChatComposerBar {...props} /></ChatBottomPanel>)
}
