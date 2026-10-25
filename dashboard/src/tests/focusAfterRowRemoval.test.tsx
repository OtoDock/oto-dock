import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import Dialog from '../components/ui/Dialog'
import { useDialogs } from '../components/ui/dialogs'
import { answerDialog, personClick, renderWithDialogs } from './dialogsHarness'

// A confirm that deletes the row which opened it: the deletion lands after
// the dialog has closed and given the focus back to the row's button, so
// the focus follows to the next row of the same list (the previous when it
// was the last, the list's label when none is left), never into another
// list, and never over a focus the person moved themselves.

/** Let the deletion land and the focus watch run. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 5))
  })
}

function Triggers({ initial, title = 'Triggers', later = true, dropsButtonOnly = false }: {
  initial: string[]; title?: string; later?: boolean; dropsButtonOnly?: boolean
}) {
  const [rows, setRows] = useState(initial)
  const [done, setDone] = useState<string[]>([])
  const [asking, setAsking] = useState<string | null>(null)
  const remove = (id: string) => {
    const apply = () => (dropsButtonOnly ? setDone((d) => [...d, id]) : setRows((r) => r.filter((x) => x !== id)))
    if (later) setTimeout(apply, 0)
    else apply()
  }
  const headingId = `${title}-title`
  return (
    <section aria-labelledby={headingId}>
      <h2 id={headingId}>{title}</h2>
      {rows.length ? (
        <div className="space-y-2">
          {rows.map((id) => (
            <div key={id} className="trigger-row flex items-center gap-2">
              <span>{id} trigger</span>
              <button className="icon-btn p-1">Pause {id}</button>
              {!done.includes(id) && (
                <button className="icon-btn p-1" onClick={() => setAsking(id)}>Delete {id}</button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p>No {title.toLowerCase()} yet.</p>
      )}
      {asking && (
        <Dialog aria-label="Delete" role="alertdialog" onClose={() => setAsking(null)}>
          <button onClick={() => { const id = asking; setAsking(null); remove(id) }}>Confirm</button>
          <button onClick={() => setAsking(null)}>Cancel</button>
        </Dialog>
      )}
    </section>
  )
}

function askAndConfirm(id: string): void {
  const opener = screen.getByRole('button', { name: `Delete ${id}` })
  opener.focus()
  personClick(opener)
  personClick(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Confirm' }))
}

describe('the focus after a confirm removes the row that opened it', () => {
  it('lands on the next row when the middle one goes', async () => {
    render(<Triggers initial={['a', 'b', 'c']} />)
    askAndConfirm('b')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete b' }))
    await settle()
    expect(screen.queryByText('b trigger')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Pause c' }))
  })

  it('lands on the previous row when the last one goes', async () => {
    render(<Triggers initial={['a', 'b', 'c']} />)
    askAndConfirm('c')
    await settle()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Pause b' }))
  })

  it("lands on the list's label when the only row goes and an empty state replaces the list", async () => {
    render(<Triggers initial={['a']} />)
    askAndConfirm('a')
    await settle()
    const heading = screen.getByRole('heading', { name: 'Triggers' })
    expect(document.activeElement).toBe(heading)
    expect(heading.getAttribute('tabindex')).toBe('-1')
    heading.blur()
    expect(heading.hasAttribute('tabindex')).toBe(false)
  })

  it('moves at once when the row went before the dialog closed', async () => {
    render(<Triggers initial={['a', 'b', 'c']} later={false} />)
    askAndConfirm('a')
    await settle()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Pause b' }))
  })

  it("stays in the row when only its button went (a Revoke that keeps the row)", async () => {
    render(<Triggers initial={['a', 'b']} dropsButtonOnly />)
    askAndConfirm('a')
    await settle()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Pause a' }))
  })

  it('keeps the opener on Cancel, whatever the list does next', async () => {
    render(<Triggers initial={['a', 'b']} />)
    const opener = screen.getByRole('button', { name: 'Delete a' })
    opener.focus()
    personClick(opener)
    personClick(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    await settle()
    expect(document.activeElement).toBe(opener)
  })

  it('never takes back a focus the person moved, or a click they made, before the deletion', async () => {
    render(<><Triggers initial={['a', 'b']} /><button>Elsewhere</button></>)
    askAndConfirm('a')
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()
    await settle()
    expect(document.activeElement).toBe(elsewhere)
  })

  it('never takes back the body after a click on blank space', async () => {
    render(<Triggers initial={['a', 'b']} />)
    askAndConfirm('a')
    fireEvent.pointerDown(document.body)
    ;(document.activeElement as HTMLElement).blur()
    await settle()
    expect(document.activeElement).toBe(document.body)
  })

  it('never moves into another list when its own empties without a label', async () => {
    function Two() {
      return (
        <main>
          <div className="space-y-3">
            <Rowless />
          </div>
          <div className="space-y-3">
            <Triggers initial={['x', 'y']} title="Others" />
          </div>
        </main>
      )
    }
    function Rowless() {
      const [rows, setRows] = useState(['a'])
      const [asking, setAsking] = useState(false)
      return (
        <div>
          {rows.length ? (
            <div className="space-y-2">
              {rows.map((id) => (
                <div key={id} className="key-row flex">
                  <span>{id} key</span>
                  <button onClick={() => setAsking(true)}>Delete {id}</button>
                </div>
              ))}
              <div className="key-row flex"><span>spare</span></div>
            </div>
          ) : <p>No keys.</p>}
          {asking && (
            <Dialog aria-label="Delete" role="alertdialog" onClose={() => setAsking(false)}>
              <button onClick={() => { setAsking(false); setTimeout(() => setRows([]), 0) }}>Confirm</button>
            </Dialog>
          )}
        </div>
      )
    }
    render(<Two />)
    askAndConfirm('a')
    await settle()
    const others = screen.getByRole('region', { name: 'Others' })
    expect(others.contains(document.activeElement)).toBe(false)
  })

  it("never moves into the next card when a card's last row goes with the card", async () => {
    function Cards() {
      const [keys, setKeys] = useState(['k1'])
      const [asking, setAsking] = useState(false)
      return (
        <div>
          {keys.length > 0 && (
            <div className="rounded-lg border p-4">
              <h3>API keys</h3>
              <div className="space-y-2">
                {keys.map((k) => <div key={k}><span>{k}</span><button onClick={() => setAsking(true)}>Revoke {k}</button></div>)}
              </div>
            </div>
          )}
          <div className="rounded-lg border p-4">
            <h3>Webhooks</h3>
            <button>Add a webhook</button>
          </div>
          {asking && (
            <Dialog aria-label="Revoke" role="alertdialog" onClose={() => setAsking(false)}>
              <button onClick={() => { setAsking(false); setTimeout(() => setKeys([]), 0) }}>Confirm</button>
            </Dialog>
          )}
        </div>
      )
    }
    render(<Cards />)
    const opener = screen.getByRole('button', { name: 'Revoke k1' })
    opener.focus()
    personClick(opener)
    personClick(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Confirm' }))
    await settle()
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Add a webhook' }))
  })

  it('treats bare wrapper divs as no rows', async () => {
    function Panel() {
      const [shown, setShown] = useState(true)
      const [asking, setAsking] = useState(false)
      return (
        <div>
          {shown && <div><button onClick={() => setAsking(true)}>Delete it</button></div>}
          <div><button>Unrelated</button></div>
          {asking && (
            <Dialog aria-label="Delete" role="alertdialog" onClose={() => setAsking(false)}>
              <button onClick={() => { setAsking(false); setTimeout(() => setShown(false), 0) }}>Confirm</button>
            </Dialog>
          )}
        </div>
      )
    }
    render(<Panel />)
    askAndConfirm('it')
    await settle()
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Unrelated' }))
  })

  it("goes to the next group's first row when a date group empties and goes", async () => {
    function Grouped() {
      const [chats, setChats] = useState([['Today', 'a'], ['Earlier', 'b'], ['Earlier', 'c']])
      const [asking, setAsking] = useState<string | null>(null)
      const groups = [...new Set(chats.map(([g]) => g))]
      return (
        <div>
          {groups.map((g) => (
            <div key={g} className="mb-3">
              <p className="label">{g}</p>
              {chats.filter(([cg]) => cg === g).map(([, id]) => (
                <div key={id} className="chat-row flex px-3">
                  <span>{id} chat</span>
                  <button className="kebab" onClick={() => setAsking(id)}>Delete {id}</button>
                </div>
              ))}
            </div>
          ))}
          {asking && (
            <Dialog aria-label="Delete" role="alertdialog" onClose={() => setAsking(null)}>
              <button onClick={() => {
                const id = asking
                setAsking(null)
                setTimeout(() => setChats((c) => c.filter(([, x]) => x !== id)), 0)
              }}>Confirm</button>
            </Dialog>
          )}
        </div>
      )
    }
    render(<Grouped />)
    askAndConfirm('a')
    await settle()
    expect(screen.queryByText('Today')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete b' }))
  })

  it('follows a row whose own confirm unmounts with it (the chat menu)', async () => {
    function Row({ id, onDelete }: { id: string; onDelete: (id: string) => void }) {
      const [asking, setAsking] = useState(false)
      return (
        <div className="chat-row flex">
          <span>{id} chat</span>
          <button onClick={() => setAsking(true)}>Delete {id}</button>
          {asking && (
            <Dialog aria-label="Delete" role="alertdialog" onClose={() => setAsking(false)}>
              <button onClick={() => { setAsking(false); onDelete(id) }}>Confirm</button>
            </Dialog>
          )}
        </div>
      )
    }
    function List() {
      const [rows, setRows] = useState(['a', 'b'])
      return <div>{rows.map((id) => <Row key={id} id={id} onDelete={(x) => setRows((r) => r.filter((y) => y !== x))} />)}</div>
    }
    render(<List />)
    askAndConfirm('a')
    await settle()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete b' }))
  })

  it('leaves a full-screen viewer as it was: only a dialog follows the row', async () => {
    const { useReturnFocus } = await import('../components/ui/focusReturn')
    function Viewer({ onClose }: { onClose: () => void }) {
      useReturnFocus(true)
      return <div><button onClick={onClose}>Close viewer</button></div>
    }
    function Messages() {
      const [rows, setRows] = useState(['m1', 'm2'])
      const [viewing, setViewing] = useState(false)
      return (
        <div>
          {rows.map((id) => (
            <div key={id} className="message-row flex">
              <span>{id}</span>
              <button onClick={() => setViewing(true)}>Open {id}</button>
              <button>Retry {id}</button>
            </div>
          ))}
          {viewing && <Viewer onClose={() => { setViewing(false); setTimeout(() => setRows(['m2']), 0) }} />}
        </div>
      )
    }
    render(<Messages />)
    const opener = screen.getByRole('button', { name: 'Open m1' })
    opener.focus()
    fireEvent.click(opener)
    fireEvent.click(screen.getByRole('button', { name: 'Close viewer' }))
    await settle()
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Open m2' }))
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: 'Retry m2' }))
  })

  it('works the same for a confirm asked through the dialogs host', async () => {
    function Hosted() {
      const dialogs = useDialogs()
      const [rows, setRows] = useState(['a', 'b'])
      const ask = async (id: string) => {
        if (await dialogs.confirm({ title: 'Delete the trigger?', confirmLabel: 'Delete', tone: 'danger' })) {
          setRows((r) => r.filter((x) => x !== id))
        }
      }
      return (
        <div>
          {rows.map((id) => (
            <div key={id} className="trigger-row flex">
              <span>{id} trigger</span>
              <button onClick={() => { void ask(id) }}>Remove {id}</button>
            </div>
          ))}
        </div>
      )
    }
    renderWithDialogs(<Hosted />)
    const opener = screen.getByRole('button', { name: 'Remove a' })
    opener.focus()
    personClick(opener)
    await answerDialog('Delete')
    await settle()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Remove b' }))
  })
})
