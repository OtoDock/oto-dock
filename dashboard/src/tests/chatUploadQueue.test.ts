// The chat attachment upload queue — the module-level singleton that
// serializes every chat upload onto ONE wire chain (3×300MB picked together
// must not run in parallel), keeps writing progress through the draft→chat
// re-key, and wires transferStore for the phase-2 machine-sync join.

import { describe, it, expect, vi, beforeEach } from 'vitest'

const uploadMock = vi.fn()
vi.mock('@/lib/uploadWithProgress', () => ({
  uploadWithProgress: (...args: unknown[]) => uploadMock(...args),
}))

import { enqueueChatUpload, dequeueChatUpload, chatUploadQueueSize } from '@/lib/chatUploadQueue'
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
