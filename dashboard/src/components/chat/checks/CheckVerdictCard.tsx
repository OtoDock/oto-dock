import { useState } from 'react'

import { isLocalTarget } from '../../../lib/placement'

// A check's verdict on a turn (proxy CHECKS.md "Rendering"): one compact
// line — the check, the verdict, the round — with the summary and the
// first findings behind a fold. Short on purpose; the whole verdict is on
// the agent's Checks page.

const TONE: Record<string, string> = {
  pass: 'border-green-300 bg-green-50 text-green-900 dark:border-green-800 dark:bg-green-900/20 dark:text-green-200',
  fail: 'border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200',
  error: 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-200',
  skipped: 'border-p-border-light bg-p-surface text-p-text-secondary',
}
const WORDS: Record<string, string> = { pass: 'passed', fail: 'did not pass', error: 'could not run', skipped: 'skipped' }

export default function CheckVerdictCard({ check, status, score, summary, findings, findingsTotal, round, rounds, ranOn, costUsd }: {
  check: string; status: string; score?: number | null; summary: string
  findings: Array<{ location: string; severity: string; text: string }>; findingsTotal: number
  round: number; rounds: number; ranOn: string; costUsd: number
}) {
  const [open, setOpen] = useState(status === 'fail')
  const where = isLocalTarget(ranOn) ? 'on the platform' : `on machine ${ranOn.slice(0, 8)}`
  return (
    <div className={`my-1.5 rounded-lg border px-2.5 py-1.5 text-xs ${TONE[status] || TONE.error}`} data-testid="check-verdict-card">
      <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((v) => !v)}>
        <span className="shrink-0" aria-hidden>{status === 'pass' ? '✓' : status === 'fail' ? '✗' : '!'}</span>
        <span className="font-medium">Check {check} {WORDS[status] || status}</span>
        {score != null && <span className="opacity-80">score {score}</span>}
        {/* `rounds` is the fix rounds the check allows: one evaluation more than that. */}
        <span className="opacity-70">round {round} of {(rounds || 0) + 1} · {where}{costUsd ? ` · $${costUsd.toFixed(2)}` : ''}</span>
        <span className="ml-auto opacity-60">{open ? '▾' : '▸'}</span>
      </button>
      {open && (summary || findings.length > 0) && (
        <div className="mt-1 space-y-1">
          {summary && <p className="opacity-90">{summary}</p>}
          {findings.length > 0 && (
            <ol className="list-decimal pl-4 space-y-0.5">
              {findings.map((f, i) => (
                <li key={i}>
                  <span className="opacity-70">[{f.severity}]</span>{f.location ? <> <code className="font-mono">{f.location}</code> —</> : ''} {f.text}
                </li>
              ))}
              {findingsTotal > findings.length && <li className="list-none opacity-70">… {findingsTotal - findings.length} more on the Checks page</li>}
            </ol>
          )}
        </div>
      )}
    </div>
  )
}
