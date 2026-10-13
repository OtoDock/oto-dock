// The transfer popup's Cancel: offered for a workspace upload batch while a
// row of it still uploads, it aborts the batch; gone once the batch ends.

import { describe, it, expect, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import TransferPopup from '@/components/workspace/TransferPopup'
import { registerBatch, useTransferStore } from '@/store/transferStore'

beforeEach(() => useTransferStore.setState({ byId: {} }))

describe('TransferPopup Cancel', () => {
  it('cancels a running batch and is gone once the batch ends', () => {
    const abort = new AbortController()
    const end = registerBatch('b1', abort)
    const s = useTransferStore.getState()
    s.beginLocalUpload({ clientId: 'l1', agent: 'ag', targetDir: 'workspace', filename: 'mesitis/a.md', bytesTotal: 3, batchId: 'b1' })
    s.beginLocalUpload({ clientId: 'l2', agent: 'ag', targetDir: 'workspace', filename: 'loose.md', bytesTotal: 3 })
    const items = () => Object.values(useTransferStore.getState().byId)
    const { rerender } = render(<TransferPopup items={items()} anchor={{ left: 0, top: 0 }} onClose={() => {}} />)
    expect(screen.getByText('Uploading mesitis')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(abort.signal.aborted).toBe(true)

    useTransferStore.getState().failLocalUpload('l1', 'cancelled')
    end()
    rerender(<TransferPopup items={items()} anchor={{ left: 0, top: 0 }} onClose={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
  })
})
