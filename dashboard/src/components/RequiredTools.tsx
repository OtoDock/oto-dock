/**
 * The tools a template needs, as the install dialogs show them: one line
 * that counts them with the Details toggle of the app block beside it, each
 * tool that needs something named with what it needs, and the ready ones
 * behind Details, the agent's own before the ones every agent has.
 */

import { useState } from 'react'
import { DetailsToggle } from './apps/AppApprovalCard'
import { type RequiredTool, toolNeed, toolNeedSentence, toolsCountLine, toolsNamedCountLine } from '../lib/status/requiredTools'

const NEED_COLOUR = {
  blocked: 'text-status-danger-fg',
  approval: 'text-status-warning-fg',
  adds: 'text-secondary',
} as const

/** `tools`: the install preview's rows, each with what it still needs.
 *  `names`: the store's list, names alone: the line counts them and its
 *  toggle lists them, with no readiness (a browse is one request). */
export default function RequiredTools({ tools = [], names }: {
  tools?: RequiredTool[]
  names?: { name: string; label?: string }[]
}) {
  const [open, setOpen] = useState(false)
  if (names) {
    if (names.length === 0) return null
    return (
      <div className="text-xs" data-testid="required-tools">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-medium text-primary" data-testid="tools-count">{toolsNamedCountLine(names.length)}</span>
          <DetailsToggle open={open} onToggle={() => setOpen(o => !o)} />
        </div>
        {open && (
          <ul className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-secondary">
            {names.map(t => <li key={t.name} className="min-w-0 break-words">{t.label || t.name}</li>)}
          </ul>
        )}
      </div>
    )
  }
  if (tools.length === 0) return null
  const needing = tools.filter(t => toolNeed(t) !== 'none')
  const ready = tools.filter(t => toolNeed(t) === 'none')
  const groups = [
    { title: 'Added for this agent', tools: ready.filter(t => !t.core) },
    { title: 'Every agent has these', tools: ready.filter(t => t.core) },
  ].filter(g => g.tools.length > 0)
  return (
    <div className="text-xs" data-testid="required-tools">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium text-primary" data-testid="tools-count">{toolsCountLine(tools)}</span>
        {ready.length > 0 && <DetailsToggle open={open} onToggle={() => setOpen(o => !o)} />}
      </div>
      {needing.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {needing.map(t => {
            const need = toolNeed(t)
            return (
              <li key={t.name} data-testid={`tool-${t.name}`}>
                <span className="font-medium text-primary">{t.label}</span>{' '}
                <span className={need === 'none' ? '' : NEED_COLOUR[need]}>{toolNeedSentence(t)}</span>
              </li>
            )
          })}
        </ul>
      )}
      {open && (
        <div className="mt-2 space-y-2">
          {groups.map(g => (
            <div key={g.title}>
              {groups.length > 1 && <div className="mb-0.5 text-tertiary">{g.title}</div>}
              <ul className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-secondary">
                {g.tools.map(t => <li key={t.name} className="min-w-0 break-words">{t.label}</li>)}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
