/**
 * The banner stack's line while the platform holds new work before an
 * update (`server_info.updating`): shown on an existing chat while the flag
 * is on, gone when a frame clears it. A new chat shows none: its first
 * message is refused meanwhile, not kept.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))

import ChatBanners from '@/pages/agent/chat/ChatBanners'
import { noteServerUpdating, useServerUpdating } from '@/lib/session/serverInfo'

type Props = Parameters<typeof ChatBanners>[0]

/** A quiet chat: no other banner of the stack shows. */
const QUIET: Omit<Props, 'chatId' | 'platformUpdating'> = {
  sessionExecutionTarget: 'local', sessionFallbackReason: null, offlineMachineName: '',
  engineNotice: null, pendingEngineSwitch: null, targetMismatch: null, phoneCallActive: false, lockSentence: '',
  viewedStreaming: false, warming: false, ws: {} as Props['ws'], agentName: 'a', mode: 'default', model: '',
  chatActiveLayer: null, selectedLayer: null, interactive: {} as Props['interactive'],
  preWarmedRef: { current: null }, preWarmPath: '', layers: undefined,
  engineSwitchBusy: false, engineSwitchError: null,
  handleEngineSwitchConfirm: () => {}, handleEngineSwitchCancel: () => {},
}

/** The stack as the page feeds it: the flag read through the page's hook. */
function Stack({ chatId }: { chatId: string | null }) {
  const platformUpdating = useServerUpdating()
  return <ChatBanners {...QUIET} chatId={chatId} platformUpdating={platformUpdating} />
}

function renderStack(chatId: string | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><Stack chatId={chatId} /></QueryClientProvider>)
}

const NOTICE = 'platform-updating-notice'

beforeEach(() => {
  noteServerUpdating(false)
  h.apiFetch.mockReset()
  h.apiFetch.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Not found' }), { status: 404 }))
})

describe("the platform's update line above the composer", () => {
  it('shows only while the flag is on', () => {
    const { container } = renderStack('c1')
    expect(screen.queryByTestId(NOTICE)).toBeNull()
    expect(container.textContent).toBe('')
    act(() => noteServerUpdating(true))
    expect(screen.getAllByTestId(NOTICE)).toHaveLength(1)
    expect(screen.getByTestId(NOTICE).textContent)
      .toBe('OtoDock is updating. Messages you send now go out when it is back.')
    act(() => noteServerUpdating(false))
    expect(screen.queryByTestId(NOTICE)).toBeNull()
  })

  it('goes with the frame a reconnected socket gets, which carries no flag', () => {
    act(() => noteServerUpdating(true))
    renderStack('c1')
    expect(screen.getByTestId(NOTICE)).toBeInTheDocument()
    act(() => noteServerUpdating(undefined))
    expect(screen.queryByTestId(NOTICE)).toBeNull()
  })

  it('is a status line on the info tint with no control in it', () => {
    act(() => noteServerUpdating(true))
    renderStack('c1')
    const notice = screen.getByTestId(NOTICE)
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice.className.split(/\s+/)).toEqual(expect.arrayContaining(
      ['rounded-card', 'border', 'border-status-info-border', 'bg-status-info-bg', 'text-status-info-fg']))
    expect(notice.querySelector('button, a, input, textarea')).toBeNull()
  })

  it('stays away on a new chat, whose first message is refused meanwhile', () => {
    act(() => noteServerUpdating(true))
    renderStack(null)
    expect(screen.queryByTestId(NOTICE)).toBeNull()
  })
})

describe('the chat page feeds the line', () => {
  it('AgentChat reads the flag and hands it to ChatBanners', () => {
    // Mounting the whole page needs every hook it uses: the page's own
    // source is the cheaper proof.
    const page = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      '../AgentChat.tsx'), 'utf8')
    expect(page).toContain('const platformUpdating = useServerUpdating()')
    expect(page).toContain('platformUpdating={platformUpdating}')
  })
})
