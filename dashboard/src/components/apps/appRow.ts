import type { PinnedApp } from '../../api/apps'
import { appKind } from '../../lib/kinds/app'
import { GRANTEE_KIND } from '../../api/shares'

/**
 * What a row of the Apps strip looks like (APPS.md; SHARING.md "A placed
 * app in the Apps panel"). The fill follows ownership: brand-blue for the
 * viewer's own (a personal row, and a person's own share, placed here or
 * granted on a colleague's personal app), accent-purple for the team's (a
 * shared row, an agent or department placement). A row a share brought
 * here also wears a teal border and the share mark; on the active chip the
 * border is backed by a ring against the page, since teal barely shows on
 * the solid fill.
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
        ? 'bg-p-accent-purple text-white border-p-accent-teal ring-2 ring-p-accent-teal ring-offset-1 ring-offset-p-bg'
        : 'bg-p-accent-purple/10 text-p-accent-purple border-p-accent-teal hover:bg-p-accent-purple/20'
    }
    return active
      ? 'bg-brand text-white border-p-accent-teal ring-2 ring-p-accent-teal ring-offset-1 ring-offset-p-bg'
      : 'bg-brand/10 text-brand border-p-accent-teal hover:bg-brand/20'
  }
  if (team) {
    return active
      ? 'bg-p-accent-purple text-white border-p-accent-purple'
      : 'bg-p-accent-purple/10 text-p-accent-purple border-p-accent-purple/30 hover:bg-p-accent-purple/20'
  }
  return active
    ? 'bg-brand text-white border-brand'
    : 'bg-brand/10 text-brand border-brand/30 hover:bg-brand/20'
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
    detail: 'The workspace file and the approved actions are kept — ask the agent to pin it back anytime.',
  }
}
