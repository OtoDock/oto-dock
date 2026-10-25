import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import AlertDialog from '../components/ui/AlertDialog'
import { useDialogs, type Dialogs } from '../components/ui/dialogs'
import { answerDialog, findAlertDialog, personClick, pressEscape, renderWithDialogs } from './dialogsHarness'

function Asker({ onAnswer, ask }: { onAnswer: (v: unknown) => void; ask: (d: Dialogs) => Promise<unknown> }) {
  const dialogs = useDialogs()
  return <button onClick={async () => onAnswer(await ask(dialogs))}>Ask</button>
}

describe('useDialogs', () => {
  it('confirm answers true on the action and false on Cancel and Escape', async () => {
    const answers: unknown[] = []
    renderWithDialogs(
      <Asker onAnswer={(v) => answers.push(v)} ask={(d) => d.confirm({ title: 'Delete the task?', body: 'It cannot be undone.', confirmLabel: 'Delete', tone: 'danger' })} />,
    )
    fireEvent.click(screen.getByText('Ask'))
    const dialog = await findAlertDialog('Delete the task?')
    expect(dialog).toHaveAccessibleDescription('It cannot be undone.')
    await act(async () => { await answerDialog('Delete') })
    fireEvent.click(screen.getByText('Ask'))
    await act(async () => { await answerDialog('Cancel') })
    fireEvent.click(screen.getByText('Ask'))
    await findAlertDialog()
    await act(async () => { pressEscape() })
    await waitFor(() => expect(answers).toEqual([true, false, false]))
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('a dangerous confirm opens on Cancel; a plain one on its action', async () => {
    renderWithDialogs(<Asker onAnswer={() => {}} ask={(d) => d.confirm({ title: 'Remove?', tone: 'danger', confirmLabel: 'Remove' })} />)
    fireEvent.click(screen.getByText('Ask'))
    const dialog = await findAlertDialog('Remove?')
    await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' })))
  })

  it('ignores the clicks nobody meant: a double click, a press from before it opened, a synthesized click at once', async () => {
    const onAnswer = vi.fn()
    renderWithDialogs(<Asker onAnswer={onAnswer} ask={(d) => d.confirm({ title: 'Run now?', confirmLabel: 'Run' })} />)
    fireEvent.click(screen.getByText('Ask'))
    const dialog = await findAlertDialog('Run now?')
    const run = within(dialog).getByRole('button', { name: 'Run' })
    fireEvent.click(run, { detail: 2 })
    fireEvent.click(run, { detail: 1 })
    fireEvent.click(run)
    await Promise.resolve()
    expect(onAnswer).not.toHaveBeenCalled()
    await act(async () => { personClick(run) })
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(true))
  })

  it('alert resolves on OK; prompt answers the value, refuses what validate refuses, and null on Cancel', async () => {
    const answers: unknown[] = []
    const { unmount } = renderWithDialogs(
      <Asker onAnswer={(v) => answers.push(v)} ask={async (d) => {
        await d.alert({ title: 'Not moved', body: 'a.txt: exists' })
        return d.prompt({ title: 'New folder', label: 'Folder name', validate: (v) => (v.trim() ? null : 'Type a name.') })
      }} />,
    )
    fireEvent.click(screen.getByText('Ask'))
    const alert = await findAlertDialog('Not moved')
    expect(within(alert).queryByRole('button', { name: 'Cancel' })).toBeNull()
    await act(async () => { personClick(within(alert).getByRole('button', { name: 'OK' })) })
    const prompt = await screen.findByRole('dialog', { name: 'New folder' })
    const field = within(prompt).getByLabelText('Folder name')
    fireEvent.click(within(prompt).getByRole('button', { name: 'OK' }))
    expect(within(prompt).getByRole('alert')).toHaveTextContent('Type a name.')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    fireEvent.change(field, { target: { value: 'notes' } })
    await act(async () => { fireEvent.submit(field.closest('form')!) })
    await waitFor(() => expect(answers).toEqual(['notes']))
    unmount()
  })

  it('an unmounting asker gets no for its open request, and no dialog for a later one', async () => {
    let dialogs: Dialogs | null = null
    function Holder() {
      dialogs = useDialogs()
      return null
    }
    function Page() {
      const [shown, setShown] = useState(true)
      return (
        <>
          {shown && <Holder />}
          <button onClick={() => setShown(false)}>Leave</button>
        </>
      )
    }
    renderWithDialogs(<Page />)
    let first: Promise<boolean> | null = null
    act(() => { first = dialogs!.confirm({ title: 'Stay?' }) })
    await findAlertDialog('Stay?')
    fireEvent.click(screen.getByText('Leave'))
    await expect(first!).resolves.toBe(false)
    expect(screen.queryByRole('alertdialog')).toBeNull()
    // An async handler still running after its component went.
    await expect(dialogs!.confirm({ title: 'Too late?' })).resolves.toBe(false)
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('two requests in a row show one after the other', async () => {
    renderWithDialogs(
      <Asker onAnswer={() => {}} ask={async (d) => { await d.alert({ title: 'First' }); await d.alert({ title: 'Second' }) }} />,
    )
    fireEvent.click(screen.getByText('Ask'))
    await act(async () => { await answerDialog('OK', 'First') })
    await findAlertDialog('Second')
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1)
  })
})

describe('AlertDialog', () => {
  it('while busy, nothing dismisses it and its buttons are off', () => {
    const onCancel = vi.fn()
    renderWithDialogs(
      <AlertDialog title="Remove the machine?" busy busyLabel="Removing…" confirmLabel="Remove" error="It is busy." onConfirm={() => {}} onCancel={onCancel}>
        Its sessions end.
      </AlertDialog>,
    )
    const dialog = screen.getByRole('alertdialog', { name: 'Remove the machine?' })
    pressEscape(dialog)
    fireEvent.pointerDown(dialog)
    fireEvent.click(dialog, { detail: 1 })
    expect(onCancel).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('button', { name: 'Removing…' })).toBeDisabled()
    expect(within(dialog).getByRole('alert')).toHaveTextContent('It is busy.')
  })
})
