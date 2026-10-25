// The workspace's tiles and tree rows hold the platform's long press
// (hooks/useLongPress): a held touch enters selection mode (or, in a tree
// with no host handler, opens the menu at the finger), the click that
// follows the release is swallowed, and Android's contextmenu during the
// hold opens no second menu. A desktop right-click still opens the menu.
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FileNode } from '@/api/agents'
import FileTree from '@/components/FileTree'
import FileTile from '@/components/workspace/FileTile'

const NODE: FileNode = { name: 'notes.md', type: 'file', path: 'workspace/notes.md', size: 12, modified: '2026-10-16T08:00:00Z' }

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const touch = (el: Element, x = 40, y = 40) => fireEvent.touchStart(el, { touches: [{ clientX: x, clientY: y }] })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('FileTile long press', () => {
  function tile(over: Partial<Parameters<typeof FileTile>[0]> = {}) {
    const props = {
      agent: 'ag', node: NODE, selected: false, renaming: false, isDesktop: false,
      onClick: vi.fn(), onDoubleClick: vi.fn(), onContextMenu: vi.fn(), onLongPress: vi.fn(), ...over,
    }
    wrap(<FileTile {...props} />)
    return { props, el: screen.getByText('notes.md').closest('[data-ta-path]')! }
  }

  it('enters selection mode on a held touch and swallows the click of the release', () => {
    const { props, el } = tile()
    touch(el)
    // Android fires contextmenu during the hold: no menu over the selection.
    fireEvent.contextMenu(el, { clientX: 40, clientY: 40 })
    act(() => { vi.advanceTimersByTime(500) })
    expect(props.onLongPress).toHaveBeenCalledTimes(1)
    fireEvent.touchEnd(el)
    fireEvent.click(el)
    expect(props.onClick).not.toHaveBeenCalled()
    expect(props.onContextMenu).not.toHaveBeenCalled()
    // The next plain tap goes through.
    touch(el)
    fireEvent.touchEnd(el)
    fireEvent.click(el)
    expect(props.onClick).toHaveBeenCalledTimes(1)
  })

  it('a mouse click after a long press with no click of its own goes through', () => {
    const { props, el } = tile()
    touch(el)
    act(() => { vi.advanceTimersByTime(500) })
    fireEvent.touchEnd(el)
    // Android sent no click after the hold; the next press is the mouse's.
    fireEvent.pointerDown(el, { pointerType: 'mouse' })
    fireEvent.click(el)
    expect(props.onClick).toHaveBeenCalledTimes(1)
  })

  it('a moving finger is a scroll, and a desktop right-click opens the menu', () => {
    const { props, el } = tile()
    touch(el)
    fireEvent.touchMove(el, { touches: [{ clientX: 40, clientY: 60 }] })
    act(() => { vi.advanceTimersByTime(600) })
    expect(props.onLongPress).not.toHaveBeenCalled()
    fireEvent.contextMenu(el, { clientX: 5, clientY: 6 })
    expect(props.onContextMenu).toHaveBeenCalledWith({ clientX: 5, clientY: 6 })
  })

  it('a tile with no long press keeps its taps', () => {
    const { props, el } = tile({ onLongPress: undefined })
    touch(el)
    act(() => { vi.advanceTimersByTime(600) })
    fireEvent.touchEnd(el)
    fireEvent.click(el)
    expect(props.onClick).toHaveBeenCalledTimes(1)
  })
})

describe('FileTree row long press', () => {
  function tree(over: Partial<Parameters<typeof FileTree>[0]> = {}) {
    const props = {
      nodes: [NODE], selectedPaths: new Set<string>(), isDesktop: false,
      onSelectReplace: vi.fn(), onSelectToggle: vi.fn(), onSelectRange: vi.fn(), onOpenFile: vi.fn(),
      onContextMenu: vi.fn(), ...over,
    }
    wrap(<FileTree {...props} />)
    return { props, row: screen.getByText('notes.md').closest('[draggable]')! }
  }

  it("hands a held touch to the host's long press and swallows the release's click", () => {
    const onLongPress = vi.fn()
    const { props, row } = tree({ onLongPress })
    touch(row)
    act(() => { vi.advanceTimersByTime(500) })
    expect(onLongPress).toHaveBeenCalledWith(NODE)
    fireEvent.touchEnd(row)
    fireEvent.click(row)
    expect(props.onOpenFile).not.toHaveBeenCalled()
    expect(props.onSelectReplace).not.toHaveBeenCalled()
  })

  it('with no host long press, opens the menu once, at the finger', () => {
    const { props, row } = tree()
    touch(row, 70, 90)
    fireEvent.contextMenu(row, { clientX: 70, clientY: 90 })
    act(() => { vi.advanceTimersByTime(500) })
    expect(props.onContextMenu).toHaveBeenCalledTimes(1)
    expect(props.onContextMenu).toHaveBeenCalledWith(NODE, { clientX: 70, clientY: 90 })
  })
})
