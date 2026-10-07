import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// A document push's card in the chat: the file, its version number and the
// push time; the newest card opens the pane on the live file, an older one
// its version read-only, an older one whose copy is gone is no button.

import DocumentCard from '@/components/chat/media/DocumentCard'
import { paneChat, useDocumentPaneStore, useDocumentPushStore } from '@/store/documentPaneStore'
import type { ChatDocument } from '@/api/documents'

const listing: ChatDocument[] = [
  {
    file_id: 'fa', filename: 'report.docx', download_url: '/v1/media/a', generation: 100,
    versions: [
      { snapshot_id: 'a2', message_id: 5, version: 2, generation: 100, turn: 2, available: true },
      { snapshot_id: 'a1', message_id: 3, version: 1, generation: 50, turn: 1, available: false },
    ],
  },
]

function stubFetch(docs: ChatDocument[] = listing) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ documents: docs }) })))
}

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  localStorage.clear()
  useDocumentPaneStore.setState({ owner: 'me', byChat: {} })
  useDocumentPushStore.setState({ pushed: {}, known: {}, dirty: {}, attended: {} })
})
afterEach(() => vi.unstubAllGlobals())

describe('DocumentCard', () => {
  it('the newest card opens Live; an older one its version; a gone one is no button', async () => {
    stubFetch()
    wrap(
      <>
        <DocumentCard chatId="c1" fileId="fa" filename="report.docx" downloadUrl="/d" snapshotId="a2" dbMessageId={5} generation={100} />
        <DocumentCard chatId="c1" fileId="fa" filename="report.docx" downloadUrl="/d" snapshotId="a1" dbMessageId={3} generation={50} />
        <DocumentCard chatId="c1" fileId="fb" filename="budget.xlsx" downloadUrl="/d" snapshotId="b0" version={4} generation={300} />
      </>,
    )
    await screen.findByText(/Version 2/)
    expect(screen.getByText(/Version 1, no longer available/)).toBeInTheDocument()
    expect(screen.getAllByRole('button')).toHaveLength(2)
    fireEvent.click(screen.getAllByTitle('Open the document')[0])
    await waitFor(() => expect(paneChat('c1')).toMatchObject({ open: true, active: 'fa', view: {} }))
    // A push the listing has not caught up with: its stamped number, Live.
    expect(screen.getByText(/Version 4/)).toBeInTheDocument()
  })

  it('an older available version opens read-only', async () => {
    stubFetch([{ ...listing[0], versions: listing[0].versions.map((v) => ({ ...v, available: true })) }])
    wrap(<DocumentCard chatId="c1" fileId="fa" filename="report.docx" downloadUrl="/d" snapshotId="a1" dbMessageId={3} generation={50} />)
    fireEvent.click(await screen.findByTitle('Open this version, read-only'))
    await waitFor(() => expect(paneChat('c1')).toMatchObject({ active: 'fa', view: { fa: 'a1' } }))
  })
})
