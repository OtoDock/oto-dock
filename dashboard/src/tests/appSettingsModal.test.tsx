/**
 * App Settings → Installations (the Android app's switcher) speaks to the app
 * over the origin-scoped channel: the list is the app's
 * answer, every action posts its method, and a refresh after a change asks
 * after the change was posted (the app runs channel calls in order).
 */
import { afterEach, describe, expect, it } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import AppSettingsModal from '@/components/chat/AppSettingsModal'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

const INSTALLS = [
  { id: 'i1', url: 'https://agents.example.com', label: 'Office', favorite: true, active: true },
  { id: 'i2', url: 'https://site.example.com', label: 'Site', favorite: false, active: false },
]

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

afterEach(() => {
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
})

describe('AppSettingsModal in the app', () => {
  it('lists what the app answers', async () => {
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    expect(ch.methods()).toEqual(['getInstallations'])
    expect(screen.getByText('Office')).toBeTruthy()
    expect(screen.getByText('Site')).toBeTruthy()
  })

  it('posts each action on the channel, and refreshes after the change', async () => {
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Set as default' }))
    await settle()
    expect(ch.posted.slice(1).map((p) => [p.m, p.a])).toEqual([
      ['setFavorite', ['i2']],
      ['getInstallations', []],
    ])
    fireEvent.click(screen.getByText('Site'))
    await settle()
    expect(ch.posted[ch.posted.length - 1]).toMatchObject({ m: 'switchInstallation', a: ['i2'] })
  })

  it('keeps the list when the app does not answer', async () => {
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    // The next refresh goes unanswered: the list stays instead of emptying.
    delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
    fireEvent.click(screen.getByRole('button', { name: 'Set as default' }))
    await settle()
    expect(screen.getByText('Site')).toBeTruthy()
    expect(ch.methods()).toEqual(['getInstallations'])
  })
})
