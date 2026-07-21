import { describe, it, expect, beforeEach } from 'vitest'
import { useInstallStore, installKey } from '@/store/installStore'

// ─── WS-reconnect reconciliation: clearInFlight drops non-terminal install
//     slices (a lifecycle whose done/failed frame was lost on a dropped
//     socket — e.g. a proxy restart mid-install — otherwise showed
//     "Preparing remote environment…" forever). Terminal slices keep their
//     done-grace / failed-with-retry UI; a genuinely running install is
//     re-fed by the server's connect replay right after. ─────────────────────

const M = 'machine-1'

beforeEach(() => {
  useInstallStore.setState({ byKey: {} })
})

describe('installStore.clearInFlight', () => {
  it('drops installing and verifying slices', () => {
    const s = useInstallStore.getState()
    s.begin({ machine_id: M, agent: 'a1' })
    s.begin({ machine_id: M, agent: 'a2' })
    s.verifying({ machine_id: M, agent: 'a2' })
    useInstallStore.getState().clearInFlight()
    expect(useInstallStore.getState().byKey[installKey(M, 'a1')]).toBeUndefined()
    expect(useInstallStore.getState().byKey[installKey(M, 'a2')]).toBeUndefined()
  })

  it('keeps terminal slices (done grace + failed retry UI)', () => {
    const s = useInstallStore.getState()
    s.begin({ machine_id: M, agent: 'done-agent' })
    s.finish({ machine_id: M, agent: 'done-agent' })
    s.begin({ machine_id: M, agent: 'failed-agent' })
    s.fail({ machine_id: M, agent: 'failed-agent', error: 'boom' })
    useInstallStore.getState().clearInFlight()
    expect(useInstallStore.getState().byKey[installKey(M, 'done-agent')]?.status).toBe('done')
    expect(useInstallStore.getState().byKey[installKey(M, 'failed-agent')]?.status).toBe('failed')
  })

  it('is a no-op state-wise when nothing is in flight', () => {
    const s = useInstallStore.getState()
    s.begin({ machine_id: M, agent: 'a1' })
    s.finish({ machine_id: M, agent: 'a1' })
    const before = useInstallStore.getState().byKey
    useInstallStore.getState().clearInFlight()
    expect(useInstallStore.getState().byKey).toBe(before)
  })

  it('a replayed install re-materializes after the clear (server is source of truth)', () => {
    const s = useInstallStore.getState()
    s.begin({ machine_id: M, agent: 'a1' })
    useInstallStore.getState().clearInFlight()
    // Connect replay re-feeds the genuinely running install's history.
    s.begin({ machine_id: M, agent: 'a1' })
    s.recordProgress({ machine_id: M, agent: 'a1', mcp: 'file-tools', phase: 'pip', pct: 50, message: '' })
    expect(useInstallStore.getState().byKey[installKey(M, 'a1')]?.status).toBe('installing')
    expect(useInstallStore.getState().byKey[installKey(M, 'a1')]?.progress['file-tools']?.pct).toBe(50)
  })
})
