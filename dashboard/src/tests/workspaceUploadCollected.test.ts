// A workspace upload batch (components/workspace/uploadCollected): each
// top-level folder reserved on the server where it lands, the popup's
// Cancel, a run of network errors stopping the batch, and the one summary
// toast at the end.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const uploadMock = vi.fn()
const reserveMock = vi.fn()
vi.mock('@/lib/uploadWithProgress', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/uploadWithProgress')>()),
  uploadWithProgress: (...args: unknown[]) => uploadMock(...args),
  reserveUploadFolder: (...args: unknown[]) => reserveMock(...args),
}))

import { uploadCollected } from '@/components/workspace/uploadCollected'
import { onAppToast, type AppToast } from '@/lib/appToasts'
import { emptyCollected, type CollectedItems } from '@/lib/droppedItems'
import { isBatchRunning, useTransferStore } from '@/store/transferStore'

const DIR = 'users/u/workspace'
const flush = () => new Promise((r) => setTimeout(r, 0))

function file(name: string, size = 3): File {
  return new File([new Uint8Array(size)], name)
}

function items(files: Array<[string, string]>, extra: Partial<CollectedItems> = {}): CollectedItems {
  return { ...emptyCollected(files.map(([relDir, name]) => ({ file: file(name), relDir }))), ...extra }
}

function resp(path: string) {
  return { path, filename: path.split('/').pop(), size: 3, transfer_id: `t-${path}`, remote_push: false }
}

function networkError(): Error {
  return Object.assign(new Error('Network error during upload'), { transient: true })
}

const rows = () => Object.values(useTransferStore.getState().byId)

let toasts: AppToast[] = []
let stopToasts: () => void = () => {}
const createDir = vi.fn()
const refresh = vi.fn()
const run = (collected: CollectedItems) => uploadCollected(collected, { agent: 'ag', dir: DIR, createDir, refresh })

beforeEach(() => {
  useTransferStore.setState({ byId: {} })
  uploadMock.mockReset()
  reserveMock.mockReset()
  createDir.mockReset()
  createDir.mockResolvedValue({})
  refresh.mockReset()
  toasts = []
  stopToasts = onAppToast((t) => toasts.push(t))
  reserveMock.mockImplementation((_agent: string, name: string) =>
    Promise.resolve({ path: `${DIR}/${name}_1`, name: `${name}_1` }))
  uploadMock.mockImplementation((f: File, _agent: string, target: string) =>
    Promise.resolve(resp(`${target}/${f.name}`)))
})

afterEach(() => stopToasts())

describe('uploadCollected', () => {
  it('reserves each top folder where it lands and uploads into the reservation', async () => {
    await run(items(
      [['mesitis', 'agent.json'], ['mesitis/context', 'company.md'], ['rent', 'r.md'], ['', 'notes.txt']],
      { emptyDirs: ['mesitis/empty', 'blank'], leftOut: 1, leftOutNames: ['.git'] },
    ))
    expect(reserveMock.mock.calls.map((c) => [c[1], c[3]])).toEqual([
      ['mesitis', DIR], ['rent', DIR], ['blank', DIR],
    ])
    expect(uploadMock.mock.calls.map((c) => c[2])).toEqual([
      `${DIR}/mesitis_1`, `${DIR}/mesitis_1/context`, `${DIR}/rent_1`, DIR,
    ])
    // A nested empty folder is made inside the reservation; a top one is it.
    expect(createDir.mock.calls).toEqual([[`${DIR}/mesitis_1/empty`]])
    expect(rows().map((r) => r.relPath).sort()).toEqual([
      `${DIR}/mesitis_1/agent.json`, `${DIR}/mesitis_1/context/company.md`,
      `${DIR}/notes.txt`, `${DIR}/rent_1/r.md`,
    ])
    expect(toasts).toEqual([{
      title: 'Uploaded', body: 'Uploaded 4 files. 1 folder left out (.git).', severity: 'success',
    }])
    expect(refresh).toHaveBeenCalled()
  })

  it('stops at Cancel: the file on the wire is cancelled, the rest never start', async () => {
    uploadMock.mockImplementation((f: File, _a: string, target: string, _p: unknown, signal: AbortSignal) =>
      f.name === 'b.md'
        ? new Promise((_res, rej) => signal.addEventListener('abort',
          () => rej(new DOMException('Upload aborted', 'AbortError'))))
        : Promise.resolve(resp(`${target}/${f.name}`)))
    const done = run(items([['d', 'a.md'], ['d', 'b.md'], ['d', 'c.md']]))
    for (let i = 0; i < 10; i++) await flush()
    const running = rows().find((r) => isBatchRunning(r))!
    expect(running.filename).toBe('d_1/b.md')
    useTransferStore.getState().cancelBatch(running.batchId!)
    await done
    expect(uploadMock).toHaveBeenCalledTimes(2)
    expect(rows().find((r) => r.filename === 'd_1/b.md')).toMatchObject({
      uploadFailed: true, uploadError: 'cancelled',
    })
    expect(rows().some((r) => isBatchRunning(r))).toBe(false)
    expect(toasts).toEqual([{
      title: 'Upload cancelled', body: 'Uploaded 1 of 3 files; the rest were not sent.', severity: 'info',
    }])
  })

  it('reserves a top folder only before its first file: a Cancel leaves the later ones uncreated', async () => {
    uploadMock.mockImplementation((f: File, _a: string, target: string, _p: unknown, signal: AbortSignal) =>
      f.name === 'a1.md'
        ? new Promise((_res, rej) => signal.addEventListener('abort',
          () => rej(new DOMException('Upload aborted', 'AbortError'))))
        : Promise.resolve(resp(`${target}/${f.name}`)))
    const done = run(items([['A', 'a1.md'], ['B', 'b1.md'], ['C', 'c1.md']], { emptyDirs: ['D'] }))
    for (let i = 0; i < 10; i++) await flush()
    const running = rows().find((r) => isBatchRunning(r))!
    useTransferStore.getState().cancelBatch(running.batchId!)
    await done
    expect(reserveMock.mock.calls.map((c) => c[1])).toEqual(['A'])
    expect(createDir).not.toHaveBeenCalled()
  })

  it('stops after three network errors in a row and marks the rest not sent', async () => {
    const outcomes: Array<'net' | 'refused' | 'ok'> = ['net', 'refused', 'net', 'net', 'net', 'ok', 'ok']
    uploadMock.mockImplementation((f: File) => {
      const what = outcomes[Number(f.name)]
      if (what === 'net') return Promise.reject(networkError())
      if (what === 'refused') return Promise.reject(new Error('File too large (max 1 MB)'))
      return Promise.resolve(resp(`${DIR}/${f.name}`))
    })
    await run(items(outcomes.map((_, i) => ['', String(i)])))
    // A refusal breaks the run: the stop comes after files 2, 3 and 4.
    expect(uploadMock).toHaveBeenCalledTimes(5)
    const notSent = rows().filter((r) => r.uploadError === 'not sent: the connection dropped')
    expect(notSent.map((r) => r.filename)).toEqual(['5', '6'])
    expect(rows().every((r) => r.uploadFailed)).toBe(true)
    expect(toasts).toEqual([{
      title: 'Upload incomplete',
      body: '7 of 7 files failed: the connection dropped.',
      severity: 'warning',
    }])
  })

  it('fails the files of a folder it cannot reserve, an empty folder it cannot make, and says so once', async () => {
    reserveMock.mockImplementation((_agent: string, name: string) => name === 'locked'
      ? Promise.reject(new Error('Viewers can write only to their own user directory'))
      : Promise.resolve({ path: `${DIR}/${name}`, name }))
    createDir.mockRejectedValue(new Error('HTTP 500'))
    await run(items(
      [['locked', 'x.md'], ['open', 'y.md']],
      { emptyDirs: ['open/empty'], unreadable: ['open/held.docx'] },
    ))
    expect(uploadMock.mock.calls.map((c) => (c[0] as File).name)).toEqual(['y.md'])
    const byName = Object.fromEntries(rows().map((r) => [r.filename, r]))
    expect(byName['locked/x.md'].uploadError).toBe(
      'the folder could not be created: Viewers can write only to their own user directory')
    expect(byName['open/empty']).toMatchObject({ uploadFailed: true, uploadError: 'HTTP 500' })
    expect(byName['open/held.docx'].uploadFailed).toBe(true)
    expect(toasts).toEqual([{
      title: 'Upload incomplete',
      body: '2 of 3 files failed. 1 empty folder not created.',
      severity: 'warning',
    }])
  })

  it('refuses too many files whole and says what a drop of left-out folders only did', async () => {
    await run({ ...items([['', 'a']]), tooMany: true })
    await run(items([], { leftOut: 2, leftOutNames: ['.git', 'node_modules'] }))
    expect(uploadMock).not.toHaveBeenCalled()
    expect(reserveMock).not.toHaveBeenCalled()
    expect(toasts.map((t) => t.title)).toEqual(['Not uploaded', 'Nothing uploaded'])
    expect(toasts[1].body).toBe('2 folders left out (.git, node_modules).')
  })
})
