import { useState } from 'react'
import { useAllowMcpHosts, type BearerPending } from '../../api/oauth'
import Button from '../ui/Button'

// The addresses an MCP sends people's sign-in to, waiting for an admin's
// Allow: on the ZIP install card and in Admin, Security. Allow sends back
// exactly what is shown here, so a manifest that changed since is refused.
export default function AllowMcpHosts({ pending }: { pending: BearerPending }) {
  const allow = useAllowMcpHosts()
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  return (
    <div className="rounded-card border border-status-warning-border bg-status-warning-bg p-3 text-xs text-secondary">
      <p className="text-primary">
        {pending.mcp_label} sends people&apos;s {pending.provider} sign-in to{' '}
        <span className="font-mono">{pending.hosts.join(', ')}</span>.
      </p>
      {pending.shared_with.length > 0 && (
        <p className="mt-1">The same sign-in also serves {pending.shared_with.join(', ')}.</p>
      )}
      <div className="mt-2 flex items-center gap-3">
        {done ? (
          <span className="text-status-success-fg">Allowed.</span>
        ) : (
          <Button variant="primary" size="xs" disabled={allow.isPending}
            onClick={() => {
              setError('')
              allow.mutateAsync(pending).then(() => setDone(true),
                (e: unknown) => setError(e instanceof Error ? e.message : 'The addresses were not allowed.'))
            }}
          >
            {allow.isPending ? 'Allowing…' : 'Allow these addresses'}
          </Button>
        )}
        {error && <span className="text-status-danger-fg">{error}</span>}
      </div>
    </div>
  )
}
