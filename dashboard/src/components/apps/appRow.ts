import type { PinnedApp } from '../../api/apps'
import { appKind } from '../../lib/kinds/app'
import { GRANTEE_KIND } from '../../api/shares'
import type { SummaryLine } from './AppApprovalCard'

/**
 * What a row of the Apps strip looks like (APPS.md; SHARING.md "A placed
 * app in the Apps panel"). The fill follows ownership: the action blue for
 * the viewer's own (a personal row, and a person's own share, placed here or
 * granted on a colleague's personal app), the agent accent for the team's (a
 * shared row, an agent or department placement). A row a share brought
 * here also wears the information border and the share mark; on the active
 * chip the border is backed by a ring against the page, since the border
 * barely shows on the solid fill.
 */

type ChipRow = Pick<PinnedApp, 'scope' | 'granted' | 'placement'>

/** The row's scope word for an agent's shared (team) app; typed to the row's
 * own union, so a renamed word fails the build here. */
const TEAM_SCOPE: PinnedApp['scope'] = 'shared'

/** A row a share brought into this panel: another agent's app placed here,
 * or a colleague's personal app shared with the viewer. */
export function cameByShare(app: ChipRow): boolean {
  return !!app.granted || !!app.placement
}

export function appChipClass(app: ChipRow, active: boolean): string {
  const team = app.placement ? app.placement.kind !== GRANTEE_KIND.PERSON : app.scope === TEAM_SCOPE
  if (cameByShare(app)) {
    if (team) {
      return active
        ? 'bg-accent-agent-fill text-on-solid border-status-info-solid ring-2 ring-status-info-solid ring-offset-1 ring-offset-surface-page'
        : 'bg-accent-agent-bg text-accent-agent-fg border-status-info-solid hover:text-primary'
    }
    return active
      ? 'bg-action text-action-fg border-status-info-solid ring-2 ring-status-info-solid ring-offset-1 ring-offset-surface-page'
      : 'bg-action-subtle text-link border-status-info-solid hover:bg-action-subtle-hover hover:text-primary'
  }
  if (team) {
    return active
      ? 'bg-accent-agent-fill text-on-solid border-accent-agent-fill'
      : 'bg-accent-agent-bg text-accent-agent-fg border-accent-agent-border hover:text-primary'
  }
  return active
    ? 'bg-action text-action-fg border-action'
    : 'bg-action-subtle text-link border-transparent hover:bg-action-subtle-hover hover:text-primary'
}

type UnpinRow = Pick<PinnedApp, 'scope' | 'kind' | 'has_server' | 'title' | 'slug'>

/** A folder app whose live release has a server entry (`has_server`): its
 * soft unpin stops that server (APPS.md "Apps with a server", "Lifecycle"),
 * so the menu says so. A single-file app, or a folder app whose live release
 * has none, unpins. */
export function stopsServer(app: Pick<PinnedApp, 'kind' | 'has_server'>): boolean {
  return appKind(app).mayServe && !!app.has_server
}

/** The words of the soft unpin, the same in the menu and in its confirm:
 * the action (the menu item and the confirm's button), the question naming
 * the app, and what is kept. */
export function unpinWords(app: UnpinRow): { action: string; question: string; detail: string } {
  const title = app.title || app.slug
  const everyone = app.scope === TEAM_SCOPE
  if (stopsServer(app)) {
    return {
      action: everyone ? 'Stop app for everyone' : 'Stop app',
      question: everyone ? `Stop “${title}” for everyone?` : `Stop “${title}”?`,
      detail: 'Stops the app and keeps its data. Pin it again to start it.',
    }
  }
  return {
    action: everyone ? 'Unpin for everyone' : 'Unpin',
    question: everyone ? `Unpin “${title}” for everyone?` : `Unpin “${title}”?`,
    detail: 'The workspace file and the approved actions are kept. Ask the agent to pin it back anytime.',
  }
}

/** The deploy card's line for a server an app brings. The words say what it
 * does, never "server" or "code". The template dialogs say it in their own
 * sentence (lib/status/templateApps.ts). */
export function addsServerLine(testId: string): SummaryLine {
  return { key: 'adds-server', testId, text: 'Runs its own program on your OtoDock' }
}

/** Whether the page may hand the media it shows to a server of the app, and
 *  the sentence that says it: it declares `files.media`, and the release the
 *  card asks about runs a server (the one that waits, else the live one). A
 *  media link's address needs no sign-in, so whoever the server passes it to
 *  gets the file. That is a power of its own, so Details always says it, on
 *  the deploy card and in the template dialogs. Empty when it does not apply. */
export function mediaServerSentence(
  app: Pick<PinnedApp, 'pending_release' | 'pending_has_server' | 'has_server' | 'actions' | 'egress'>,
): string {
  const server = app.pending_release ? !!app.pending_has_server : !!app.has_server
  // Only a platform action carries a method (the manifest's own entry).
  if (!server || !app.actions.some((a) => a.method === 'files.media')) return ''
  return 'While the app is open, its program can get the videos, audio and pictures it shows you.'
    + ((app.egress?.length ?? 0) > 0 ? ' It can send them to the sites it may reach.' : '')
}
