// The chat attachment upload queue — the module-level singleton that
// serializes every chat upload onto ONE wire chain (3×300MB picked together
// must not run in parallel), keeps writing progress through the draft→chat
// re-key, and wires transferStore for the phase-2 machine-sync join.

import { describe, it, expect, vi, beforeEach } from 'vitest'

const uploadMock = vi.fn()
const reserveMock = vi.fn()
const invalidateMock = vi.fn()
vi.mock('@/lib/uploadWithProgress', () => ({
  uploadWithProgress: (...args: unknown[]) => uploadMock(...args),
  reserveUploadFolder: (...args: unknown[]) => reserveMock(...args),
}))
vi.mock('@/lib/queryClient', () => ({
  queryClient: { invalidateQueries: (...args: unknown[]) => invalidateMock(...args) },
}))

import { enqueueChatUpload, dequeueChatUpload, chatUploadQueueSize, chatUploadActive } from '@/lib/chatUploadQueue'
import { useChatStore } from '@/store/chatStore'
import { useTransferStore } from '@/store/transferStore'

const flush = () => new Promise((r) => setTimeout(r, 0))

function makeFile(name: string, size: number): File {
  return new File([new Uint8Array(size)], name)
}

function addPending(chatId: string, id: string, file: File): AbortController {
  const abort = new AbortController()
  useChatStore.getState().addPendingFiles(chatId, [{
    id, name: file.name, size: file.size, file,
    uploading: true, queued: true, abortController: abort,
  }])
  return abort
}

function resp(path: string, name: string, size: number, transferId: string, remotePush = false) {
  return { path, filename: name, size, transfer_id: transferId, remote_push: remotePush }
}

describe('chatUploadQueue', () => {
  beforeEach(async () => {
    // Drain anything a previous test left queued, then reset stores.
    while (chatUploadQueueSize() > 0) {
      const before = chatUploadQueueSize()
      useChatStore.setState({ byChat: {} })
      await flush()
      if (chatUploadQueueSize() >= before) break
    }
    useChatStore.setState({ byChat: {} })
    useTransferStore.setState({ byId: {} })
    uploadMock.mockReset()
    reserveMock.mockReset()
    invalidateMock.mockReset()
  })

  it('refreshes the workspace tree once a file lands, and for a folder once it exists and once it is whole', async () => {
    uploadMock.mockImplementation((file: File) => Promise.resolve(resp('users/u/workspace/uploads/files/' + file.name, file.name, file.size, 't')))
    const abort = addPending('chatT', 'one', makeFile('one.txt', 3))
    enqueueChatUpload({ fileId: 'one', file: makeFile('one.txt', 3), agent: 'ag', abort })
    for (let i = 0; i < 6; i++) await flush()
    expect(invalidateMock.mock.calls).toEqual([[{ queryKey: ['agent-files', 'ag'] }]])

    invalidateMock.mockReset()
    reserveMock.mockResolvedValue({ path: 'users/u/workspace/uploads/files/pics', name: 'pics' })
    const abortF = new AbortController()
    useChatStore.getState().addPendingFiles('chatT', [{
      id: 'fold', name: 'pics', size: 7, folder: true, fileCount: 2,
      uploading: true, queued: true, abortController: abortF,
    }])
    enqueueChatUpload({
      fileId: 'fold', agent: 'ag', abort: abortF,
      folder: { name: 'pics', files: [
        { file: makeFile('a.png', 3), relDir: '' },
        { file: makeFile('b.png', 4), relDir: 'sub' },
      ] },
    })
    for (let i = 0; i < 10; i++) await flush()
    // The reserved (empty) folder shows at once; the whole folder once more at the end.
    expect(invalidateMock.mock.calls).toEqual([[{ queryKey: ['agent-files', 'ag'] }]])
  })

  it('refreshes nothing for an upload that failed before anything landed', async () => {
    uploadMock.mockRejectedValue(new Error('Network error during upload'))
    const abort = addPending('chatU', 'bad', makeFile('bad.txt', 3))
    enqueueChatUpload({ fileId: 'bad', file: makeFile('bad.txt', 3), agent: 'ag', abort })
    for (let i = 0; i < 6; i++) await flush()
    expect(invalidateMock).not.toHaveBeenCalled()
  })

  it('uploads a folder into a reserved folder, each file in its place', async () => {
    reserveMock.mockResolvedValue({ path: 'users/u/workspace/uploads/files/mesitis_1', name: 'mesitis_1' })
    uploadMock.mockImplementation((file: File) => Promise.resolve(resp('x', file.name, file.size, 't')))
    const abort = new AbortController()
    useChatStore.getState().addPendingFiles('chatF', [{
      id: 'fold', name: 'mesitis', size: 7, folder: true, fileCount: 2,
      uploading: true, queued: true, abortController: abort,
    }])
    enqueueChatUpload({
      fileId: 'fold', agent: 'ag', abort,
      folder: { name: 'mesitis', files: [
        { file: makeFile('agent.json', 3), relDir: '' },
        { file: makeFile('company.md', 4), relDir: 'context' },
      ] },
    })
    for (let i = 0; i < 10; i++) await flush()
    expect(reserveMock).toHaveBeenCalledWith('ag', 'mesitis', abort.signal)
    expect(uploadMock.mock.calls.map((c) => c[2])).toEqual([
      'users/u/workspace/uploads/files/mesitis_1',
      'users/u/workspace/uploads/files/mesitis_1/context',
    ])
    const chip = useChatStore.getState().byChat['chatF'].pendingFiles[0]
    expect(chip).toMatchObject({
      uploading: false, uploadedPath: 'users/u/workspace/uploads/files/mesitis_1',
      name: 'mesitis_1', folder: true,
    })
  })

  it('names the file that failed a folder upload', async () => {
    reserveMock.mockResolvedValue({ path: 'w/up/mesitis', name: 'mesitis' })
    uploadMock.mockImplementation((file: File) => file.name === 'company.md'
      ? Promise.reject(new Error('File too large (max 1 MB)'))
      : Promise.resolve(resp('x', file.name, file.size, 't')))
    const abort = new AbortController()
    useChatStore.getState().addPendingFiles('chatG', [{
      id: 'fold2', name: 'mesitis', size: 7, folder: true, fileCount: 2,
      uploading: true, queued: true, abortController: abort,
    }])
    enqueueChatUpload({
      fileId: 'fold2', agent: 'ag', abort,
      folder: { name: 'mesitis', files: [
        { file: makeFile('agent.json', 3), relDir: '' },
        { file: makeFile('company.md', 4), relDir: 'context' },
      ] },
    })
    for (let i = 0; i < 10; i++) await flush()
    const chip = useChatStore.getState().byChat['chatG'].pendingFiles[0]
    expect(chip.error).toBe('context/company.md: File too large (max 1 MB)')
    expect(chip.uploadedPath).toBeUndefined()
    // The reservation and the count stay on the chip for a Retry.
    expect(chip).toMatchObject({ folderPath: 'w/up/mesitis', filesDone: 1 })
  })

  function addFolderChip(chatId: string, id: string, files: Array<{ file: File; relDir: string }>) {
    const abort = new AbortController()
    useChatStore.getState().addPendingFiles(chatId, [{
      id, name: 'mesitis', size: 0, folder: true, fileCount: files.length, folderFiles: files,
      uploading: true, queued: true, abortController: abort,
    }])
    return abort
  }
  const folderFiles = () => [
    { file: makeFile('a.md', 3), relDir: '' },
    { file: makeFile('b.md', 4), relDir: 'context' },
    { file: makeFile('c.md', 5), relDir: 'context' },
  ]

  it('resumes a folder at the file that failed, in the folder reserved before', async () => {
    reserveMock.mockResolvedValue({ path: 'w/up/mesitis', name: 'mesitis' })
    uploadMock.mockImplementation((file: File) => file.name === 'b.md'
      ? Promise.reject(new Error('Network error during upload'))
      : Promise.resolve(resp('x', file.name, file.size, 't')))
    const files = folderFiles()
    const abort = addFolderChip('chatR', 'fr', files)
    enqueueChatUpload({ fileId: 'fr', agent: 'ag', abort, folder: { name: 'mesitis', files } })
    for (let i = 0; i < 10; i++) await flush()
    const failed = useChatStore.getState().byChat['chatR'].pendingFiles[0]
    expect(failed).toMatchObject({ error: 'context/b.md: Network error during upload', filesDone: 1 })

    // The Retry the chip offers: the same reservation, from the second file.
    uploadMock.mockClear()
    uploadMock.mockImplementation((file: File) => Promise.resolve(resp('x', file.name, file.size, 't')))
    const again = new AbortController()
    enqueueChatUpload({
      fileId: 'fr', agent: 'ag', abort: again,
      folder: { name: failed.name, files, path: failed.folderPath, done: failed.filesDone },
    })
    for (let i = 0; i < 10; i++) await flush()
    expect(reserveMock).toHaveBeenCalledTimes(1)
    expect(uploadMock.mock.calls.map((c) => [(c[0] as File).name, c[2]])).toEqual([
      ['b.md', 'w/up/mesitis/context'], ['c.md', 'w/up/mesitis/context'],
    ])
    expect(useChatStore.getState().byChat['chatR'].pendingFiles[0]).toMatchObject({
      uploadedPath: 'w/up/mesitis', filesDone: 3, uploadSent: 12, uploadTotal: 12,
    })
  })

  it('a reservation that fails fails the chip, uploads nothing, and is reserved again on retry', async () => {
    reserveMock.mockRejectedValueOnce(new Error('Contributor role or above is required'))
    const files = folderFiles()
    const abort = addFolderChip('chatV', 'fv', files)
    enqueueChatUpload({ fileId: 'fv', agent: 'ag', abort, folder: { name: 'mesitis', files } })
    for (let i = 0; i < 10; i++) await flush()
    const chip = useChatStore.getState().byChat['chatV'].pendingFiles[0]
    expect(chip.error).toBe('Contributor role or above is required')
    expect(chip.folderPath).toBeUndefined()
    expect(uploadMock).not.toHaveBeenCalled()

    reserveMock.mockResolvedValue({ path: 'w/up/mesitis', name: 'mesitis' })
    uploadMock.mockImplementation((file: File) => Promise.resolve(resp('x', file.name, file.size, 't')))
    enqueueChatUpload({
      fileId: 'fv', agent: 'ag', abort: new AbortController(),
      folder: { name: chip.name, files, path: chip.folderPath, done: chip.filesDone },
    })
    for (let i = 0; i < 10; i++) await flush()
    expect(reserveMock).toHaveBeenCalledTimes(2)
    expect(uploadMock).toHaveBeenCalledTimes(3)
  })

  it('a folder removed mid-upload stops there, keeps what landed, writes no error', async () => {
    reserveMock.mockResolvedValue({ path: 'w/up/mesitis', name: 'mesitis' })
    let release!: () => void
    uploadMock.mockImplementation((file: File, _a: string, _t: string, _p: unknown, signal: AbortSignal) =>
      file.name === 'a.md'
        ? Promise.resolve(resp('x', file.name, file.size, 't'))
        : new Promise((_res, rej) => {
          release = () => rej(new DOMException('Upload aborted', 'AbortError'))
          signal.addEventListener('abort', () => release())
        }))
    const files = folderFiles()
    const abort = addFolderChip('chatA', 'fa2', files)
    enqueueChatUpload({ fileId: 'fa2', agent: 'ag', abort, folder: { name: 'mesitis', files } })
    for (let i = 0; i < 10; i++) await flush()
    expect(uploadMock).toHaveBeenCalledTimes(2)
    abort.abort()
    useChatStore.getState().removePendingFile('chatA', 'fa2')
    for (let i = 0; i < 10; i++) await flush()
    expect(uploadMock).toHaveBeenCalledTimes(2) // c.md never started
    expect(chatUploadActive()).toBe(false)
  })

  it('reports an upload as active while its only entry is on the wire', async () => {
    // The queue is empty the moment its single file starts uploading; the
    // reload-on-new-build deferral must still see the running one.
    let resolveUpload!: (v: unknown) => void
    uploadMock.mockImplementation(() => new Promise((res) => { resolveUpload = res }))
    const f = makeFile('one.bin', 10)
    const a = addPending('chat1', 'fo', f)
    expect(chatUploadActive()).toBe(false)
    enqueueChatUpload({ fileId: 'fo', file: f, agent: 'ag', abort: a })
    await flush()
    expect(chatUploadQueueSize()).toBe(0)
    expect(chatUploadActive()).toBe(true)
    resolveUpload(resp('p/one.bin', 'one.bin', 10, 't1'))
    await flush()
    expect(chatUploadActive()).toBe(false)
  })

  it('serializes uploads: the second starts only after the first resolves', async () => {
    const resolvers: Array<(v: unknown) => void> = []
    uploadMock.mockImplementation(() => new Promise((res) => resolvers.push(res)))
    const f1 = makeFile('a.bin', 10)
    const f2 = makeFile('b.bin', 20)
    const a1 = addPending('chat1', 'f1', f1)
    const a2 = addPending('chat1', 'f2', f2)
    enqueueChatUpload({ fileId: 'f1', file: f1, agent: 'ag', abort: a1 })
    enqueueChatUpload({ fileId: 'f2', file: f2, agent: 'ag', abort: a2 })
    await flush()
    expect(uploadMock).toHaveBeenCalledTimes(1)

    resolvers[0](resp('users/u/workspace/uploads/files/a.bin', 'a.bin', 10, 't1'))
    await flush()
    expect(uploadMock).toHaveBeenCalledTimes(2)

    const s1 = useChatStore.getState().byChat['chat1'].pendingFiles.find((f) => f.id === 'f1')!
    expect(s1.uploading).toBe(false)
    expect(s1.uploadedPath).toBe('users/u/workspace/uploads/files/a.bin')
    expect(s1.transferId).toBe('t1')

    resolvers[1](resp('users/u/workspace/uploads/files/b.bin', 'b.bin', 20, 't2'))
    await flush()
    const s2 = useChatStore.getState().byChat['chat1'].pendingFiles.find((f) => f.id === 'f2')!
    expect(s2.uploadedPath).toBe('users/u/workspace/uploads/files/b.bin')
  })

  it('registers phase 1 in transferStore and links to the server transfer id', async () => {
    let resolveUpload!: (v: unknown) => void
    uploadMock.mockImplementation(() => new Promise((res) => { resolveUpload = res }))
    const f = makeFile('vid.mp4', 100)
    const a = addPending('chat1', 'fx', f)
    enqueueChatUpload({ fileId: 'fx', file: f, agent: 'ag', abort: a })
    await flush()
    expect(useTransferStore.getState().byId['local:chat-fx']).toMatchObject({
      kind: 'upload', phase: 1, uploadTotal: 100, agent: 'ag',
    })
    resolveUpload(resp('users/u/workspace/uploads/files/vid.mp4', 'vid.mp4', 100, 'tid-1', true))
    await flush()
    const linked = useTransferStore.getState().byId['tid-1']
    expect(linked).toBeTruthy()
    expect(linked.relPath).toBe('users/u/workspace/uploads/files/vid.mp4')
    expect(linked.doneAt).toBeNull() // remote_push=true → waits for machine events
    expect(useTransferStore.getState().byId['local:chat-fx']).toBeUndefined()
  })

  it('dequeues a removed queued entry without ever uploading it', async () => {
    let resolveFirst!: (v: unknown) => void
    uploadMock.mockImplementation(() => new Promise((res) => { resolveFirst = res }))
    const f1 = makeFile('a.bin', 10)
    const f2 = makeFile('b.bin', 20)
    const a1 = addPending('chat1', 'f1', f1)
    const a2 = addPending('chat1', 'f2', f2)
    enqueueChatUpload({ fileId: 'f1', file: f1, agent: 'ag', abort: a1 })
    enqueueChatUpload({ fileId: 'f2', file: f2, agent: 'ag', abort: a2 })
    await flush()
    expect(dequeueChatUpload('f2')).toBe(true)
    useChatStore.getState().removePendingFile('chat1', 'f2')
    resolveFirst(resp('p/a.bin', 'a.bin', 10, 't1'))
    await flush()
    expect(uploadMock).toHaveBeenCalledTimes(1)
  })

  it('keeps progress and completion flowing after the draft slice re-keys', async () => {
    let resolveUpload!: (v: unknown) => void
    uploadMock.mockImplementation(() => new Promise((res) => { resolveUpload = res }))
    const f = makeFile('c.bin', 1000)
    const a = addPending('new:agent-x', 'fk', f)
    enqueueChatUpload({ fileId: 'fk', file: f, agent: 'ag', abort: a })
    await flush()

    // Simulate transferNewChatToChat: the slice moves under a real chat id.
    const slice = useChatStore.getState().byChat['new:agent-x']
    useChatStore.setState({ byChat: { 'chat-99': { ...slice, chatId: 'chat-99' } } })

    // Progress written mid-upload must land in the NEW slice (resolved at
    // write time, not captured at enqueue time).
    const onProgress = uploadMock.mock.calls[0][3] as (sent: number, total: number) => void
    onProgress(500, 1000)
    const during = useChatStore.getState().byChat['chat-99'].pendingFiles[0]
    expect(during.uploadSent).toBe(500)

    resolveUpload(resp('p/c.bin', 'c.bin', 1000, 't9'))
    await flush()
    const done = useChatStore.getState().byChat['chat-99'].pendingFiles[0]
    expect(done.uploadedPath).toBe('p/c.bin')
    expect(done.uploading).toBe(false)
  })

  it('throttles progress store writes to spaced whole-percent steps', async () => {
    let resolveUpload!: (v: unknown) => void
    uploadMock.mockImplementation(() => new Promise((res) => { resolveUpload = res }))
    const f = makeFile('d.bin', 100)
    const a = addPending('chat1', 'ft', f)
    enqueueChatUpload({ fileId: 'ft', file: f, agent: 'ag', abort: a })
    await flush()
    const onProgress = uploadMock.mock.calls[0][3] as (sent: number, total: number) => void
    onProgress(50, 100) // first write goes through
    onProgress(60, 100) // same instant → throttled away
    const st = useChatStore.getState().byChat['chat1'].pendingFiles[0]
    expect(st.uploadSent).toBe(50)
    resolveUpload(resp('p/d.bin', 'd.bin', 100, 'tt'))
    await flush()
  })

  it('surfaces a failed upload on the chip and in transferStore', async () => {
    uploadMock.mockImplementation(() => Promise.reject(new Error('HTTP 413')))
    const f = makeFile('big.mp4', 10)
    const a = addPending('chat1', 'ff', f)
    enqueueChatUpload({ fileId: 'ff', file: f, agent: 'ag', abort: a })
    await flush()
    const st = useChatStore.getState().byChat['chat1'].pendingFiles[0]
    expect(st.error).toBe('HTTP 413')
    expect(st.uploading).toBe(false)
    expect(useTransferStore.getState().byId['local:chat-ff']?.uploadFailed).toBe(true)
  })

  it('treats an abort as removal: no error is written to the chip', async () => {
    uploadMock.mockImplementation(
      () => Promise.reject(new DOMException('Upload aborted', 'AbortError')),
    )
    const f = makeFile('e.bin', 10)
    const a = addPending('chat1', 'fa', f)
    enqueueChatUpload({ fileId: 'fa', file: f, agent: 'ag', abort: a })
    await flush()
    const st = useChatStore.getState().byChat['chat1'].pendingFiles[0]
    expect(st.error).toBeUndefined()
  })
})
