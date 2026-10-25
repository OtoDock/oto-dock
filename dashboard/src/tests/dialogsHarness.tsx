// What a test needs around a component that asks through `useDialogs()`:
// the hosts App.tsx mounts (the dialogs: with none a request would wait
// forever; the toast region and its app-wide host) and clicks made
// the way a person makes them. The dialog buttons take a pointer click only
// when its press landed inside the open dialog, and a click with no detail
// (a reader's synthesized one) only after the opening moment; a bare
// `fireEvent.click` is that synthesized click.
import { fireEvent, render, screen, within, type RenderOptions } from '@testing-library/react'
import type { ReactElement } from 'react'

import AppToastHost from '../components/AppToastHost'
import DialogsHost from '../components/ui/DialogsHost'
import ToastViewport from '../components/ui/Toast'

export function renderWithDialogs(ui: ReactElement, options?: RenderOptions) {
  return render(
    <>
      {ui}
      <ToastViewport />
      <AppToastHost />
      <DialogsHost />
    </>,
    options,
  )
}

/** A person's click: the press, then a single click. */
export function personClick(el: Element): void {
  fireEvent.pointerDown(el)
  fireEvent.click(el, { detail: 1 })
}

/** The open alert dialog (a confirm, an alert), found by its title. */
export async function findAlertDialog(title?: string | RegExp): Promise<HTMLElement> {
  const all = await screen.findAllByRole('alertdialog')
  if (title === undefined) return all[all.length - 1]
  const match = all.filter((d) => within(d).queryByRole('heading', { name: title }))
  if (!match.length) throw new Error(`no alert dialog titled ${String(title)}`)
  return match[match.length - 1]
}

/** Answer the topmost alert dialog with the button of that name. */
export async function answerDialog(button: string | RegExp, title?: string | RegExp): Promise<void> {
  const dialog = await findAlertDialog(title)
  personClick(within(dialog).getByRole('button', { name: button }))
}

/** An Escape typed where the focus is, as the keyboard sends it. */
export function pressEscape(target: Element = document.activeElement ?? document.body): void {
  fireEvent.keyDown(target, { key: 'Escape' })
}
