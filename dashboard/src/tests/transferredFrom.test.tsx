import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TransferredFrom from '@/components/TransferredFrom'

describe('TransferredFrom', () => {
  it('names the first creator, with the date as its tooltip', () => {
    render(<TransferredFrom row={{ transferred_from: 'local:pat', transferred_at: '2026-09-27T07:51:00+00:00',
                                   transferred_from_name: 'Pat Person' }} />)
    const line = screen.getByTestId('transferred-from')
    expect(line.textContent).toBe('transferred from Pat Person')
    expect(line.getAttribute('title')).toMatch(/^Moved to its current owner on /)
  })

  it('falls back when the name could not be resolved, and shows nothing for a row that never moved', () => {
    const { rerender } = render(<TransferredFrom row={{ transferred_from: 'local:gone', transferred_from_name: '' }} />)
    expect(screen.getByTestId('transferred-from').textContent).toBe('transferred from a removed person')
    expect(screen.getByTestId('transferred-from').getAttribute('title')).toBeNull()
    rerender(<TransferredFrom row={{ transferred_from: '' }} />)
    expect(screen.queryByTestId('transferred-from')).toBeNull()
  })
})
