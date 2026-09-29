import { useState } from 'react'
import { useApproveDeploy, useDeployStatus, useRejectDeploy, type PinnedApp } from '../../api/apps'
import {
  AppCardShell, DetailsToggle, ExactManifest, ExactManifestToggle, ManifestBlocks, ManifestSummary, SecretsGroup,
  type SummaryLine,
} from './AppApprovalCard'
import AppSettingsPanel from './AppSettingsPanel'

/**
 * A folder app's release waiting for a person (APPS.md "Deploy pipeline"):
 * the agent deployed, but app.json changed (or the app's deploys always
 * wait, or a required secret has no value yet), so the copy sits next to
 * the live release until the owner or an editor approves it here — one
 * click that also approves the manifest it carries — or rejects it, which
 * removes the copy and keeps the live release and its manifest. Rendered in
 * the approval card's slot whenever `app.deploy_state === 'pending'`, in
 * the approval card's shell: pinned header, a body that opens with the
 * plain list (the files the release changes, then what the app may do),
 * the file names and the full groups behind "Details", pinned Approve /
 * Reject. While a required secret is not set (`status.waiting`), the card
 * says which, Approve stays off and "Set secrets" opens the settings panel
 * (APPS.md "Secrets").
 */

interface Props {
  app: PinnedApp
  agent: string
}

function FileList({ label, files }: { label: string; files: string[] }) {
  if (!files.length) return null
  return (
    <li>
      <span className="font-medium text-p-text">{files.length} {label}</span>
      <span className="text-p-text-light"> — {files.slice(0, 6).join(', ')}{files.length > 6 ? ', …' : ''}</span>
    </li>
  )
}

/** "Adds 3 files, changes 2 and removes 1" — the counts alone. */
function changesLine(c: { added: string[]; changed: string[]; removed: string[] }): SummaryLine | null {
  const parts = [
    c.added.length ? `adds ${c.added.length} file${c.added.length > 1 ? 's' : ''}` : '',
    c.changed.length ? `changes ${c.changed.length}` : '',
    c.removed.length ? `removes ${c.removed.length}` : '',
  ].filter(Boolean)
  if (!parts.length) return null
  const text = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0]
  return { key: 'changes', testId: 'deploy-changes', text: text[0].toUpperCase() + text.slice(1) }
}

export default function AppDeployCard({ app, agent }: Props) {
  const { data: status } = useDeployStatus(app.id)
  const approve = useApproveDeploy(agent)
  const reject = useRejectDeploy(agent)
  const [showManifest, setShowManifest] = useState(false)
  const [details, setDetails] = useState(false)
  const [settings, setSettings] = useState(false)
  const pending = status?.pending_release ?? app.pending_release ?? 0
  const live = status?.release ?? app.release ?? 0
  const changes = status?.changes
  const waiting = status?.waiting ?? ''
  const lowersApproval = status?.lowers_approval === true
  const manifestChanged = status ? !status.manifest_approved : !app.actions_approved
  const showsManifest = manifestChanged && !(app.manifest_empty ?? app.actions.length === 0)
  const busy = approve.isPending || reject.isPending
  const error = (approve.error || reject.error) as Error | null
  // The card's row carries the declared names; the status carries the set
  // flags a moment fresher, so the lines read them from there when it has them.
  const cardApp = status?.secrets ? { ...app, secrets: status.secrets } : app
  const onSetSecret = app.can_manage ? () => setSettings(true) : undefined
  const before = changes ? [changesLine(changes)].filter((l): l is SummaryLine => l !== null) : []

  return (
    <AppCardShell
      testId="app-deploy-card"
      header={(
        <>
          <p className="font-medium text-p-text">
            Release {pending} of “{app.title || app.slug}” is waiting for approval
            {live ? ` — viewers keep release ${live} meanwhile.` : ' — nothing serves until then.'}
          </p>
          <p className="mt-0.5 text-p-text-secondary">
            {manifestChanged
              ? 'What the app may do changed with this release: review it before it goes live.'
              : waiting
                ? 'The manifest is approved; a secret it needs has no value yet.'
                : lowersApproval
                  ? 'Deploys of this app wait for a person until this release is approved.'
                  : 'Deploys of this app always wait for a person.'}
          </p>
          {lowersApproval && (
            <p className="mt-0.5 text-amber-600 dark:text-amber-400" data-testid="deploy-lowers-approval">
              Approving this release also turns off approval: later deploys of this app go live without asking.
            </p>
          )}
          {waiting && (
            <p className="mt-0.5 text-amber-600 dark:text-amber-400" data-testid="deploy-waiting">
              Waiting for a secret: {waiting}. Set it in the app’s settings, then approve.
            </p>
          )}
        </>
      )}
      body={(
        <>
          <ManifestSummary
            app={cardApp}
            onSetSecret={onSetSecret}
            before={before}
            only={showsManifest ? undefined : waiting ? ['secrets'] : []}
          />
          {details && (
            <>
              {changes && (
                <ul className="mt-1.5 space-y-0.5 text-p-text-secondary" data-testid="deploy-files">
                  <FileList label="added" files={changes.added} />
                  <FileList label="changed" files={changes.changed} />
                  <FileList label="removed" files={changes.removed} />
                </ul>
              )}
              {showsManifest ? (
                <ManifestBlocks app={cardApp} onSetSecret={onSetSecret} />
              ) : waiting ? (
                <div className="mt-1.5 space-y-1.5 text-p-text-secondary">
                  <SecretsGroup app={cardApp} onSetSecret={onSetSecret} />
                </div>
              ) : null}
            </>
          )}
          {showsManifest && showManifest && <ExactManifest app={cardApp} />}
          {settings && <AppSettingsPanel app={app} agent={agent} onClose={() => setSettings(false)} />}
        </>
      )}
      footer={(
        <>
          {app.can_manage ? (
            <>
              <button
                onClick={() => approve.mutate({ appId: app.id, release: pending, sig: app.actions_sig })}
                disabled={busy || !!waiting}
                title={waiting ? `${waiting} — set it first` : undefined}
                className="rounded-md bg-emerald-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-emerald-700 disabled:opacity-60"
              >
                {approve.isPending ? 'Starting…' : 'Approve and go live'}
              </button>
              <button
                onClick={() => reject.mutate({ appId: app.id })}
                disabled={busy}
                className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover disabled:opacity-60"
              >
                Reject
              </button>
              {waiting && (
                <button
                  onClick={() => setSettings(true)}
                  className="rounded-md border border-amber-500/50 px-2.5 py-1 font-medium text-amber-700 transition-colors hover:bg-amber-500/10 dark:text-amber-300"
                  data-testid="deploy-set-secrets"
                >
                  Set secrets
                </button>
              )}
            </>
          ) : (
            <span className="text-p-text-light">
              {app.scope === 'shared' ? 'An editor of this agent decides.' : 'The owner decides.'}
            </span>
          )}
          {(changes || showsManifest || waiting) && (
            <DetailsToggle open={details} onToggle={() => setDetails((v) => !v)} />
          )}
          {showsManifest && (
            <ExactManifestToggle open={showManifest} onToggle={() => setShowManifest((v) => !v)} />
          )}
          {error && <span className="text-red-500">{error.message}</span>}
        </>
      )}
    />
  )
}
