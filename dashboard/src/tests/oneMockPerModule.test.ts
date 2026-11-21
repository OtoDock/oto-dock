// @vitest-environment node
// A test file mocks a module once. vitest resolves the mocks of a file
// together, so of two registrations for one module either can land last:
// the file then passes or fails by the order they came back in.
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

import { DASHBOARD } from '../../design/build.mjs'

const SRC = join(DASHBOARD, 'src')
const MOCK = /^\s*vi\.mock\(\s*['"]([^'"]+)['"]/gm

function testFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return testFiles(path)
    return /\.(test|spec)\.tsx?$/.test(entry.name) ? [path] : []
  })
}

/** The modules a source mocks more than once. */
function mockedTwice(source: string): string[] {
  const seen = new Map<string, number>()
  for (const [, module] of source.matchAll(MOCK)) seen.set(module, (seen.get(module) ?? 0) + 1)
  return [...seen].filter(([, n]) => n > 1).map(([module]) => module)
}

describe('one mock per module in a test file', () => {
  it('finds a module mocked twice, and only that one', () => {
    const source = "vi.mock('@/api/apps', () => ({}))\nvi.mock('@/api/shares', () => ({}))\n  vi.mock(\"@/api/apps\", () => ({}))\n"
    expect(mockedTwice(source)).toEqual(['@/api/apps'])
    expect(mockedTwice("vi.mock('@/api/apps', () => ({}))\n")).toEqual([])
  })

  it('no test file mocks a module twice', () => {
    const files = testFiles(SRC)
    expect(files.length).toBeGreaterThan(100)
    const twice = files.flatMap((file) => mockedTwice(readFileSync(file, 'utf8'))
      .map((module) => `${relative(DASHBOARD, file)}: ${module}`))
    expect(twice).toEqual([])
  })
})
