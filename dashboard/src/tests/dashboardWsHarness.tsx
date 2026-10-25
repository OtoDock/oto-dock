// The dashboard socket under test: useDashboardWs rendered inside a query
// client (it patches query caches), connected, and its first fake socket
// opened. The global WebSocket must already be stubbed with FakeWebSocket.
import type { ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useDashboardWs } from '@/hooks/useDashboardWs'
import { FakeWebSocket } from './fakeWebSocket'

/** `callbacks` builds the hook's options on every render, as an inline
 * `useDashboardWs({...})` would. */
export function connectDashboardWs(callbacks: () => Parameters<typeof useDashboardWs>[0]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const hook = renderHook(() => useDashboardWs(callbacks()), { wrapper })
  act(() => { hook.result.current.connect() })
  const ws = FakeWebSocket.instances[0]
  act(() => { ws.onopen?.() })
  return { hook, ws }
}
