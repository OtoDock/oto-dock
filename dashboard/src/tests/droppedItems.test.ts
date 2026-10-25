// Folder drops and folder picks flattened into files with their folder
// paths: the walk over the browser's entries, the cap, the folders an
// upload leaves out (what the sync and the listing skip), and the desktop
// test for the folder picker.

import { describe, it, expect, afterEach } from 'vitest'

import {
  canPickFolder, collectDropped, collectPicked, dropRoots, insideFolder, leftOutNote,
  splitDropForAttach, type DropRoot,
} from '../lib/droppedItems'
import { folderNote, folderRefusal } from '../pages/agent/chat/useChatAttachments'

function fileEntry(name: string, opts: { unreadable?: boolean } = {}): FileSystemFileEntry {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (ok: (f: File) => void, fail?: (e: DOMException) => void) => {
      if (opts.unreadable) fail?.(new DOMException('gone', 'NotFoundError'))
      else ok(new File(['x'], name))
    },
  } as unknown as FileSystemFileEntry
}

/** A directory whose reader answers in batches of `batch`, like Chrome's
 * 100; `failBatch` makes that call (0-based) fail. */
function dirEntry(
  name: string, children: FileSystemEntry[],
  opts: { batch?: number; unreadable?: boolean; failBatch?: number } = {},
): FileSystemDirectoryEntry {
  const batch = opts.batch ?? 100
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let at = 0
      let calls = 0
      return {
        readEntries: (ok: (e: FileSystemEntry[]) => void, fail?: (e: DOMException) => void) => {
          if (opts.unreadable || calls++ === opts.failBatch) {
            fail?.(new DOMException('denied', 'NotReadableError'))
            return
          }
          const out = children.slice(at, at + batch)
          at += out.length
          ok(out)
        },
      }
    },
  } as unknown as FileSystemDirectoryEntry
}

const paths = (r: Awaited<ReturnType<typeof collectDropped>>) =>
  r.files.map((f) => (f.relDir ? `${f.relDir}/${f.file.name}` : f.file.name))

describe('collectDropped', () => {
  it('walks nested folders and keeps plain files at the target', async () => {
    const roots: DropRoot[] = [
      dirEntry('mesitis', [
        fileEntry('agent.json'),
        dirEntry('context', [fileEntry('company.md')]),
      ]),
      new File(['y'], 'notes.txt'),
    ]
    const r = await collectDropped(roots)
    expect(paths(r)).toEqual(['mesitis/agent.json', 'mesitis/context/company.md', 'notes.txt'])
    expect(r.emptyDirs).toEqual([])
    expect(r.tooMany).toBe(false)
  })

  it('reads a directory to the end across batches', async () => {
    const many = Array.from({ length: 250 }, (_, i) => fileEntry(`f${i}.md`))
    const r = await collectDropped([dirEntry('big', many, { batch: 100 })])
    expect(r.files).toHaveLength(250)
  })

  it('reports empty folders and what it could not read', async () => {
    const r = await collectDropped([
      dirEntry('t', [
        dirEntry('empty', []),
        dirEntry('locked', [], { unreadable: true }),
        fileEntry('gone.md', { unreadable: true }),
        fileEntry('ok.md'),
      ]),
    ])
    expect(paths(r)).toEqual(['t/ok.md'])
    expect(r.emptyDirs).toEqual(['t/empty'])
    expect(r.unreadable).toEqual(['t/locked', 't/gone.md'])
  })

  it('stops past the cap and says so', async () => {
    const files = Array.from({ length: 6 }, (_, i) => fileEntry(`f${i}`))
    const r = await collectDropped([dirEntry('d', files)], 5)
    expect(r.tooMany).toBe(true)
    expect(r.files).toHaveLength(5)
    const exact = await collectDropped([dirEntry('d', files.slice(0, 5))], 5)
    expect(exact.tooMany).toBe(false)
  })

  it('leaves out what the sync leaves out, counts it, and keeps hidden files', async () => {
    const many = (n: number) => Array.from({ length: n }, (_, i) => fileEntry(`o${i}`))
    const r = await collectDropped([
      dirEntry('repo', [
        dirEntry('.git', many(40)),
        dirEntry('node_modules', many(40)),
        dirEntry('src', [fileEntry('a.ts'), dirEntry('__pycache__', many(2))]),
        dirEntry('lib', [
          dirEntry('.cache', many(2)),
          dirEntry('venv', [fileEntry('pyvenv.cfg'), ...many(2)]),
        ]),
        fileEntry('.env'),
        fileEntry('.gitignore'),
      ]),
    ], 5)
    // The filter runs before the cap: 160 left-out files never trip it.
    expect(r.tooMany).toBe(false)
    expect(paths(r)).toEqual(['repo/src/a.ts', 'repo/.env', 'repo/.gitignore'])
    expect(r.leftOut).toBe(5)
    expect(r.leftOutNames).toEqual(['.git', 'node_modules', '__pycache__'])
    expect(leftOutNote(r)).toBe('5 folders left out: .git, node_modules, __pycache__')
  })

  it('keeps what the sync carries: .claude, .codex, and a folder merely named venv', async () => {
    const r = await collectDropped([
      dirEntry('proj', [
        dirEntry('.claude', [dirEntry('commands', [fileEntry('review.md')])]),
        dirEntry('.codex', [fileEntry('config.toml')]),
        dirEntry('venv', [fileEntry('notes.md')]),
      ]),
    ])
    expect(paths(r)).toEqual([
      'proj/.claude/commands/review.md', 'proj/.codex/config.toml', 'proj/venv/notes.md'])
    expect(r.leftOut).toBe(0)
  })

  it('keeps a dropped hidden top folder; a top node_modules is left out', async () => {
    const r = await collectDropped([
      dirEntry('.config', [fileEntry('a.md')]),
      dirEntry('node_modules', [fileEntry('b.js')]),
    ])
    expect(paths(r)).toEqual(['.config/a.md'])
    expect(leftOutNote(r)).toBe('1 folder left out: node_modules')
  })

  it('reports a folder whose later batch fails as unreadable, none of its files', async () => {
    const files = Array.from({ length: 5 }, (_, i) => fileEntry(`f${i}`))
    const r = await collectDropped([
      dirEntry('t', [dirEntry('flaky', files, { batch: 2, failBatch: 1 }), fileEntry('ok.md')]),
    ])
    expect(paths(r)).toEqual(['t/ok.md'])
    expect(r.unreadable).toEqual(['t/flaky'])
  })

  it('tells an unreadable-only and an empty-only folder apart for the chat chip', async () => {
    const unreadable = await collectDropped([
      dirEntry('t', [fileEntry('a', { unreadable: true }), fileEntry('b', { unreadable: true })]),
    ])
    expect(unreadable.files).toEqual([])
    expect(folderRefusal(unreadable, 100)).toBe("2 items can't be read: nothing to attach")
    const empty = await collectDropped([dirEntry('t', [dirEntry('e', [])])])
    expect(empty.emptyDirs).toEqual(['t/e'])
    expect(folderRefusal(empty, 100)).toBe('An empty folder: nothing to attach')
    const hiddenOnly = await collectDropped([dirEntry('t', [dirEntry('.git', [fileEntry('x')])])])
    expect(folderRefusal(hiddenOnly, 100)).toBe('Nothing to attach. 1 folder left out: .git')
  })

  it('names the first file over the cap, and notes what a good folder lost', async () => {
    const big = { ...fileEntry('big.bin'), file: (ok: (f: File) => void) => ok(new File(['x'.repeat(200)], 'big.bin')) }
    const r = await collectDropped([
      dirEntry('t', [dirEntry('sub', [big as unknown as FileSystemFileEntry]), fileEntry('gone', { unreadable: true })]),
    ])
    expect(folderRefusal(r, 100)).toMatch(/^t\/sub\/big\.bin: over the .* upload limit$/)
    const ok = await collectDropped([
      dirEntry('t', [fileEntry('a'), fileEntry('gone', { unreadable: true }), dirEntry('.git', [fileEntry('x')])]),
    ])
    expect(folderRefusal(ok, 100)).toBe('')
    expect(folderNote(ok)).toBe("1 item can't be read · 1 folder left out: .git")
  })
})

describe('dropRoots and splitDropForAttach', () => {
  function transfer(items: Array<{ entry: FileSystemEntry | null; file: File | null }>, withEntries = true) {
    return {
      items: items.map((it) => ({
        kind: 'file',
        getAsFile: () => it.file,
        ...(withEntries ? { webkitGetAsEntry: () => it.entry } : {}),
      })),
      files: items.map((it) => it.file).filter(Boolean),
    } as unknown as DataTransfer
  }

  it('takes the entries, and the file where an item has none', () => {
    const dir = dirEntry('mesitis', [])
    const plain = new File(['p'], 'p.txt')
    const roots = dropRoots(transfer([
      { entry: dir, file: new File([], 'mesitis') },
      { entry: null, file: plain },
    ]))
    expect(roots).toEqual([dir, plain])
  })

  it('falls back to the plain files without the entries API', () => {
    const f = new File(['a'], 'a.txt')
    expect(dropRoots(transfer([{ entry: null, file: f }], false))).toEqual([f])
  })

  it('splits a chat drop into its files and its folders', () => {
    const f = new File(['a'], 'a.txt')
    const dir = dirEntry('mesitis', [])
    const split = splitDropForAttach(transfer([
      { entry: dir, file: new File([], 'mesitis') },
      { entry: fileEntry('a.txt'), file: f },
    ]))
    expect(split).toEqual({ files: [f], folders: [dir] })
  })

  it("takes a folder's files inside it", () => {
    const x = new File(['x'], 'x')
    expect(insideFolder([
      { file: x, relDir: 'mesitis' }, { file: x, relDir: 'mesitis/context' },
    ], 'mesitis').map((f) => f.relDir)).toEqual(['', 'context'])
  })
})

describe('collectPicked', () => {
  function picked(rel: string): File {
    const f = new File(['z'], rel.split('/').pop()!)
    Object.defineProperty(f, 'webkitRelativePath', { value: rel })
    return f
  }

  it('reads the folder of each file from its relative path', () => {
    const r = collectPicked([picked('rent/agent.json'), picked('rent/context/a.md')])
    expect(r.files.map((f) => f.relDir)).toEqual(['rent', 'rent/context'])
  })

  it('refuses a pick over the cap whole', () => {
    const r = collectPicked([picked('a/1'), picked('a/2'), picked('a/3')], 2)
    expect(r).toEqual({
      files: [], emptyDirs: [], unreadable: [], tooMany: true, leftOut: 0, leftOutNames: [],
    })
  })

  it('leaves out files under a left-out folder before the cap, counting each folder once', () => {
    const r = collectPicked([
      picked('.repo/.git/HEAD'), picked('.repo/.git/objects/ab/cd'),
      picked('.repo/web/node_modules/x/index.js'), picked('.repo/src/a.ts'),
      picked('.repo/.env'),
    ], 2)
    expect(r.tooMany).toBe(false)
    expect(r.files.map((f) => `${f.relDir}/${f.file.name}`)).toEqual(['.repo/src/a.ts', '.repo/.env'])
    expect(r.leftOut).toBe(2)
    expect(r.leftOutNames).toEqual(['.git', 'node_modules'])
  })
})

describe('collectPicked venvs', () => {
  const picked = (rel: string) => {
    const f = new File(['x'], rel.split('/').pop() as string)
    Object.defineProperty(f, 'webkitRelativePath', { value: rel })
    return f
  }

  it('leaves out a real venv by its pyvenv.cfg and keeps a folder merely named venv', () => {
    const r = collectPicked([
      picked('p/env/pyvenv.cfg'), picked('p/env/lib/site.py'),
      picked('p/venv/notes.md'), picked('p/.claude/settings.json'),
    ])
    expect(r.files.map((f) => `${f.relDir}/${f.file.name}`)).toEqual([
      'p/venv/notes.md', 'p/.claude/settings.json'])
    expect(r.leftOut).toBe(1)
    expect(r.leftOutNames).toEqual(['env'])
  })
})

describe('canPickFolder', () => {
  const real = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  afterEach(() => {
    if (real) Object.defineProperty(window, 'matchMedia', real)
    else delete (window as { matchMedia?: unknown }).matchMedia
  })
  const media = (matches: boolean) => Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (query: string) => ({ matches: matches && query === '(hover: hover) and (pointer: fine)' }),
  })

  it('is true for a hover-capable fine pointer, false for touch', () => {
    media(true)
    expect(canPickFolder()).toBe(true)
    media(false)
    expect(canPickFolder()).toBe(false)
  })

  it('is false, not a crash, where matchMedia is missing', () => {
    delete (window as { matchMedia?: unknown }).matchMedia
    expect(canPickFolder()).toBe(false)
  })
})
