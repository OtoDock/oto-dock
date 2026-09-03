// ChatFilePreview — the file chip's preview surface. FilePreviewBody IS the
// modal (every branch renders its own FilePreviewPortal chrome), so this
// component is just the FileNode mapping plus one routing guard: a
// previewable:false Collabora document (e.g. knowledge/report.docx) must
// never reach the document branch, which would fire the doomed
// /v1/documents/wopi-url call — it gets the download-only portal instead.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatFilePreview from '@/components/chat/ChatFilePreview'
import type { ResolvedChatPath } from '@/api/chats'
import * as authApi from '@/api/auth'

vi.mock('@/components/workspace/FilePreviewBody', () => ({
  default: (props: {
    agent: string
    node: { path: string; name: string; type: string; size: number; modified: string }
    canWrite: boolean
    onClose: () => void
  }) => (
    <div
      data-testid="file-preview-body"
      data-agent={props.agent}
      data-canwrite={String(props.canWrite)}
      data-node={JSON.stringify(props.node)}
    >
      <button onClick={props.onClose}>mock-close</button>
    </div>
  ),
}))

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const resolved = (over: Partial<ResolvedChatPath> = {}): ResolvedChatPath => ({
  agent: 'helper',
  path: 'workspace/reports/q3.xlsx',
  filename: 'q3.xlsx',
  size: 2048,
  previewable: true,
  ...over,
})

// Braces matter: a function RETURNED from a vitest hook runs as teardown —
// returning the mock from mockReset() would invoke apiFetch(undefined).
beforeEach(() => { fetchSpy.mockReset() })

describe('ChatFilePreview', () => {
  it('maps the resolved payload onto the FileNode; canWrite is always false', () => {
    const onClose = vi.fn()
    render(<ChatFilePreview resolved={resolved()} onClose={onClose} />)
    const body = screen.getByTestId('file-preview-body')
    expect(body.dataset.agent).toBe('helper')
    expect(body.dataset.canwrite).toBe('false')
    expect(JSON.parse(body.dataset.node!)).toEqual({
      path: 'workspace/reports/q3.xlsx',
      name: 'q3.xlsx',
      type: 'file',
      size: 2048,
      modified: '', // required by FileNode; only name/path are read
    })
    // onClose is wired straight through
    fireEvent.click(screen.getByText('mock-close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('previewable:false document routes to download-only — no wopi/document branch', () => {
    const onClose = vi.fn()
    render(
      <ChatFilePreview
        resolved={resolved({ path: 'knowledge/report.docx', filename: 'report.docx', size: 10, previewable: false })}
        onClose={onClose}
      />,
    )
    // The document branch never mounts: no FilePreviewBody, no wopi-url fetch.
    expect(screen.queryByTestId('file-preview-body')).toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(screen.getByText(/Preview unavailable/)).toBeInTheDocument()
    expect(screen.getByTitle('Download')).toHaveAttribute(
      'href',
      '/v1/agents/helper/files/knowledge/report.docx?download=true&fn=report.docx',
    )
    // onClose is wired to the portal chrome's close button
    fireEvent.click(screen.getByTitle('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('previewable:false non-document (image) still routes through FilePreviewBody', () => {
    // Media/text/images resolve previewable:false too (not Collabora docs) —
    // their FilePreviewBody branches are path-based and role-gated, so they
    // are unaffected by the routing guard.
    render(
      <ChatFilePreview
        resolved={resolved({ path: 'workspace/pic.png', filename: 'pic.png', size: 5, previewable: false })}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByTestId('file-preview-body')).toBeInTheDocument()
  })
})
