import { describe, expect, it } from 'vitest'
import { cliChipInfo } from '../lib/cliChip'

const pins = { claude: '2.1.220', codex: '0.145.0' }

describe('cliChipInfo', () => {
  it('appends the resolved version when it matches the pin (no mismatch)', () => {
    const info = cliChipInfo(
      'claude-code',
      { claude: { version: '2.1.220', path: '/usr/bin/claude' } },
      pins,
    )
    expect(info).toEqual({ label: 'claude-code 2.1.220', mismatch: false, title: undefined })
  })

  it('flags drift with an explanatory tooltip', () => {
    const info = cliChipInfo(
      'claude-code',
      { claude: { version: '2.1.168', path: '/home/u/.local/bin/claude' } },
      pins,
    )
    expect(info.label).toBe('claude-code 2.1.168')
    expect(info.mismatch).toBe(true)
    expect(info.title).toBe('Platform pin: 2.1.220')
  })

  it('maps codex chip name to the codex status key', () => {
    const info = cliChipInfo('codex', { codex: { version: '0.145.0' } }, pins)
    expect(info).toEqual({ label: 'codex 0.145.0', mismatch: false, title: undefined })
  })

  it('renders a plain chip when there is no status (old satellite)', () => {
    expect(cliChipInfo('claude-code', undefined, pins)).toEqual({
      label: 'claude-code',
      mismatch: false,
    })
    expect(cliChipInfo('claude-code', { claude: { version: null } }, pins)).toEqual({
      label: 'claude-code',
      mismatch: false,
    })
  })

  it('never flags without a pin to judge against', () => {
    const info = cliChipInfo('claude-code', { claude: { version: '9.9.9' } }, {})
    expect(info.mismatch).toBe(false)
    expect(info.label).toBe('claude-code 9.9.9')
  })

  it('leaves unknown CLI names untouched', () => {
    expect(cliChipInfo('mystery-cli', { claude: { version: '1' } }, pins)).toEqual({
      label: 'mystery-cli',
      mismatch: false,
    })
  })
})
