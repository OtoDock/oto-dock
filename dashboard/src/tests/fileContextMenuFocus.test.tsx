// An item of the workspace's file menu that opens a dialog (the delete
// confirm) gives the focus back to where it was when the menu opened (the
// tile's 3-dot button), so the dialog returns it there when it closes.
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { expect, it } from 'vitest'

import Dialog from '@/components/ui/Dialog'
import FileContextMenu from '@/components/workspace/FileContextMenu'
import { pressEscape } from './dialogsHarness'

function Host() {
  const [menu, setMenu] = useState(false)
  const [asking, setAsking] = useState(false)
  return (
    <>
      <button onClick={() => setMenu(true)}>More</button>
      {menu && (
        <FileContextMenu
          x={10}
          y={10}
          onClose={() => setMenu(false)}
          actions={[{ key: 'delete', label: 'Delete', tone: 'danger', icon: null, onClick: () => setAsking(true) }]}
        />
      )}
      {asking && <Dialog aria-label="Confirm delete" onClose={() => setAsking(false)}><button>Cancel</button></Dialog>}
    </>
  )
}

it("the delete confirm opened from the file menu returns the focus to the tile's button", () => {
  render(<Host />)
  const more = screen.getByText('More')
  more.focus()
  fireEvent.click(more)
  const item = screen.getByText('Delete').closest('button')!
  item.focus()
  fireEvent.click(item)
  expect(screen.getByRole('dialog', { name: 'Confirm delete' })).toBeTruthy()
  pressEscape()
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(more)
})
