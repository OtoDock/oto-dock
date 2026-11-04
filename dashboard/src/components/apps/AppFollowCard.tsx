import { useState } from 'react'
import { useTakeRelease, type PinnedApp } from '../../api/apps'
import { AppCardShell } from './AppApprovalCard'
import { FOLLOW_CONFIRM, FOLLOW_MODE, dismiss, mergePrompt, noticeWaits, type FollowConfirmKind, type FollowNoticeState } from './AppFollowCard.helpers'
import { CopyButton } from '../CopyButton'
import Button from '../ui/Button'
import { CARD } from '../ui/classes'

/**
 * An app that follows another agent's app, when its source released past
 * the release this copy took (services/apps/app_follow.py):
 * one card in the approval card's slot, for whoever may manage the row and
 * may take the source's releases. Three states from the row's `follows`:
 * `update` (this copy never released on its own since: Update puts the
 * source's files here, held for approval, the data stays), `merge` (it did:
 * the agent is asked to merge the two) and `asked` (the merge is staged;
 * the agent's release settles it). Not now hides the card until the source
 * releases again (the release number is kept per app in localStorage). The
 * result of a press is the host's notice (`FollowNotice`): the merge's
 * sentence carries the prompt to give the agent, with a Copy button, since
 * nothing may be sent into the chat for the person.
 */

interface Props {
  app: PinnedApp
  agent: string
  onResult: (notice: FollowNoticeState) => void
}

export default function AppFollowCard({ app, agent, onResult }: Props) {
  const take = useTakeRelease(agent)
  const [hidden, setHidden] = useState(false)
  const f = app.follows
  if (!f?.update || hidden) return null
  const { release, mode } = f.update
  const source = f.agent_name || f.agent
  const title = app.title || app.slug
  const busy = take.isPending || !!app.deploying

  const run = (wanted: 'update' | 'merge') => {
    take.mutate({ appId: app.id, mode: wanted }, {
      onSuccess: (r) => onResult(wanted === 'merge'
        ? { text: r.text, error: false, copy: mergePrompt(app, r) }
        : { text: r.text, error: false, release: r.release }),
      onError: (e) => onResult({ text: (e as Error).message, error: true }),
    })
  }
  const notNow = () => {
    dismiss(app.id, release)
    setHidden(true)
  }

  return (
    <AppCardShell
      testId="app-follow-card"
      header={(
        <p className="font-medium text-primary">
          {mode === FOLLOW_MODE.ASKED
            ? `Release ${release} of ${source}’s app is unpacked beside “${title}”.`
            : `Release ${release} of ${source}’s app is available for “${title}”.`}
        </p>
      )}
      body={(
        <p className="mt-0.5 text-secondary">
          {mode === FOLLOW_MODE.UPDATE && (
            <>Update replaces this app’s files with release {release}. Its data stays. The new release waits for approval here.</>
          )}
          {mode === FOLLOW_MODE.MERGE && (
            <>This app released on its own since it was copied. Ask the agent to merge the two and deploy one release.</>
          )}
          {mode === FOLLOW_MODE.ASKED && (
            <>Give the agent the request to merge it and deploy one release.</>
          )}
        </p>
      )}
      footer={(
        <>
          {mode === FOLLOW_MODE.UPDATE && (
            <Button variant="primary" size="xs" onClick={() => run('update')} disabled={busy} data-testid="follow-update">
              {take.isPending ? 'Updating…' : `Update to release ${release}`}
            </Button>
          )}
          {mode !== FOLLOW_MODE.ASKED && (
            <Button size="xs" onClick={() => run('merge')} disabled={busy} data-testid="follow-merge">
              Ask the agent to merge
            </Button>
          )}
          {mode === FOLLOW_MODE.ASKED && f.update.path && (
            <CopyButton text={mergePrompt(app, { release, path: f.update.path })} />
          )}
          <Button size="xs" onClick={notNow} disabled={take.isPending} data-testid="follow-not-now">
            Not now
          </Button>
        </>
      )}
    />
  )
}

/** What a press did, or why it was refused, until dismissed; the merge's
 * notice carries the prompt for the agent with a Copy button. A notice that
 * says a release waits here goes once the app showed that release waiting
 * and no longer does (approved or rejected); before the list catches up it
 * stays. The host keys it by the notice, so a new one starts unseen. */
export function FollowNotice({ notice, app, onClose }: { notice: FollowNoticeState; app: PinnedApp; onClose: () => void }) {
  const waits = noticeWaits(notice, app)
  const [seen, setSeen] = useState(false)
  if (waits && !seen) setSeen(true)
  if (seen && !waits) return null
  return (
    <div
      className={`mx-3 mt-2 flex items-start gap-2 rounded-card border px-3 py-2.5 text-xs ${notice.error ? 'border-status-danger-border bg-status-danger-bg' : 'border-status-success-border bg-status-success-bg'}`}
      data-testid="app-follow-notice"
      role="status"
    >
      <span className="min-w-0 flex-1 break-words text-primary">{notice.text}</span>
      {notice.copy && <CopyButton text={notice.copy} />}
      <button
        onClick={onClose}
        aria-label="Dismiss"
        className="shrink-0 rounded-full px-1.5 text-secondary transition-colors hover:bg-surface-hover hover:text-primary"
      >
        ×
      </button>
    </div>
  )
}

/** The two-step confirm of the menu's follow rows: Send this release to the
 * source (it replaces the source's files, held for approval there) and Stop
 * following (this app keeps its files and its data). */
export function FollowConfirm({ app, kind, busy, onConfirm, onCancel }: {
  app: PinnedApp
  kind: FollowConfirmKind
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const source = app.follows?.agent_name || app.follows?.agent || 'the source'
  const title = app.title || app.slug
  return (
    <div
      className={`mx-3 mt-2 flex flex-wrap items-center gap-2 px-3 py-2.5 text-xs ${CARD}`}
      data-testid={kind === FOLLOW_CONFIRM.SEND ? 'app-send-to-source-confirm' : 'app-stop-following-confirm'}
    >
      <span className="min-w-0 break-words text-primary">
        {kind === FOLLOW_CONFIRM.SEND
          ? `Send release ${app.release ?? 0} of “${title}” to ${source}’s app? It replaces that app’s files and waits for approval there. Its data stays.`
          : `Stop following ${source}’s app? This app keeps its files and its data, and no newer release is offered here.`}
      </span>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <Button variant="primary" size="xs" onClick={onConfirm} disabled={busy}>
          {kind === FOLLOW_CONFIRM.SEND ? 'Send this release' : 'Stop following'}
        </Button>
        <Button size="xs" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
