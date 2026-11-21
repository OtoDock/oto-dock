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

  it('a paste from an office suite inserts its text and attaches nothing', async () => {
    const props = baseProps()
    const { container } = render(<ChatInput {...props} />)
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    const plain = 'Q3 revenue\t1200\nQ4 revenue\t1500'
    fireEvent.paste(textarea, {
      clipboardData: {
        files: [makeFile('image.png', 100, 'image/png')],
        getData: (kind: string) => (kind === 'text/plain' ? plain : ''),
      },
    })
    await new Promise((r) => setTimeout(r, 50))
    expect(props.onAddImages).not.toHaveBeenCalled()
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

  it('a folder chip says Folder while it is read, retries from its files, notes what it lost', () => {
    const props = baseProps()
    const files = [{ file: makeFile('a.md', 600), relDir: '' }, { file: makeFile('b.md', 600), relDir: '' }]
    props.pendingFiles = [
      { id: 'r', name: 'reading', size: 0, folder: true, uploading: true, queued: true },
      // 1200 bytes in all, over the 1000-byte cap: a folder's own files are what the cap checks.
      { id: 'f', name: 'failed', size: 1200, folder: true, fileCount: 2, folderFiles: files, error: 'b.md: HTTP 500' },
      { id: 'x', name: 'refused', size: 0, folder: true, fileCount: 0, error: 'An empty folder: nothing to attach' },
      { id: 'g', name: 'good', size: 7, folder: true, fileCount: 1, uploadedPath: 'w/good', note: '1 folder left out: .git' },
    ]
    const { container } = render(<ChatInput {...props} />)
    expect(container.textContent).toContain('readingFolder')
    expect(container.textContent).toContain('Reading the folder…')
    expect(container.textContent).toContain('1 folder left out: .git')
    const retries = screen.getAllByLabelText('Retry upload')
    expect(retries).toHaveLength(1)
    fireEvent.click(retries[0])
    expect(props.onRetryFile).toHaveBeenCalledWith('f')
  })

  it('a dropped folder is handed over at once, its walk as a promise', async () => {
    const onAddFolder = vi.fn()
    const { container } = render(<ChatInput {...baseProps()} onAddFolder={onAddFolder} />)
    const entry = {
      isFile: false, isDirectory: true, name: 'mesitis',
      createReader: () => {
        let read = false
        return {
          readEntries: (ok: (e: unknown[]) => void) => {
            ok(read ? [] : [{
              isFile: true, isDirectory: false, name: 'a.md',
              file: (got: (f: File) => void) => got(makeFile('a.md', 3)),
            }])
            read = true
          },
        }
      },
    }
    fireEvent.drop(composerPill(container), {
      dataTransfer: {
        types: ['Files'],
        items: [{ kind: 'file', getAsFile: () => null, webkitGetAsEntry: () => entry }],
        files: [],
      },
    })
    expect(onAddFolder).toHaveBeenCalledTimes(1)
    const [name, walk] = onAddFolder.mock.calls[0]
    expect(name).toBe('mesitis')
    expect(walk).toBeInstanceOf(Promise)
    const got = await walk
    expect(got.files.map((f: { relDir: string; file: File }) => [f.relDir, f.file.name])).toEqual([['', 'a.md']])
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

  describe('a file card dragged from a chat', () => {
    const TOKEN = 'k'.repeat(43)
    function card(ref: Record<string, unknown>) {
      const data: Record<string, string> = { 'application/x-otodock-file': JSON.stringify(ref) }
      return { files: [] as File[], items: [], types: Object.keys(data), getData: (t: string) => data[t] ?? '' }
    }
    const ref = () => ({ v: 1, origin: window.location.origin, token: TOKEN, name: 'q3.pdf' })

    it('shows the drop overlay', () => {
      const props = baseProps()
      const { container } = render(<ChatInput {...props} />)
      fireEvent.dragEnter(composerPill(container), { dataTransfer: card(ref()) })
      expect(container.textContent).toContain('Drop to attach')
    })

    it('hands the checked reference to the intake, and the textarea inserts nothing', async () => {
      const props = { ...baseProps(), onAddFetched: vi.fn() }
      const { container } = render(<ChatInput {...props} />)
      const notCancelled = fireEvent.drop(composerPill(container), { dataTransfer: card(ref()) })
      expect(notCancelled).toBe(false)
      expect(props.onAddFetched).toHaveBeenCalledTimes(1)
      expect(props.onAddFetched.mock.calls[0][0]).toBe('q3.pdf')
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(new Uint8Array(3)))
      await props.onAddFetched.mock.calls[0][1](new AbortController().signal)
      expect(fetchSpy.mock.calls[0][0]).toBe(`/v1/media/${TOKEN}?download=1`)
      fetchSpy.mockRestore()
      expect(props.onAddFiles).not.toHaveBeenCalled()
    })

    it('turns a card from another install away with its sentence and fetches nothing', () => {
      const props = { ...baseProps(), onAddFetched: vi.fn() }
      const { container } = render(<ChatInput {...props} />)
      fireEvent.drop(composerPill(container), { dataTransfer: card({ ...ref(), origin: 'https://other.example' }) })
      expect(props.onAddFetched).not.toHaveBeenCalled()
      expect(props.onAddFiles.mock.calls[0][0][0]).toMatchObject({
        name: 'Dropped file', error: 'This file is from another OtoDock.',
      })
    })

    it('shows no size on a chip that has no file yet, or was refused', () => {
      const props = baseProps()
      props.pendingFiles = [
        { id: 'w', name: 'q3.pdf', size: 0, uploading: true, queued: true },
        { id: 'r', name: 'Dropped file', size: 0, error: 'This file is from another OtoDock.' },
        { id: 'z', name: 'empty.txt', size: 0, file: makeFile('empty.txt', 0) },
      ]
      const { container } = render(<ChatInput {...props} />)
      expect(container.textContent).toContain('This file is from another OtoDock.')
      expect(container.textContent?.match(/0 B/g) ?? []).toHaveLength(1)   // the picked empty file keeps its size
    })

    it('lets real files win over a card', async () => {
      const props = { ...baseProps(), onAddFetched: vi.fn() }
      const { container } = render(<ChatInput {...props} />)
      const both = { ...card(ref()), files: [makeFile('real.zip', 300)], types: ['Files', 'application/x-otodock-file'] }
      fireEvent.drop(composerPill(container), { dataTransfer: both })
      await waitFor(() => expect(props.onAddFiles).toHaveBeenCalledTimes(1))
      expect(props.onAddFiles.mock.calls[0][0][0].name).toBe('real.zip')
      expect(props.onAddFetched).not.toHaveBeenCalled()
    })
  })
})
