// ChatInput attachment surface: every entrance (picker, paste, drop) routes
// through one ingest helper — oversize files become VISIBLE error chips (the
// old path silently ate them), chips show live progress / queued / retry
// states, and tapping an image tile opens the standard lightbox.

import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/react'

vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({
    available: false, status: 'idle',
    start: vi.fn(), stop: vi.fn(), toggle: vi.fn(),
  }),
}))

// A 1000-byte install cap makes oversize cases cheap to construct.
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { feature_flags: { upload_max_bytes: 1000 } } }),
}))

// The real lightbox portals + wires esc/history stacks — not under test here.
vi.mock('@/components/chat/media/ImageLightbox', () => ({
  default: ({ images, initialIndex }: { images: unknown[]; initialIndex?: number }) => (
    <div data-testid="lightbox" data-count={images.length} data-idx={initialIndex} />
  ),
}))

import ChatInput from '@/components/chat/ChatInput'
import type { PendingFile } from '@/store/types'

function baseProps() {
  return {
    value: '',
    onChange: () => {},
    onSend: () => {},
    pendingImages: [],
    onAddImages: vi.fn(),
    onRemoveImage: () => {},
    pendingFiles: [] as PendingFile[],
    onAddFiles: vi.fn(),
    onRemoveFile: vi.fn(),
    onRetryFile: vi.fn(),
  }
}

function makeFile(name: string, size: number, type = 'application/octet-stream'): File {
  return new File([new Uint8Array(size)], name, { type })
}

function docInput(container: HTMLElement): HTMLInputElement {
  // The any-type upload input is the one without an accept filter.
  const inputs = Array.from(container.querySelectorAll('input[type="file"]'))
  return inputs.find((i) => !i.hasAttribute('accept')) as HTMLInputElement
}

function composerPill(container: HTMLElement): HTMLElement {
  return container.querySelector('[class*="backdrop-blur"]') as HTMLElement
}

describe('ChatInput attachments', () => {
  it('a file over the cap becomes a visible error chip instead of vanishing', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    fireEvent.change(docInput(container), { target: { files: [makeFile('huge.mp4', 2000)] } })
    await waitFor(() => expect(props.onAddFiles).toHaveBeenCalledTimes(1))
    const [files] = props.onAddFiles.mock.calls[0]
    expect(files).toHaveLength(1)
    expect(files[0].error).toContain('upload limit')
    expect(files[0].file).toBeInstanceOf(File)
  })

  it('a file within the cap is added with no error', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    fireEvent.change(docInput(container), { target: { files: [makeFile('ok.pdf', 500)] } })
    await waitFor(() => expect(props.onAddFiles).toHaveBeenCalledTimes(1))
    expect(props.onAddFiles.mock.calls[0][0][0].error).toBeUndefined()
  })

  it('pasting a file attaches it like the + menu', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.paste(textarea, { clipboardData: { files: [makeFile('notes.txt', 100)] } })
    await waitFor(() => expect(props.onAddFiles).toHaveBeenCalledTimes(1))
    expect(props.onAddFiles.mock.calls[0][0][0].name).toBe('notes.txt')
  })

  it('pasting a small image goes to the inline photo path', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.paste(textarea, {
      clipboardData: { files: [makeFile('shot.png', 100, 'image/png')] },
    })
    await waitFor(() => expect(props.onAddImages).toHaveBeenCalledTimes(1))
    const [images] = props.onAddImages.mock.calls[0]
    expect(images[0].name).toBe('shot.png')
    expect(images[0].base64).toMatch(/^data:/)
    expect(props.onAddFiles).not.toHaveBeenCalled()
  })

  it('dropping an external file on the composer attaches it', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    fireEvent.drop(composerPill(container), {
      dataTransfer: { files: [makeFile('drop.zip', 300)], types: ['Files'] },
    })
    await waitFor(() => expect(props.onAddFiles).toHaveBeenCalledTimes(1))
    expect(props.onAddFiles.mock.calls[0][0][0].name).toBe('drop.zip')
  })

  it('an external-file drag shows the drop overlay', () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    fireEvent.dragEnter(composerPill(container), {
      dataTransfer: { files: [], types: ['Files'] },
    })
    expect(container.textContent).toContain('Drop to attach')
  })

  it('renders queued / progress / syncing chip states', () => {
    const props = baseProps()
    props.pendingFiles = [
      { id: 'q', name: 'q.bin', size: 500, file: makeFile('q.bin', 500), uploading: true, queued: true },
      {
        id: 'u', name: 'u.bin', size: 500, file: makeFile('u.bin', 500),
        uploading: true, uploadSent: 250_000, uploadTotal: 1_000_000,
      },
    ]
    const { container } = render(<ChatInput {...props} />)
    expect(container.textContent).toContain('Queued')
    expect(container.textContent).toContain('25%')
  })

  it('error chips show the message inline and retry only when retryable', () => {
    const props = baseProps()
    props.pendingFiles = [
      { id: 'e1', name: 'net.bin', size: 500, file: makeFile('net.bin', 500), error: 'Network error during upload' },
      { id: 'e2', name: 'big.bin', size: 2000, file: makeFile('big.bin', 2000), error: 'Over the 1000 B upload limit' },
    ]
    const { container } = render(<ChatInput {...props} />)
    expect(container.textContent).toContain('Network error during upload')
    const retries = screen.getAllByLabelText('Retry upload')
    expect(retries).toHaveLength(1) // the over-cap chip is remove-only
    fireEvent.click(retries[0])
    expect(props.onRetryFile).toHaveBeenCalledWith('e1')
  })

  it('tapping an image tile opens the lightbox at that photo', () => {
    const props = baseProps()
    const png = 'data:image/png;base64,iVBORw0KGgo='
    ;(props as any).pendingImages = [
      { id: 'i1', base64: png, name: 'one.png' },
      { id: 'i2', base64: png, name: 'two.png' },
    ]
    render(<ChatInput {...props} />)
    fireEvent.click(screen.getByLabelText('Preview two.png'))
    const box = screen.getByTestId('lightbox')
    expect(box.getAttribute('data-count')).toBe('2')
    expect(box.getAttribute('data-idx')).toBe('1')
  })

  it('an open lightbox survives the pending photos emptying in the background', () => {
    // Background store activity (draft→chat slice re-key at warmup, another
    // tab clearing the draft) can transiently empty the live array — the
    // viewer must keep its open-time snapshot instead of yanking closed
    // (2026-08-13 "picture closes itself after ~10s" report).
    const props = baseProps()
    const png = 'data:image/png;base64,iVBORw0KGgo='
    ;(props as any).pendingImages = [{ id: 'i1', base64: png, name: 'one.png' }]
    const { rerender } = render(<ChatInput {...props} />)
    fireEvent.click(screen.getByLabelText('Preview one.png'))
    expect(screen.getByTestId('lightbox')).toBeTruthy()
    rerender(<ChatInput {...props} pendingImages={[]} />)
    const box = screen.getByTestId('lightbox')
    expect(box).toBeTruthy()
    expect(box.getAttribute('data-count')).toBe('1')
  })
})
