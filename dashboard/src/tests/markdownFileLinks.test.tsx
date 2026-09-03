// Filesystem-path hrefs in chat markdown must never render as anchors: the
// browser cannot open local files, and react-markdown's defaultUrlTransform
// turned them into href="" — an anchor that reloads the current chat route
// (the efpolis incident). They render as code-styled chips instead — inert
// everywhere, clickable (resolve → preview) inside a chat (ChatFileContext).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import MarkdownContent, { isFileSystemPathHref } from '@/components/chat/MarkdownContent'
import { ChatFileProvider } from '@/components/chat/ChatFileContext'
import { SearchProvider } from '@/contexts/SearchContext'
import * as authApi from '@/api/auth'

// The preview surface is exercised in chatFilePreview.test.tsx — here it only
// marks "the preview opened".
vi.mock('@/components/workspace/FilePreviewBody', () => ({
  default: () => <div data-testid="file-preview-body" />,
}))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

describe('isFileSystemPathHref', () => {
  const paths = [
    'C:\\Users\\amour\\OtoDock\\file.xlsx',
    'C:/Users/amour/OtoDock/file.xlsx',
    '\\\\server\\share\\doc.docx', // UNC
    'file:///C:/Users/amour/file.pdf',
    'sandbox:/mnt/data/report.xlsx',
    '~/notes/todo.md',
    '/workspace/report.xlsx',
    '/users/x/workspace/y.xlsx',
    '/home/jim/file.txt',
    'report.xlsx', // bare relative — extension rule
    './notes.md', // accepted false positive (no working SPA target)
  ]
  const nonPaths = [
    'https://example.com/docs',
    'mailto:someone@example.com',
    '#anchor',
    '?q=search',
    '/chat/agent/abc123', // SPA route, no extension
    '/agents/foo/config', // /agents is deliberately not a POSIX root
    'tel:+301234567890',
  ]

  it('recognizes filesystem paths', () => {
    for (const href of paths) {
      expect(isFileSystemPathHref(href), href).toBe(true)
    }
  })

  it('leaves real URLs, anchors, queries and SPA routes alone', () => {
    for (const href of nonPaths) {
      expect(isFileSystemPathHref(href), href).toBe(false)
    }
  })
})

function renderMd(md: string) {
  return render(
    <SearchProvider query="">
      <MarkdownContent>{md}</MarkdownContent>
    </SearchProvider>,
  )
}

describe('MarkdownContent — file-path links', () => {
  it('renders a Windows-path link as an inert chip, not an anchor', () => {
    // Greek filename: micromark percent-encodes it — the chip must show the
    // decoded path
    renderMd('[Άνοιγμα αρχείου](C:\\Users\\amour\\OtoDock\\αρχείο.xlsx)')
    expect(screen.queryByRole('link')).toBeNull()
    const chip = screen.getByTitle('Local file path — ask for a preview or download link')
    expect(chip.tagName).toBe('SPAN')
    expect(chip).not.toHaveAttribute('href')
    expect(chip.textContent).toContain('C:\\Users\\amour\\OtoDock\\αρχείο.xlsx')
    expect(chip.className).not.toContain('cursor-pointer')
  })

  it('keeps an https link as a working anchor', () => {
    renderMd('[docs](https://example.com/docs)')
    const link = screen.getByRole('link', { name: /docs/ })
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', 'https://example.com/docs')
  })
})

// ─── Clickable chips — ChatFileContext present (the chat message list) ───────

const jsonRes = (body: unknown) => ({ ok: true, json: async () => body }) as Response

// found:true payload of POST /v1/chats/{id}/resolve-path
const RESOLVED = {
  found: true,
  agent: 'helper',
  path: 'workspace/report.xlsx',
  filename: 'report.xlsx',
  size: 123,
  previewable: true,
}

// NOTE: resolveChatPath keeps a module-level positive cache keyed by
// `${chatId}\n${path}` — every test uses its own chatId so no test sees
// another test's cache entries.
function renderChatMd(md: string, chatId: string) {
  return render(
    <SearchProvider query="">
      <ChatFileProvider chatId={chatId} agent="helper">
        <MarkdownContent>{md}</MarkdownContent>
      </ChatFileProvider>
    </SearchProvider>,
  )
}

describe('MarkdownContent — clickable file chips (chat context)', () => {
  // Braces matter: a function RETURNED from a vitest hook runs as teardown —
  // returning the mock from mockReset() would invoke apiFetch(undefined).
  beforeEach(() => { fetchSpy.mockReset() })
  afterEach(() => { vi.useRealTimers() })

  it('renders the chip as a button with the preview affordance', () => {
    renderChatMd('[open](/workspace/report.xlsx)', 'chat-a')
    const chip = screen.getByTitle('Open file preview')
    expect(chip.tagName).toBe('BUTTON')
    expect(chip.className).toContain('cursor-pointer')
    expect(chip.textContent).toContain('/workspace/report.xlsx')
    expect(chip.textContent).toContain('↗')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('click POSTs the raw href and opens the preview on found:true', async () => {
    fetchSpy.mockResolvedValue(jsonRes(RESOLVED))
    renderChatMd('[open](/workspace/report.xlsx)', 'chat-b')
    fireEvent.click(screen.getByTitle('Open file preview'))
    await waitFor(() => expect(screen.getByTestId('file-preview-body')).toBeInTheDocument())
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(fetchSpy).toHaveBeenCalledWith('/v1/chats/chat-b/resolve-path', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ path: '/workspace/report.xlsx' }),
    }))
  })

  it('POSTs the decoded path for percent-encoded (Windows/unicode) hrefs', async () => {
    // micromark encodes '\' as %5C and Greek as %CE%.. — the backend never
    // URL-decodes, so the chip must POST the decoded form it displays.
    fetchSpy.mockResolvedValue(jsonRes({ found: false }))
    renderChatMd('[open](C:\\Users\\amour\\αρχείο.xlsx)', 'chat-c')
    await act(async () => { fireEvent.click(screen.getByTitle('Open file preview')) })
    const [, options] = fetchSpy.mock.calls[0]
    expect(JSON.parse((options as RequestInit).body as string))
      .toEqual({ path: 'C:\\Users\\amour\\αρχείο.xlsx' })
  })

  it('found:false shows a transient not-found state and does not open', async () => {
    vi.useFakeTimers()
    fetchSpy.mockResolvedValue(jsonRes({ found: false }))
    renderChatMd('[open](/workspace/missing.xlsx)', 'chat-d')
    await act(async () => { fireEvent.click(screen.getByTitle('Open file preview')) })
    const chip = screen.getByTitle("File not found in the agent's workspace")
    expect(chip.textContent).toContain('not found')
    expect(screen.queryByTestId('file-preview-body')).toBeNull()
    // …and reverts after a moment (no toast, no sticky error)
    act(() => { vi.advanceTimersByTime(3000) })
    expect(screen.getByTitle('Open file preview')).toBeTruthy()
  })

  it('negative results are not cached — a second click re-resolves', async () => {
    fetchSpy.mockResolvedValue(jsonRes({ found: false }))
    renderChatMd('[open](/workspace/latent.xlsx)', 'chat-e')
    const chip = screen.getByRole('button')
    await act(async () => { fireEvent.click(chip) })
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    await act(async () => { fireEvent.click(chip) })
    expect(fetchSpy).toHaveBeenCalledTimes(2)
  })

  it('positive results are cached — two clicks, one fetch', async () => {
    fetchSpy.mockResolvedValue(jsonRes(RESOLVED))
    renderChatMd('[open](/workspace/report.xlsx)', 'chat-f')
    const chip = screen.getByTitle('Open file preview')
    await act(async () => { fireEvent.click(chip) })
    expect(screen.getByTestId('file-preview-body')).toBeInTheDocument()
    await act(async () => { fireEvent.click(chip) })
    expect(screen.getByTestId('file-preview-body')).toBeInTheDocument()
    expect(fetchSpy).toHaveBeenCalledTimes(1)
  })

  it('a fetch error shows the same transient not-found state', async () => {
    fetchSpy.mockRejectedValue(new Error('network down'))
    renderChatMd('[open](/workspace/err.xlsx)', 'chat-g')
    await act(async () => { fireEvent.click(screen.getByTitle('Open file preview')) })
    expect(screen.getByTitle("File not found in the agent's workspace")).toBeTruthy()
    expect(screen.queryByTestId('file-preview-body')).toBeNull()
  })
})
