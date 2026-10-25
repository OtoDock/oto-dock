// A folder attached in chat: the chip shows at once while a dropped folder
// is read (Send waits on it), every file is checked against the upload cap
// before anything is reserved, a Retry resumes in the folder reserved
// before, and a folder chip stays a folder when it is rebuilt from a sent
// message.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

const uploadMock = vi.fn()
const reserveMock = vi.fn()
vi.mock('@/lib/uploadWithProgress', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/uploadWithProgress')>()),
  uploadWithProgress: (...args: unknown[]) => uploadMock(...args),
  reserveUploadFolder: (...args: unknown[]) => reserveMock(...args),
}))
// A 1000-byte install cap.
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { feature_flags: { upload_max_bytes: 1000 } } }),
}))

import { lastTurnPrompt } from '@/lib/messageBlocks'
import { emptyCollected, type CollectedItems } from '@/lib/droppedItems'
import { useChatAttachments } from '@/pages/agent/chat/useChatAttachments'
import { useChatStore } from '@/store/chatStore'

const KEY = 'chat-f'
const flush = () => new Promise((r) => setTimeout(r, 0))
const chip = () => useChatStore.getState().byChat[KEY].pendingFiles[0]

function collected(files: Array<[string, string, number]>, extra: Partial<CollectedItems> = {}): CollectedItems {
  return {
    ...emptyCollected(files.map(([relDir, name, size]) => ({ file: new File([new Uint8Array(size)], name), relDir }))),
    ...extra,
  }
}

beforeEach(() => {
  useChatStore.setState({ byChat: {} })
  uploadMock.mockReset()
  reserveMock.mockReset()
  reserveMock.mockResolvedValue({ path: 'users/u/workspace/uploads/files/mesitis', name: 'mesitis' })
  uploadMock.mockImplementation((f: File, _a: string, target: string) => Promise.resolve({
    path: `${target}/${f.name}`, filename: f.name, size: f.size, transfer_id: 't', remote_push: false,
  }))
})

describe('handleAddFolder', () => {
  it('shows the chip at once, uploading, and fills it in when the walk ends', async () => {
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ag', draftKey: KEY }))
    let finish!: (items: CollectedItems) => void
    act(() => result.current.handleAddFolder('mesitis', new Promise((res) => { finish = res })))
    expect(chip()).toMatchObject({ name: 'mesitis', folder: true, uploading: true, queued: true })
    expect(chip().fileCount).toBeUndefined()
    expect(reserveMock).not.toHaveBeenCalled()

    await act(async () => {
      finish(collected([['', 'a.md', 3], ['context', 'b.md', 4]], { leftOut: 1, leftOutNames: ['.git'] }))
      for (let i = 0; i < 10; i++) await flush()
    })
    expect(reserveMock).toHaveBeenCalledWith('ag', 'mesitis', expect.any(AbortSignal))
    expect(chip()).toMatchObject({
      fileCount: 2, size: 7, uploading: false, filesDone: 2,
      uploadedPath: 'users/u/workspace/uploads/files/mesitis', note: '1 folder left out: .git',
    })
  })

  it('refuses a folder with a file over the cap before anything is reserved', async () => {
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ag', draftKey: KEY }))
    await act(async () => {
      result.current.handleAddFolder('mesitis', collected([['', 'a.md', 3], ['media', 'clip.mp4', 2000]]))
      for (let i = 0; i < 5; i++) await flush()
    })
    expect(reserveMock).not.toHaveBeenCalled()
    expect(uploadMock).not.toHaveBeenCalled()
    expect(chip()).toMatchObject({ uploading: false, fileCount: 2 })
    expect(chip().error).toMatch(/^media\/clip\.mp4: over the .* upload limit$/)
    expect(chip().folderFiles).toBeUndefined() // no Retry for it
  })

  it('a removed chip stops a walk still running: nothing is reserved', async () => {
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ag', draftKey: KEY }))
    let finish!: (items: CollectedItems) => void
    act(() => result.current.handleAddFolder('mesitis', new Promise((res) => { finish = res })))
    act(() => result.current.handleRemoveFile(chip().id))
    await act(async () => {
      finish(collected([['', 'a.md', 3]]))
      for (let i = 0; i < 5; i++) await flush()
    })
    expect(reserveMock).not.toHaveBeenCalled()
    expect(useChatStore.getState().byChat[KEY].pendingFiles).toEqual([])
  })

  it('Retry resumes a failed folder at its failed file, in the same folder', async () => {
    uploadMock.mockImplementationOnce((f: File, _a: string, target: string) => Promise.resolve({
      path: `${target}/${f.name}`, filename: f.name, size: f.size, transfer_id: 't', remote_push: false,
    })).mockImplementationOnce(() => Promise.reject(new Error('HTTP 502')))
    const { result } = renderHook(() => useChatAttachments({ agentName: 'ag', draftKey: KEY }))
    await act(async () => {
      result.current.handleAddFolder('mesitis', collected([['', 'a.md', 3], ['', 'b.md', 4]]))
      for (let i = 0; i < 10; i++) await flush()
    })
    expect(chip()).toMatchObject({ error: 'b.md: HTTP 502', filesDone: 1 })
    await act(async () => {
      result.current.handleRetryFile(chip().id)
      for (let i = 0; i < 10; i++) await flush()
    })
    expect(reserveMock).toHaveBeenCalledTimes(1)
    expect(uploadMock.mock.calls.map((c) => (c[0] as File).name)).toEqual(['a.md', 'b.md', 'b.md'])
    expect(chip()).toMatchObject({ uploading: false, filesDone: 2, error: undefined })
  })
})

describe('a folder chip rebuilt from a sent message', () => {
  it('stays a folder in Send again', () => {
    const again = lastTurnPrompt([{
      id: 'u1', role: 'user', createdAt: '',
      blocks: [
        { type: 'file_attachments', files: [
          { name: 'mesitis', path: 'users/u/workspace/uploads/files/mesitis', folder: true },
          { name: 'a.pdf', path: 'users/u/workspace/uploads/files/a.pdf' },
        ] },
        { type: 'text', content: 'look' },
      ],
    }] as never)
    expect(again?.files.map((f) => [f.name, f.folder ?? false])).toEqual([['mesitis', true], ['a.pdf', false]])
  })
})
