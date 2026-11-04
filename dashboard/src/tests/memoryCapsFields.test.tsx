/**
 * Setup → System Settings → Memory: the two size caps keep a draft and save
 * when the field is left, each with its own key; a value outside the
 * server's range never goes out and the field goes back to the saved value.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { MemoryCapsFields } from '@/pages/admin/SystemSettingsTab'

function fields() {
  const mutateAsync = vi.fn(() => Promise.resolve())
  const run = vi.fn(async (_key: string, save: () => Promise<unknown>) => { await save(); return true })
  render(
    <MemoryCapsFields
      memory={{ topic_grow_cap_bytes: 32768, entry_cap_bytes: 2048 }}
      run={run}
      stateOf={() => null}
      saveMemory={{ mutateAsync }}
    />,
  )
  return { mutateAsync, run }
}

describe('Memory caps fields', () => {
  it('shows both caps and saves each under its own key when the field is left', async () => {
    const { mutateAsync, run } = fields()
    const grow = screen.getByLabelText('Growth cap in bytes') as HTMLInputElement
    const entry = screen.getByLabelText('Entry cap in bytes') as HTMLInputElement
    expect(grow.value).toBe('32768')
    expect(entry.value).toBe('2048')
    fireEvent.change(grow, { target: { value: '40960' } })
    expect(mutateAsync).not.toHaveBeenCalled()
    fireEvent.blur(grow)
    await Promise.resolve()
    expect(run).toHaveBeenCalledWith('topic_grow_cap_bytes', expect.any(Function), expect.objectContaining({ failTitle: 'The memory setting was not saved' }))
    expect(mutateAsync).toHaveBeenCalledWith({ topic_grow_cap_bytes: 40960 })
    fireEvent.change(entry, { target: { value: '4096' } })
    fireEvent.blur(entry)
    await Promise.resolve()
    expect(mutateAsync).toHaveBeenCalledWith({ entry_cap_bytes: 4096 })
  })

  it('never sends a value outside the range, puts the saved one back and says so', () => {
    const { mutateAsync } = fields()
    const grow = screen.getByLabelText('Growth cap in bytes') as HTMLInputElement
    expect(screen.getByText('From 16,384 to 65,536.')).toBeTruthy()
    expect(screen.getByText('From 256 to 65,536.')).toBeTruthy()
    fireEvent.change(grow, { target: { value: '4096' } })
    fireEvent.blur(grow)
    expect(mutateAsync).not.toHaveBeenCalled()
    expect(grow.value).toBe('32768')
    expect(screen.getByText('Outside the range, set back to 32,768.')).toBeTruthy()
    expect(screen.queryByText('From 16,384 to 65,536.')).toBeNull()
    // The next keystroke clears the note and the range shows again.
    fireEvent.change(grow, { target: { value: '4' } })
    expect(screen.getByText('From 16,384 to 65,536.')).toBeTruthy()
    fireEvent.change(grow, { target: { value: '' } })
    fireEvent.blur(grow)
    expect(mutateAsync).not.toHaveBeenCalled()
    expect(grow.value).toBe('32768')
    expect(screen.getByText('Outside the range, set back to 32,768.')).toBeTruthy()
  })

  it('does not save an unchanged value', () => {
    const { mutateAsync } = fields()
    const entry = screen.getByLabelText('Entry cap in bytes')
    fireEvent.blur(entry)
    expect(mutateAsync).not.toHaveBeenCalled()
  })
})
