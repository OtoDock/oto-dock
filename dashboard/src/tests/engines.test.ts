import { describe, it, expect } from 'vitest'

// ─── lib/engines: every question a page asks about an engine is answered from
//     its descriptor. The engines here are invented (ids no real engine has),
//     so a helper that remembered the three real ids would fail. ───────────────

import {
  accountLabel, acceptsLocalEndpoints, acceptsRelay, codingEngineNames, engineLabel,
  engineLabels, isCoding, joinOr, keyProviders, oauthFlow, orderedEngines, runsInteractive, runsRemote,
  sortEngineRows, sortEngines, supportsCompact, supportsOAuth, vendorBadge,
} from '@/lib/engines'
import { descriptor } from './fixtures/engines'

const acme = descriptor({
  name: 'acme-cli', display_name: 'Acme Coder',
  identity: { vendor_id: 'acme', vendor_label: 'Acme', account_label: 'AcmeHub', role: 'coding', sort_order: 15 },
  runtime: { supports_interactive_pty: true, supports_remote_execution: true, interactive_first_prompt_via_argv: true },
  auth: { auth_types: ['oauth', 'api_key'], oauth_flow: 'device_code' },
})
const zephyr = descriptor({
  name: 'zephyr-api', display_name: 'Zephyr API',
  identity: { role: 'supporting', sort_order: 40 },
  providers: [
    { id: 'zephyr', label: 'Zephyr', requires_key: true },
    { id: 'nimbus', label: 'Nimbus', requires_key: true },
    { id: 'local_box', label: 'A box on the LAN', requires_key: false },
  ],
  auth: { auth_types: ['api_key', 'local_endpoint', 'relay'] },
})

describe('engine labels', () => {
  it('strips the vendor prefix only when the name starts with it', () => {
    // "Acme Coder" by the vendor Acme reads "Coder" — as "OpenAI Codex" reads "Codex".
    expect(engineLabel(acme)).toBe('Coder')
    // A name that merely contains the vendor keeps it; a multi-provider engine has none to strip.
    expect(engineLabel(descriptor({ display_name: 'Coder by Acme', identity: { vendor_label: 'Acme' } }))).toBe('Coder by Acme')
    expect(engineLabel(descriptor({ display_name: 'Acme Acme Coder', identity: { vendor_label: 'Acme' } }))).toBe('Acme Coder')
    expect(engineLabel(zephyr)).toBe('Zephyr API')
  })
  it('vendor chip and account label come from the identity', () => {
    expect(vendorBadge(acme)).toBe('Acme')
    expect(vendorBadge(zephyr)).toBeNull()
    expect(accountLabel(acme)).toBe('AcmeHub')
    expect(accountLabel(zephyr)).toBe('')
  })
  it('maps the catalog to id → label', () => {
    expect(engineLabels({ a: acme, z: zephyr })).toEqual({ 'acme-cli': 'Coder', 'zephyr-api': 'Zephyr API' })
    expect(engineLabels(undefined)).toEqual({})
  })
})

describe('engine order and roles', () => {
  it('sorts by sort_order, then id', () => {
    const tie = descriptor({ name: 'alpha', identity: { sort_order: 40 } })
    expect(sortEngines([zephyr, acme, tie]).map((e) => e.name)).toEqual(['acme-cli', 'alpha', 'zephyr-api'])
    expect(orderedEngines({ z: zephyr, a: acme }).map((e) => e.name)).toEqual(['acme-cli', 'zephyr-api'])
    expect(orderedEngines(undefined)).toEqual([])
    expect(sortEngineRows([{ capabilities: zephyr, x: 1 }, { capabilities: acme, x: 2 }]).map((r) => r.x)).toEqual([2, 1])
  })
  it('names the coding engines for a sentence', () => {
    const beta = descriptor({ name: 'beta', display_name: 'Beta', identity: { role: 'coding', sort_order: 20 } })
    expect(isCoding(acme)).toBe(true)
    expect(isCoding(zephyr)).toBe(false)
    expect(codingEngineNames([zephyr, beta, acme])).toEqual(['Coder', 'Beta'])
    expect(joinOr(['Coder', 'Beta'])).toBe('Coder or Beta')
    expect(joinOr(['A', 'B', 'C'])).toBe('A, B or C')
    expect(joinOr(['A'])).toBe('A')
    expect(joinOr([])).toBe('')
  })
})

describe('auth facts', () => {
  it('login, flow, local endpoints and the relay read the auth types', () => {
    expect(supportsOAuth(acme)).toBe(true)
    expect(supportsOAuth(zephyr)).toBe(false)
    expect(oauthFlow(acme)).toBe('device_code')
    expect(oauthFlow(zephyr)).toBe('')
    expect(acceptsLocalEndpoints(zephyr)).toBe(true)
    expect(acceptsLocalEndpoints(acme)).toBe(false)
    expect(acceptsRelay(zephyr)).toBe(true)
    expect(acceptsRelay(acme)).toBe(false)
  })
  it('key providers: the key-taking providers, else the vendor, else none', () => {
    // A multi-provider engine offers its key-taking providers (a LAN box is not a key).
    expect(keyProviders(zephyr).map((p) => p.id)).toEqual(['zephyr', 'nimbus'])
    // A single-vendor engine offers its vendor.
    expect(keyProviders(acme)).toEqual([{ id: 'acme', label: 'Acme', requires_key: true }])
    // An engine that takes no key offers nothing, whatever its vendor.
    expect(keyProviders(descriptor({ identity: { vendor_id: 'acme' }, auth: { auth_types: ['oauth'] } }))).toEqual([])
    // A multi-provider engine with no vendor and no providers list offers nothing.
    expect(keyProviders(descriptor({ providers: null, identity: { vendor_id: '' } }))).toEqual([])
  })
})

describe('runtime facts and their unknown-catalog defaults', () => {
  it('interactive: false until the descriptor says so', () => {
    expect(runsInteractive(acme)).toBe(true)
    expect(runsInteractive(zephyr)).toBe(false)
    expect(runsInteractive(undefined)).toBe(false)
  })
  it('remote: true until the descriptor says otherwise (the server refuses at start)', () => {
    expect(runsRemote(acme)).toBe(true)
    expect(runsRemote(zephyr)).toBe(false)
    expect(runsRemote(undefined)).toBe(true)
  })
  it('compact: the composer offers it where the engine declares it, never without a catalog', () => {
    const compacting = descriptor({ name: 'orbit-cli', behaviour: { supports_compact: true } })
    const catalog = { 'orbit-cli': compacting, 'acme-cli': acme }
    expect(supportsCompact(catalog, 'orbit-cli')).toBe(true)
    expect(supportsCompact(catalog, 'acme-cli')).toBe(false)   // the fixture's default
    expect(supportsCompact(catalog, 'unknown-cli')).toBe(false)
    expect(supportsCompact(undefined, 'orbit-cli')).toBe(false)
  })
})
