/**
 * The apps and checks a community template brings, one folded line in the
 * template panel. The install and update dialogs keep who gets each app in
 * view, its own program said in the same sentence, with the one approval;
 * a department counts its agents' apps in one line with the program counted
 * in view and lists them under each agent's name; the store shows the line
 * alone, the catalog's description and picture of each app behind its toggle. What each
 * app may do is behind the toggle, in the deploy card's short list.
 */

import { useState } from 'react'
import { SCOPE } from '../../lib/visibility'
import { DetailsToggle, ManifestSummary } from '../apps/AppApprovalCard'
import { mediaServerSentence } from '../apps/appRow'
import {
  type TemplateApp, type TemplateCheck, appChangeSentence, appWhoSentence, approvalSentence,
  appAsksNothing, appsAndChecksCountLine, appsCountLine, appsServerCountSentence, checksCountLine,
} from '../../lib/status/templateApps'

export interface TemplateApproval {
  checked: boolean
  onChange: (checked: boolean) => void
  scope: 'everyone' | 'own'
  testId: string
  hint: string
}

/** One agent of a department, with what it brings. */
export interface TemplateAppsGroup {
  key: string
  title: string
  apps: TemplateApp[]
  checks: TemplateCheck[]
}

export default function TemplateApps({ apps, checks, approval, groups, store = false }: {
  apps: TemplateApp[]
  checks: TemplateCheck[]
  approval?: TemplateApproval
  groups?: TemplateAppsGroup[]
  /** The store: the counting line alone, everything else behind its toggle. */
  store?: boolean
}) {
  const [open, setOpen] = useState(false)
  if (apps.length === 0 && checks.length === 0) return null
  const toggle = <DetailsToggle open={open} onToggle={() => setOpen(o => !o)} />
  return (
    <div className="text-xs" data-testid="template-apps">
      {groups ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium text-primary" data-testid="apps-count">{appsAndChecksCountLine(apps, checks)}</span>
            {toggle}
          </div>
          {!store && appsServerCountSentence(apps) && (
            <p className="mt-1 text-primary" data-testid="apps-server-count">{appsServerCountSentence(apps)}</p>
          )}
          {open && groups.filter(g => g.apps.length + g.checks.length > 0).map(g => (
            <div key={g.key} className="mt-2" data-testid={`template-apps-group-${g.key}`}>
              <p className="font-medium text-primary">{g.title}</p>
              {g.apps.map(a => <AppRow key={a.slug} app={a} open />)}
              <Checks checks={g.checks} />
            </div>
          ))}
        </>
      ) : (
        <>
          {apps.length > 0 && (
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium text-primary" data-testid="apps-count">{appsCountLine(apps)}</span>
              {toggle}
            </div>
          )}
          {(!store || open) && apps.map(a => <AppRow key={a.slug} app={a} open={open} />)}
          {checks.length > 0 && (
            <div className={`flex flex-wrap items-baseline gap-x-2 ${apps.length > 0 ? 'mt-2' : ''}`}>
              <span className="font-medium text-primary" data-testid="checks-count">{checksCountLine(checks)}</span>
              {apps.length === 0 && toggle}
            </div>
          )}
          {open && <Checks checks={checks} />}
        </>
      )}
      {approval && (
        <>
          <label className="mt-3 flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={approval.checked}
              onChange={e => approval.onChange(e.target.checked)}
              className="mt-0.5 accent-action"
              data-testid={approval.testId}
            />
            <span className="text-primary font-medium">{approvalSentence(approval.scope, groups ? groups.length : 1)}</span>
          </label>
          <p className="mt-1 text-tertiary">{approval.hint}</p>
        </>
      )}
    </div>
  )
}

/** One app: who gets it with its own program, then under the toggle what it
 *  may do (the install's manifest row) or the catalog's line about it. */
function AppRow({ app: a, open }: { app: TemplateApp; open: boolean }) {
  return (
    <div className="mt-1.5" data-testid={`consent-app-${a.slug}`}>
      <p className={a.adds_server ? 'text-primary' : 'text-secondary'}>
        {[appChangeSentence(a), appWhoSentence(a)].filter(Boolean).join(' ')}
      </p>
      {open && a.row && (
        <div className="mt-1.5 rounded-card bg-surface-muted p-2" data-testid={`consent-app-details-${a.slug}`}>
          <p className="font-medium text-primary">What {a.title} may do</p>
          {a.row.manifest_empty
            ? <p className="mt-1 text-secondary" data-testid={`consent-app-nothing-${a.slug}`}>{appAsksNothing(a)}</p>
            : <ManifestSummary app={a.row} />}
          {mediaServerSentence(a.row) && (
            <p className="mt-1 text-secondary" data-testid="media-server-line">{mediaServerSentence(a.row)}</p>
          )}
          {(a.blueprint_tasks?.length ?? 0) > 0 && (
            <div className="mt-1.5 text-secondary">
              <p>The tasks its buttons run:</p>
              <ul className="mt-0.5 space-y-1 pl-3">
                {a.blueprint_tasks!.map(t => (
                  <li key={t.slug}>
                    <span className="font-medium text-primary">{t.description || t.slug}</span>
                    <span className="block break-words">“{t.prompt.slice(0, 300)}{t.prompt.length > 300 ? '…' : ''}”</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(a.blueprint_triggers?.length ?? 0) > 0 && (
            <p className="mt-1.5 text-secondary" data-testid={`consent-app-triggers-${a.slug}`}>
              Comes with {a.blueprint_triggers!.length === 1 ? 'a trigger' : `${a.blueprint_triggers!.length} triggers`}
              {a.visibility === SCOPE.USER ? ' of each member’s own' : ''}: {a.blueprint_triggers!.map(t => t.description || t.slug).join(', ')}
            </p>
          )}
        </div>
      )}
      {open && !a.row && (a.description || a.picture) && (
        <div className="mt-0.5 flex items-start gap-2 text-secondary" data-testid={`consent-app-about-${a.slug}`}>
          {a.picture && <img src={a.picture} alt="" className="w-12 h-8 rounded-sm object-cover shrink-0 bg-surface-muted" />}
          {a.description && <p className="min-w-0">{a.description}</p>}
        </div>
      )}
    </div>
  )
}

/** Each check's description, whether it is mandatory, and its words. */
function Checks({ checks }: { checks: TemplateCheck[] }) {
  if (!checks.length) return null
  return (
    <ul className="mt-1.5 space-y-1 rounded-card bg-surface-muted p-2 text-secondary">
      {checks.map(c => (
        <li key={c.name} data-testid={`consent-check-${c.name}`}>
          <span className="font-medium text-primary">{c.description || c.name}</span>
          {c.mandatory ? <span>, mandatory</span> : null}
          {c.words ? <span className="block">{c.words}</span> : null}
        </li>
      ))}
    </ul>
  )
}
