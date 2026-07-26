// transferStore (Feature E, 1.4.0): local upload lifecycle + link re-key,
// server event application, section mapping, prune linger, reconnect
// reconciliation. Plain-store style (mirrors installStoreReconnect.test.ts).

import { describe, it, expect, beforeEach } from 'vitest'

import {
  useTransferStore,
  sectionForRelPath,
  transfersForSection,
  isTransferActive,
} from '../store/transferStore'

const S = () => useTransferStore.getState()

beforeEach(() => {
  useTransferStore.setState({ byId: {} })
})

describe('sectionForRelPath', () => {
  it('maps every section prefix', () => {
    expect(sectionForRelPath('users/alice/workspace/a.mp4', 'alice')).toBe('my-workspace')
    expect(sectionForRelPath('users/alice/context/notes.md', 'alice')).toBe('my-context')
    expect(sectionForRelPath('workspace/report.pdf', 'alice')).toBe('agent-workspace')
    expect(sectionForRelPath('knowledge/facts.md', 'alice')).toBe('agent-knowledge')
    expect(sectionForRelPath('config/agent.md', 'alice')).toBe('agent-config')
  })
  it('returns null for foreign users and unknown roots', () => {
    expect(sectionForRelPath('users/bob/workspace/a.mp4', 'alice')).toBeNull()
    expect(sectionForRelPath('users/alice/workspace/a.mp4', undefined)).toBeNull()
    expect(sectionForRelPath('somewhere/else.bin', 'alice')).toBeNull()
  })
})

describe('local upload lifecycle', () => {
  it('begin → progress → link re-keys to the server id', () => {
    S().beginLocalUpload({
      clientId: 'local:abc', agent: 'a1', targetDir: 'workspace',
      filename: 'v.mp4', bytesTotal: 100,
    })
    S().updateLocalUpload('local:abc', 40)
    expect(S().byId['local:abc'].uploadSent).toBe(40)
    expect(S().byId['local:abc'].phase).toBe(1)

    S().linkUpload('local:abc', {
      transferId: 'srv-1', relPath: 'workspace/v.mp4', remotePush: true,
    })
    expect(S().byId['local:abc']).toBeUndefined()
    const t = S().byId['srv-1']
    expect(t.phase).toBe(2)
    expect(t.relPath).toBe('workspace/v.mp4')
    expect(t.uploadSent).toBe(100) // upload leg complete on link
    expect(t.doneAt).toBeNull()    // phase 2 pending
  })

  it('remotePush=false completes the item at link time', () => {
    S().beginLocalUpload({
      clientId: 'local:x', agent: 'a1', targetDir: '', filename: 'f.txt', bytesTotal: 5,
    })
    S().linkUpload('local:x', { transferId: 'srv-2', relPath: 'f.txt', remotePush: false })
    expect(S().byId['srv-2'].doneAt).not.toBeNull()
  })

  it('an early transfer_started merges into the linked item', () => {
    S().beginLocalUpload({
      clientId: 'local:y', agent: 'a1', targetDir: 'workspace',
      filename: 'v.mp4', bytesTotal: 100,
    })
    // Server events raced ahead of the POST response:
    S().applyStarted({
      transfer_id: 'srv-3', agent_slug: 'a1', rel_path: 'workspace/v.mp4',
      filename: 'v.mp4', kind: 'upload', bytes_total: 100,
      machines: [{ machine_id: 'm1', name: 'Desktop', state: 'queued' }],
    })
    S().linkUpload('local:y', { transferId: 'srv-3', relPath: 'workspace/v.mp4', remotePush: true })
    const t = S().byId['srv-3']
    expect(Object.keys(t.machines)).toEqual(['m1'])
    expect(t.machines.m1.name).toBe('Desktop')
  })

  it('failLocalUpload marks failed + lingers', () => {
    S().beginLocalUpload({
      clientId: 'local:z', agent: 'a1', targetDir: '', filename: 'f.txt', bytesTotal: 5,
    })
    S().failLocalUpload('local:z')
    const t = S().byId['local:z']
    expect(t.uploadFailed).toBe(true)
    expect(t.doneAt).not.toBeNull()
    expect(isTransferActive(t)).toBe(true) // linger window shows the failure
  })
})

describe('server events', () => {
  const started = {
    transfer_id: 't1', agent_slug: 'a1', rel_path: 'workspace/big.bin',
    filename: 'big.bin', kind: 'sync', bytes_total: 1000,
    machines: [
      { machine_id: 'm1', name: 'Desk', state: 'queued', bytes_total: 1000 },
      { machine_id: 'm2', name: 'Lap', state: 'queued', bytes_total: 1000 },
    ],
  }

  it('applies started/progress/machine_state/done', () => {
    S().applyStarted(started)
    expect(Object.keys(S().byId.t1.machines)).toHaveLength(2)

    S().applyProgress({ transfer_id: 't1', machine_id: 'm1', bytes_sent: 500, bytes_total: 1000 })
    expect(S().byId.t1.machines.m1.state).toBe('active')
    expect(S().byId.t1.machines.m1.bytesSent).toBe(500)

    S().applyMachineState({ transfer_id: 't1', machine_id: 'm1', state: 'done' })
    expect(S().byId.t1.machines.m1.bytesSent).toBe(1000)

    S().applyMachineState({ transfer_id: 't1', machine_id: 'm2', state: 'failed', error: 'offline' })
    expect(S().byId.t1.machines.m2.error).toBe('offline')

    S().applyDone({ transfer_id: 't1', ok: false })
    expect(S().byId.t1.doneAt).not.toBeNull()
  })

  it('snapshot upserts full state (connect replay)', () => {
    S().applySnapshot({
      transfer_id: 't2', agent_slug: 'a1', rel_path: 'workspace/x.bin',
      filename: 'x.bin', kind: 'sync', bytes_total: 10,
      machines: [{ machine_id: 'm1', name: 'Desk', state: 'active', bytes_sent: 4, bytes_total: 10 }],
    })
    const t = S().byId.t2
    expect(t.machines.m1.bytesSent).toBe(4)
    expect(t.doneAt).toBeNull()
  })
})

describe('prune + clearInFlight', () => {
  it('prune drops items past the linger window', () => {
    S().applyStarted({
      transfer_id: 't3', agent_slug: 'a1', rel_path: 'workspace/x.bin',
      filename: 'x.bin', kind: 'sync', bytes_total: 1, machines: [],
    })
    S().applyDone({ transfer_id: 't3', ok: true })
    useTransferStore.setState((s) => ({
      byId: { t3: { ...s.byId.t3, doneAt: Date.now() - 10_000 } },
    }))
    S().prune()
    expect(S().byId.t3).toBeUndefined()
  })

  it('clearInFlight keeps phase-1 + terminal items, drops in-flight sync ghosts', () => {
    S().beginLocalUpload({
      clientId: 'local:p1', agent: 'a1', targetDir: '', filename: 'f', bytesTotal: 1,
    })
    S().applyStarted({
      transfer_id: 'ghost', agent_slug: 'a1', rel_path: 'workspace/g.bin',
      filename: 'g.bin', kind: 'sync', bytes_total: 1,
      machines: [{ machine_id: 'm1', state: 'active' }],
    })
    S().clearInFlight()
    expect(S().byId['local:p1']).toBeDefined()
    expect(S().byId.ghost).toBeUndefined()
  })
})

describe('transfersForSection', () => {
  it('filters by agent + section and sorts by start', () => {
    S().applyStarted({
      transfer_id: 'w1', agent_slug: 'a1', rel_path: 'workspace/one.bin',
      filename: 'one.bin', kind: 'sync', bytes_total: 1, machines: [],
    })
    S().applyStarted({
      transfer_id: 'p1', agent_slug: 'a1', rel_path: 'users/alice/workspace/two.bin',
      filename: 'two.bin', kind: 'sync', bytes_total: 1, machines: [],
    })
    S().applyStarted({
      transfer_id: 'other', agent_slug: 'a2', rel_path: 'workspace/three.bin',
      filename: 'three.bin', kind: 'sync', bytes_total: 1, machines: [],
    })
    const shared = transfersForSection(S().byId, 'a1', 'agent-workspace', 'alice')
    expect(shared.map((t) => t.id)).toEqual(['w1'])
    const personal = transfersForSection(S().byId, 'a1', 'my-workspace', 'alice')
    expect(personal.map((t) => t.id)).toEqual(['p1'])
    expect(transfersForSection(S().byId, 'a1', '', 'alice')).toEqual([])
  })
})
