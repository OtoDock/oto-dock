// @vitest-environment node
// The settings catalog's own rules: ids unique and well formed, the role
// literals equal to the mirror's, every card binding complete, an admin
// card behind a confirm, and every sentence a person reads on the reader
// rules (the catalog's `confirm`, `label`, `group` and option labels are
// read in the chat and on the skill page, which plainSentences.test.ts
// does not reach).
import { describe, expect, it } from 'vitest'

import { AREA, CATALOG, CATALOG_ROLE, KIND, SETTINGS_CATALOG_VERSION, entriesOf, findEntry } from '@/components/settings/catalog'
import { ROLE } from '@/lib/permissions'

const MECHANISM = /\b(carve|floor|hook|broker|probe|egress|daemon|endpoint|handler|payload|regex|mutex|socket|stdout|stderr|cgroup|namespace|userns|asyncio|coroutine|mixin|adapter|tombstone|manifest|runtime|sentinel|heuristic|idempotent|pool)s?\b/i
const DASH = /[–—]|(^|\s)-(\s|$)/
const MAX_WORDS = 20

function sentences(text: string): string[] {
  return text.split(/(?<=[.!?])\s+/).map((s) => s.trim()).filter(Boolean)
}

function faults(text: string): string[] {
  const out: string[] = []
  if (text.includes(';')) out.push('semicolon')
  if (DASH.test(text)) out.push('dash')
  if (MECHANISM.test(text)) out.push(`mechanism word ${MECHANISM.exec(text)?.[1]}`)
  if (/[()[\]]/.test(text)) out.push('brackets')
  for (const s of sentences(text)) {
    if (s.split(/\s+/).length > MAX_WORDS) out.push(`long: ${s}`)
    if (/^[a-z]/.test(s)) out.push(`lower case start: ${s}`)
  }
  return out
}

describe('the settings catalog', () => {
  it('has a version and well-formed, unique ids', () => {
    expect(SETTINGS_CATALOG_VERSION).toBeGreaterThan(0)
    const ids = CATALOG.map((e) => e.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const e of CATALOG) {
      expect(e.id, e.id).toMatch(/^(user|agent|admin)\.[a-z0-9-]+\.[a-z0-9-]+$/)
      expect(e.id.startsWith(`${e.area}.${e.section}.`), `${e.id} names its area and section`).toBe(true)
    }
    expect(findEntry(ids[0])?.id).toBe(ids[0])
    expect(entriesOf(AREA.USER, 'general').length).toBeGreaterThan(5)
  })

  it('spells the roles as the mirror does', () => {
    expect(CATALOG_ROLE.MEMBER).toBe(ROLE.MEMBER)
    expect(CATALOG_ROLE.ADMIN).toBe(ROLE.ADMIN)
  })

  it('binds every card completely, an admin card behind a confirm', () => {
    for (const e of CATALOG) {
      if (!e.value) continue
      if ('device' in e.value) {
        expect(e.kind === KIND.CHOICE || e.kind === KIND.TOGGLE, e.id).toBe(true)
        continue
      }
      expect(e.value.read.path, e.id).toMatch(/^\/(v1|auth)\//)
      expect(e.value.read.field, e.id).toMatch(/^[a-z_]+$/)
      expect(e.value.write.path, e.id).toMatch(/^\/v1\//)
      expect(e.value.write.field, e.id).toMatch(/^[a-z_]+$/)
      expect([KIND.TOGGLE, KIND.CHOICE, KIND.TEXT, KIND.NUMBER], e.id).toContain(e.kind)
      if (e.kind === KIND.CHOICE) expect(e.options?.length ?? 0, e.id).toBeGreaterThan(1)
      if (e.writes === CATALOG_ROLE.ADMIN) expect(e.value.confirm, `${e.id} needs a confirm sentence`).toBeTruthy()
      else expect(e.value.confirm, `${e.id} is not an admin's`).toBeUndefined()
    }
  })

  it('names a lowest role on every row, the writer never below the reader', () => {
    const rank = { [CATALOG_ROLE.MEMBER]: 0, [CATALOG_ROLE.ADMIN]: 1 }
    for (const e of CATALOG) {
      expect(rank[e.reads], e.id).toBeDefined()
      if (e.writes) expect(rank[e.writes], e.id).toBeGreaterThanOrEqual(rank[e.reads])
      if (e.kind === KIND.READONLY) expect(e.writes, e.id).toBeNull()
      if (e.kind === KIND.ITEM) expect(e.item, `${e.id} says what one row is`).toBeTruthy()
    }
  })

  it('reads on the reader rules', () => {
    const found: string[] = []
    for (const e of CATALOG) {
      const texts = [e.help, e.note ?? '', e.group, ...(e.options ?? []).map((o) => o.label)]
      if (e.value && 'confirm' in e.value && e.value.confirm) texts.push(e.value.confirm)
      for (const t of texts) for (const f of faults(t)) found.push(`${e.id}: ${f}`)
      expect(e.help.trim().endsWith('.'), `${e.id} help ends with a full stop`).toBe(true)
    }
    expect(found).toEqual([])
  })
})

// The generator's two outputs are current, and its labels are the pages'.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { REPO, SECTION_LABELS, buildSettingsOutputs, whereOf } from '../../design/settings-map.mjs'
import { adminSections, userSections } from '@/components/settings/sections'

describe('the generated settings outputs', () => {
  const outputs = buildSettingsOutputs()

  it.each([...outputs.keys()])('%s is current', (rel) => {
    const path = join(REPO, rel)
    // The public cut's gate runs this suite in the snapshot, where the
    // proxy and the MCPs may be absent.
    if (!existsSync(path)) return
    expect(
      readFileSync(path, 'utf8') === outputs.get(rel),
      `${rel} is stale: run \`npm run settings:build\` in dashboard/ and commit the result`,
    ).toBe(true)
  })

  it('names the sections as the registry does', () => {
    for (const s of userSections({ remoteMachinesAvailable: true })) {
      expect(SECTION_LABELS[AREA.USER][s.path.split('/').pop()!]).toBe(s.label)
    }
    for (const s of adminSections({ remoteMachinesAvailable: true, pendingMcpRequests: 0 })) {
      expect(SECTION_LABELS[AREA.ADMIN][s.path.split('/').pop()!]).toBe(s.label)
    }
    expect(whereOf(CATALOG[0])).toBe('User Settings, General')
  })

  it('serves every row with its sentences and no value', () => {
    const json = JSON.parse(outputs.get('proxy/api/settings/catalog.json')!) as { version: number; entries: Record<string, unknown>[] }
    expect(json.version).toBe(SETTINGS_CATALOG_VERSION)
    expect(json.entries.length).toBe(CATALOG.length)
    for (const row of json.entries) {
      expect(typeof row.where).toBe('string')
      expect(typeof row.who).toBe('string')
      expect(typeof row.card).toBe('boolean')
      expect(row).not.toHaveProperty('current')
    }
  })
})
