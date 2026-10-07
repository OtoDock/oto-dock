import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

// A send while the chat's document pane holds unsaved edits saves them
// first (the agent reads the stored file): the send button is disabled
// while the editor saves, a save that is not confirmed in time sends with
// a notice, and the composer says so while the edits are unsaved.

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({ available: false, status: 'idle', start: vi.fn(), stop: vi.fn(), toggle: vi.fn() }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))
vi.mock('@/components/chat/media/ImageLightbox', () => ({ default: () => null }))

import ChatInput from '@/components/chat/ChatInput'
import { SEND_SAVE_TIMEOUT_MS } from '@/components/chat/media/DocumentFrame'
import { registerPaneSave, useDocumentPushStore } from '@/store/documentPaneStore'

function props(over: Record<string, unknown> = {}) {
  return {
    value: 'hello',
    onChange: vi.fn(),
    onSend: vi.fn(),
    pendingImages: [],
    onAddImages: vi.fn(),
    onRemoveImage: vi.fn(),
    pendingFiles: [],
    onAddFiles: vi.fn(),
    onRemoveFile: vi.fn(),
    draftKey: 'c1',
    ...over,
  }
}

function deferredSave() {
  let resolve: (v: boolean) => void = () => {}
  const save = vi.fn(() => new Promise<boolean>((r) => { resolve = r }))
  return { save, answer: (v: boolean) => act(async () => { resolve(v) }) }
}

const sendButton = () => screen.getByRole('button', { name: 'Send' })

let unregister: () => void = () => {}
beforeEach(() => {
  unregister()
  useDocumentPushStore.setState({ dirty: {}, dirtyNames: {} })
})

describe('ChatInput: a send with unsaved document edits', () => {
  it('a clean document sends at once, with no line above the composer', () => {
    const p = props()
    render(<ChatInput {...p} />)
    expect(screen.queryByRole('status')).toBeNull()
    fireEvent.click(sendButton())
    expect(p.onSend).toHaveBeenCalledWith('hello')
  })

  it('saves first with the button disabled, then sends', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    render(<ChatInput {...p} />)
    expect(screen.getByRole('status')).toHaveTextContent('Unsaved edits in report.docx.')
    fireEvent.click(sendButton())
    // The send's own wait, shorter than a leave's: a message goes out even
    // when the editor is slow to confirm.
    expect(d.save).toHaveBeenCalledTimes(1)
    expect(d.save).toHaveBeenCalledWith(SEND_SAVE_TIMEOUT_MS)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(sendButton()).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('Saving report.docx before sending.')
    await d.answer(true)
    expect(p.onSend).toHaveBeenCalledWith('hello')
    expect(screen.queryByText(/not saved yet/)).toBeNull()
  })

  it('a save the editor does not confirm sends anyway, with the notice', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    render(<ChatInput {...p} />)
    fireEvent.click(sendButton())
    await d.answer(false)
    expect(p.onSend).toHaveBeenCalledWith('hello')
    expect(screen.getByRole('status')).toHaveTextContent("The document's last edits are not saved yet.")
  })

  it('Shift+Enter while the save awaits sends once', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    render(<ChatInput {...p} />)
    const box = screen.getByRole('textbox')
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(d.save).toHaveBeenCalledTimes(1)
    await d.answer(true)
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })

  it('a chat switch during the save sends nothing into the other chat', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    const view = render(<ChatInput {...p} />)
    fireEvent.click(sendButton())
    view.rerender(<ChatInput {...p} draftKey="c2" value="" />)
    await d.answer(true)
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it("the send after the save goes through the page's send as it is then", async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    const view = render(<ChatInput {...p} />)
    fireEvent.click(sendButton())
    // A turn started meanwhile: the page hands a new send.
    const later = vi.fn()
    view.rerender(<ChatInput {...p} onSend={later} />)
    await d.answer(true)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(later).toHaveBeenCalledWith('hello')
  })

  it('a file still uploading when the save answers keeps the message in the composer', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    const view = render(<ChatInput {...p} />)
    fireEvent.click(sendButton())
    view.rerender(<ChatInput {...p} pendingFiles={[{ id: 'u1', name: 'a.pdf', size: 3, uploading: true } as never]} />)
    await d.answer(true)
    expect(p.onSend).not.toHaveBeenCalled()
    expect(p.onChange).not.toHaveBeenCalledWith('')
  })

  it('a chat switch lets the next chat send while the old save is still out', async () => {
    useDocumentPushStore.getState().setDirty('c1', 'f1', 'report.docx')
    const d = deferredSave()
    unregister = registerPaneSave('c1', d.save)
    const p = props()
    const view = render(<ChatInput {...p} />)
    fireEvent.click(sendButton())
    expect(sendButton()).toBeDisabled()
    view.rerender(<ChatInput {...p} draftKey="c2" value="hi there" />)
    expect(sendButton()).not.toBeDisabled()
    expect(screen.queryByRole('status')).toBeNull()
    fireEvent.click(sendButton())
    expect(p.onSend).toHaveBeenCalledWith('hi there')
    await d.answer(true)
    expect(p.onSend).toHaveBeenCalledTimes(1)
  })
})
