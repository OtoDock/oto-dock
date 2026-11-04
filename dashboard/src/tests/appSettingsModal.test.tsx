/**
 * App Settings → Installations (the Android app's switcher) speaks to the app
 * over the origin-scoped channel: the list is the app's
 * answer, every action posts its method, and a refresh after a change asks
 * after the change was posted (the app runs channel calls in order).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

const h = vi.hoisted(() => ({ native: false, platform: 'web', opened: [] as string[] }))
vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: () => h.native, getPlatform: () => h.platform },
}))
vi.mock('@capacitor/browser', () => ({
  Browser: { open: async ({ url }: { url: string }) => { h.opened.push(url) } },
}))

import AppSettingsModal from '@/components/chat/AppSettingsModal'
import { APP_PRIVACY_POLICY_URL } from '@/lib/nativeBridge'
import { installFakeNativeChannel } from './fixtures/nativeChannel'

const INSTALLS = [
  { id: 'i1', url: 'https://agents.example.com', label: 'Office', favorite: true, active: true },
  { id: 'i2', url: 'https://site.example.com', label: 'Site', favorite: false, active: false },
]

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

afterEach(() => {
  delete (window as unknown as { OtoDockNative?: unknown }).OtoDockNative
  h.native = false
  h.platform = 'web'
  h.opened = []
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

describe('the privacy policy link', () => {
  it('opens the policy through the app inside the Android app', async () => {
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Privacy policy' }))
    expect(ch.posted[ch.posted.length - 1]).toMatchObject({ m: 'openPrivacyPolicy', a: [] })
  })

  it('opens it as an outside link in a shell without the channel', async () => {
    h.native = true
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Privacy policy' }))
    await settle()
    expect(h.opened).toEqual([APP_PRIVACY_POLICY_URL])
  })

  it('is not shown in a browser', async () => {
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    expect(screen.queryByRole('button', { name: 'Privacy policy' })).toBeNull()
  })
})

describe('the battery row', () => {
  it('shows in the Android app while the battery setting would help, and opens it', async () => {
    h.platform = 'android'
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS, getBatteryAdvice: true })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    expect(screen.getByText('Alarms on this phone')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Open battery settings' }))
    expect(ch.posted[ch.posted.length - 1]).toMatchObject({ m: 'openBatterySettings', a: [] })
  })

  it('stays away once the setting is right, and asks again on return to the page', async () => {
    h.platform = 'android'
    const answers: Record<string, unknown> = { getInstallations: INSTALLS, getBatteryAdvice: false }
    const ch = installFakeNativeChannel(answers)
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    expect(screen.queryByTestId('app-battery-row')).toBeNull()
    answers.getBatteryAdvice = true
    document.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(ch.methods().filter((m) => m === 'getBatteryAdvice')).toHaveLength(2)
    expect(screen.getByTestId('app-battery-row')).toBeTruthy()
  })

  it('is never asked for outside Android', async () => {
    h.platform = 'ios'
    const ch = installFakeNativeChannel({ getInstallations: INSTALLS, getBatteryAdvice: true })
    render(<AppSettingsModal open onClose={() => {}} />)
    await settle()
    expect(ch.methods()).not.toContain('getBatteryAdvice')
    expect(screen.queryByTestId('app-battery-row')).toBeNull()
  })
})
