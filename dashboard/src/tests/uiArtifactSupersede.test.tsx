import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act, renderHook } from '@testing-library/react'

import BlockRenderer from '@/components/chat/ChatBlockRenderer'
import UiArtifact from '@/components/chat/media/UiArtifact'
import { supersededUiBlocks, uiTitlesByPath } from '@/lib/messageBlocks'
import { useArtifactWindows } from '@/hooks/useArtifactWindows'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'

const noop = () => {}

function renderBlock(block: MessageBlock, extra: Record<string, unknown> = {}) {
  return render(
    <BlockRenderer
      block={block}
      blockId="m1-b0"
      blockOrder={0}
      isUserMessage={false}
      onPermissionRespond={noop}
      {...extra}
    />,
  )
}

const uiBlock = (path?: string, token = 'tok-1'): MessageBlock => ({
  type: 'ui', token, uiUrl: `/v1/ui/${token}`, title: 'Connect Four', path,
})

// html-less re-display: no title on the wire.
const untitledUiBlock = (path?: string, token = 'tok-2'): MessageBlock => ({
  type: 'ui', token, uiUrl: `/v1/ui/${token}`, path,
})

function msg(id: string, blocks: MessageBlock[]): DisplayMessage {
  return { id, role: 'assistant', blocks, createdAt: '2026-07-11T00:00:00Z' } as DisplayMessage
}

// ───────────────────────── supersede map (pure) ─────────────────────────────

describe('supersededUiBlocks', () => {
  it('marks every instance of a path except the LAST across messages', () => {
    const messages = [
      msg('m0', [uiBlock('ws/board.html', 'a')]),
      msg('m1', [{ type: 'text', content: 'my move' } as MessageBlock]),
      msg('m2', [uiBlock('ws/board.html', 'b'), uiBlock('ws/other.html', 'c')]),
      msg('m3', [uiBlock('ws/board.html', 'd')]),
    ]
    const set = supersededUiBlocks(messages)
    expect(set.has('0:0')).toBe(true)   // first board — superseded
    expect(set.has('2:0')).toBe(true)   // middle board — superseded
    expect(set.has('2:1')).toBe(false)  // other.html — its own latest
    expect(set.has('3:0')).toBe(false)  // latest board
  })

  it('never supersedes blocks without a path (distinct one-off artifacts)', () => {
    const set = supersededUiBlocks([
      msg('m0', [uiBlock(undefined, 'a')]),
      msg('m1', [uiBlock(undefined, 'b')]),
    ])
    expect(set.size).toBe(0)
  })

  it('uiTitlesByPath keeps the latest non-empty title per path', () => {
    const titles = uiTitlesByPath([
      msg('m0', [uiBlock('ws/board.html', 'a')]),           // "Connect Four"
      msg('m1', [untitledUiBlock('ws/board.html', 'b')]),   // html-less re-display
    ])
    expect(titles.get('ws/board.html')).toBe('Connect Four')
  })

  it('chip and artifact inherit the fallback title for html-less re-displays', () => {
    renderBlock(untitledUiBlock('ws/board.html'), {
      uiSuperseded: true, uiTitle: 'Connect Four',
    })
    expect(screen.getByText('Connect Four')).toBeTruthy()
  })
})

// ───────────────────────── superseded chip render ────────────────────────────

describe('superseded ui block chip', () => {
  it('renders a chip (no iframe) and scrolls to the latest instance on click', () => {
    const { container } = renderBlock(uiBlock('ws/board.html'), { uiSuperseded: true })
    expect(container.querySelector('iframe')).toBeNull()
    expect(screen.getByTestId('ui-artifact-superseded')).toBeTruthy()
    expect(screen.getByText('Connect Four')).toBeTruthy()

    // The latest full instance carries data-ui-path — chip click scrolls to it.
    const target = document.createElement('div')
    target.setAttribute('data-ui-path', 'ws/board.html')
    target.scrollIntoView = vi.fn()
    document.body.appendChild(target)
    fireEvent.click(screen.getByTestId('ui-artifact-superseded'))
    expect(target.scrollIntoView).toHaveBeenCalled()
    target.remove()
  })

  it('renders the full artifact when not superseded', () => {
    const { container } = renderBlock(uiBlock('ws/board.html'), { uiSuperseded: false })
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('ui-artifact-superseded')).toBeNull()
  })
})

// ───────────────────────── building placeholder ──────────────────────────────

describe('display_ui building placeholder', () => {
  const tool = (name: string, status: 'running' | 'done'): MessageBlock => ({
    type: 'tool', name, toolId: 't1', summary: name, status,
  })

  it('running display_ui renders the build card instead of a pill', () => {
    renderBlock(tool('mcp__display__display_ui', 'running'))
    expect(screen.getByTestId('ui-artifact-building')).toBeTruthy()
    expect(screen.getByText('Working on an interactive artifact…')).toBeTruthy()
  })

  it('running pin_app renders the mini-app variant', () => {
    renderBlock(tool('mcp__display__pin_app', 'running'))
    expect(screen.getByText('Working on a mini-app…')).toBeTruthy()
  })

  it('completed display_ui and other running tools keep the normal pill', () => {
    const { rerender } = renderBlock(tool('mcp__display__display_ui', 'done'))
    expect(screen.queryByTestId('ui-artifact-building')).toBeNull()
    rerender(
      <BlockRenderer
        block={tool('Bash', 'running')}
        blockId="m1-b0"
        blockOrder={0}
        isUserMessage={false}
        onPermissionRespond={noop}
      />,
    )
    expect(screen.queryByTestId('ui-artifact-building')).toBeNull()
  })
})

// ───────────────────────── footer chrome placement ───────────────────────────

describe('UiArtifact chrome footer', () => {
  it('marker sits in a non-overlay footer row and the wrapper carries data-ui-path', () => {
    const { container, getByText } = render(
      <UiArtifact token="tok-1" uiUrl="/v1/ui/tok-1" path="ws/board.html" />,
    )
    const marker = getByText('generated')
    // Never an absolute overlay — a reserved row below the frame cannot sit
    // on artifact content (the old top-right cluster covered headers).
    expect(marker.parentElement!.className).not.toContain('absolute')
    expect(
      container.querySelector('[data-testid="ui-artifact"]')!.getAttribute('data-ui-path'),
    ).toBe('ws/board.html')
  })
})

// ───────────────────────── PiP same-path replace ─────────────────────────────

describe('useArtifactWindows ui replace-by-path', () => {
  function makeWs() {
    const handlers = new Map<string, (msg: any) => void>()
    return {
      ws: {
        subscribe: (type: string, cb: (msg: any) => void) => {
          handlers.set(type, cb)
          return () => handlers.delete(type)
        },
      } as any,
      emit: (event: any) => handlers.get('pty_artifact')?.({ chat_id: 'c1', event }),
    }
  }

  it('a re-shown artifact replaces its window in place; new paths stack', () => {
    const { ws, emit } = makeWs()
    const { result } = renderHook(() => useArtifactWindows(ws, 'c1'))
    act(() => {
      emit({ type: 'ui', token: 'a', ui_url: '/v1/ui/a', title: 'Board', path: 'ws/board.html' })
    })
    expect(result.current.windows).toHaveLength(1)
    act(() => {
      emit({ type: 'ui', token: 'b', ui_url: '/v1/ui/b', title: 'Board v2', path: 'ws/board.html' })
    })
    expect(result.current.windows).toHaveLength(1)
    expect((result.current.windows[0].block as any).token).toBe('b')
    expect(result.current.windows[0].title).toBe('Board v2')
    act(() => {
      emit({ type: 'ui', token: 'c', ui_url: '/v1/ui/c', title: 'Other', path: 'ws/other.html' })
    })
    expect(result.current.windows).toHaveLength(2)
  })
})
