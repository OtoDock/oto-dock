import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import Button from '../components/ui/Button'
import Dialog from '../components/ui/Dialog'
import { BUTTON_VARIANT, CARD, CARD_BORDERED, FIELD, SEGMENT } from '../components/ui/classes'
import { Input, Label, Select } from '../components/ui/Field'
import { CheckboxRow, SaveState, SectionCard, SettingRow } from '../components/ui/SettingsControls'
import Switch from '../components/ui/Switch'
import ToastViewport, { ToastPortal } from '../components/ui/Toast'
import Tooltip from '../components/ui/Tooltip'
import { pressEscape } from './dialogsHarness'

afterEach(() => {
  vi.useRealTimers()
})

describe('Button', () => {
  it('is a plain button unless told to submit, so a form is not sent by accident', () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <Button>Cancel</Button>
        <Button type="submit" variant="primary">Save</Button>
      </form>,
    )
    expect(screen.getByText('Cancel')).toHaveAttribute('type', 'button')
    fireEvent.click(screen.getByText('Cancel'))
    expect(onSubmit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Save'))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })
})

describe('the quiet button, the item and the segment', () => {
  it('the secondary is a soft fill with an edge and primary text, its hover laid over the fill', () => {
    const secondary = BUTTON_VARIANT.secondary.split(' ')
    expect(secondary).toEqual(expect.arrayContaining(['bg-surface-muted', 'border', 'border-default', 'text-primary']))
    expect(secondary).toEqual(expect.arrayContaining(['hover:from-surface-hover', 'hover:to-surface-hover']))
    // A phone has no hover: the press shows the same layer.
    expect(secondary).toEqual(expect.arrayContaining(['active:from-surface-hover', 'active:to-surface-hover']))
    // Replacing the fill with the translucent hover turns it lighter in light.
    expect(secondary).not.toContain('hover:bg-surface-hover')
    expect(secondary).not.toContain('text-secondary')
  })

  it('a Button is the secondary unless told otherwise', () => {
    render(<Button>Create Agent</Button>)
    expect(screen.getByRole('button', { name: 'Create Agent' }).className).toContain(BUTTON_VARIANT.secondary)
  })

  it('an item inside a group sits on the page colour with a hairline, the group is the raised card', () => {
    expect(CARD_BORDERED.split(' ')).toEqual(expect.arrayContaining(['bg-surface-page', 'border', 'border-subtle']))
    expect(CARD_BORDERED).not.toContain('bg-surface-card')
    expect(CARD).toContain('bg-surface-card')
    expect(CARD).toContain('shadow-elevation-1')
  })

  it('a segment is the secondary, the chosen one the action fill with an edge of its own colour', () => {
    expect(SEGMENT.off).toBe(BUTTON_VARIANT.secondary)
    expect(SEGMENT.on.split(' ')).toEqual(expect.arrayContaining(['border', 'border-action', 'bg-action', 'text-action-fg']))
    // A forced-colour mode drops the fill: the chosen one takes the system's
    // selection colours, or it would be the same frame as the others.
    expect(SEGMENT.on.split(' ')).toEqual(expect.arrayContaining([
      'forced-colors:forced-color-adjust-none',
      'forced-colors:bg-[Highlight]', 'forced-colors:text-[HighlightText]', 'forced-colors:border-[Highlight]',
    ]))
  })

  it('a SectionCard is a group whose toggle reports whether it is open', () => {
    render(<SectionCard title="Voices"><p>The voice list</p></SectionCard>)
    const toggle = screen.getByRole('button', { name: /Voices/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('The voice list')).toBeInTheDocument()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('The voice list')).toBeNull()
  })
})

describe('Switch', () => {
  it('is a named switch that reports its state and toggles', () => {
    function Host() {
      const [on, setOn] = useState(false)
      return <Switch aria-label="Chat audio" checked={on} onChange={setOn} />
    }
    render(<Host />)
    const sw = screen.getByRole('switch', { name: 'Chat audio' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    expect(sw).toHaveAttribute('aria-checked', 'true')
  })
})

describe('Input, Label, Select', () => {
  it('a field clips its fill to the padding box so the translucent edge reads on every surface', () => {
    expect(FIELD).toContain('bg-clip-padding')
    expect(FIELD).toContain('bg-surface-card')
  })
  it('a label names its field; invalid marks it for readers', () => {
    render(
      <>
        <Label htmlFor="n">Name</Label>
        <Input id="n" invalid />
        <Label htmlFor="c">Kind</Label>
        <Select id="c" defaultValue="b"><option value="a">A</option><option value="b">B</option></Select>
      </>,
    )
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    expect((screen.getByLabelText('Kind') as HTMLSelectElement).tagName).toBe('SELECT')
    expect(screen.getByLabelText('Kind')).not.toHaveAttribute('aria-invalid')
  })
})

describe('Tooltip', () => {
  it('shows on hover after the delay and on focus, describes the control, and Escape hides it', () => {
    vi.useFakeTimers()
    render(<Tooltip text="Download the file"><button>⬇</button></Tooltip>)
    const button = screen.getByRole('button')
    fireEvent.mouseEnter(button.parentElement!)
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(300) })
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('Download the file')
    expect(button).toHaveAttribute('aria-describedby', tip.id)
    pressEscape(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(button).not.toHaveAttribute('aria-describedby')
    fireEvent.focus(button)
    expect(screen.getByRole('tooltip')).toBeTruthy()
    fireEvent.blur(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('with onlyWhenClipped, stays shut for a label that fits', () => {
    vi.useFakeTimers()
    render(<Tooltip text="Short" onlyWhenClipped className="truncate">Short</Tooltip>)
    fireEvent.mouseEnter(screen.getByText('Short'))
    act(() => { vi.advanceTimersByTime(300) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('the toast region', () => {
  it('stands empty, and moves into an open dialog so a toast raised there is seen', () => {
    function Page({ dialog }: { dialog: boolean }) {
      return (
        <>
          <ToastViewport />
          <ToastPortal><div role="status">Saved</div></ToastPortal>
          {dialog && <Dialog aria-label="Form" onClose={() => {}}><p>fields</p></Dialog>}
        </>
      )
    }
    const { rerender } = render(<Page dialog={false} />)
    const region = screen.getByRole('region', { name: 'Notifications' })
    expect(within(region).getByRole('status')).toHaveTextContent('Saved')
    expect(screen.queryByRole('dialog')).toBeNull()
    rerender(<Page dialog />)
    const dialog = screen.getByRole('dialog', { name: 'Form' })
    const moved = screen.getByRole('region', { name: 'Notifications' })
    expect(dialog.contains(moved)).toBe(true)
    expect(within(moved).getByRole('status')).toHaveTextContent('Saved')
  })
})

describe('the setting rows', () => {
  const TEXT = 'The longer description of the setting.'

  it('SettingRow keeps its hint visible and its longer text behind the button', () => {
    render(
      <SettingRow label="Remember callers" labelId="remember" description="Uses storage." info={TEXT}>
        <Switch aria-labelledby="remember" checked={false} onChange={() => {}} />
      </SettingRow>,
    )
    expect(screen.getByText('Uses storage.')).toBeTruthy()
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(screen.getByRole('switch', { name: 'Remember callers' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'About Remember callers' }), { detail: 0 })
    expect(screen.getByRole('tooltip').textContent).toBe(TEXT)
  })

  it('SettingRow without info has no button, and its label names the control it is for', () => {
    render(<SettingRow label="Language" htmlFor="lang" description="One at a time"><select id="lang" /></SettingRow>)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Language' })).toBeTruthy()
  })

  it('CheckboxRow: the label and the hint toggle it, the info button never does, the hint describes the box', () => {
    function Host() {
      const [on, setOn] = useState(true)
      return <CheckboxRow label="Subscription usage alerts" description="A short hint." info={TEXT} checked={on} onChange={setOn} />
    }
    render(<Host />)
    const box = screen.getByRole('checkbox', { name: 'Subscription usage alerts' })
    expect(box).toBeChecked()
    expect(box).toHaveAccessibleDescription('A short hint.')
    const info = screen.getByRole('button', { name: 'About Subscription usage alerts' })
    fireEvent.pointerDown(info, { pointerType: 'touch' })
    fireEvent.click(info, { detail: 1 })
    expect(screen.getByRole('tooltip').textContent).toBe(TEXT)
    expect(box).toBeChecked()
    fireEvent.click(info, { detail: 1 })
    expect(box).toBeChecked()
    fireEvent.click(screen.getByText('Subscription usage alerts'))
    expect(box).not.toBeChecked()
    // The label is stretched over the row: the browser sends a press on the
    // hint to it (its ::after covers the row), so the hint is part of it.
    const label = screen.getByText('Subscription usage alerts')
    expect(label.className).toContain('after:absolute')
    expect(label.className).toContain('after:inset-0')
    expect(label.closest('.relative')?.contains(screen.getByText('A short hint.'))).toBe(true)
  })

  it('SaveState is a status region, there and empty at rest, so its words are announced', () => {
    const { rerender } = render(<SaveState state={null} />)
    const region = screen.getByRole('status')
    expect(region.textContent).toBe('')
    rerender(<SaveState state="saving" />)
    expect(screen.getByRole('status')).toBe(region)
    expect(region).toHaveTextContent('Saving…')
    rerender(<SaveState state="saved" />)
    expect(region).toHaveTextContent('Saved')
    expect(region.className).not.toContain('animate')
    expect(region.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    // Beside a Save button that says Saving… itself, only Saved shows.
    rerender(<SaveState state="saving" besideButton />)
    expect(region.textContent).toBe('')
  })

  it('the rows show their save state after the label, outside it, so the control keeps its name', () => {
    render(
      <>
        <SettingRow label="Chat audio" labelId="chat-audio" save="saved">
          <Switch aria-labelledby="chat-audio" checked onChange={() => {}} />
        </SettingRow>
        <CheckboxRow label="Usage alerts" save="saved" checked onChange={() => {}} />
        <SettingRow label="Not wired"><span /></SettingRow>
      </>,
    )
    expect(screen.getAllByRole('status')).toHaveLength(2)
    expect(screen.getByRole('switch', { name: 'Chat audio' })).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: 'Usage alerts' })).toBeTruthy()
    for (const label of [screen.getByText('Chat audio'), screen.getByText('Usage alerts')]) {
      const status = label.parentElement!.querySelector('[role=status]')!
      expect(status).toHaveTextContent('Saved')
      expect(label.contains(status)).toBe(false)
      expect(label.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })
})
