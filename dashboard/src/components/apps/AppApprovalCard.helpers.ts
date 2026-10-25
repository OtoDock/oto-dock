// Whether an app needs the approval card: one answer for every surface that
// hosts the card (the apps overlay, the Dock, the app page).

import type { PinnedApp } from '../../api/apps'
import { DEPLOY_STATE } from '../../lib/status/appDeploy'

export function appNeedsApproval(app: PinnedApp | null | undefined): boolean {
  if (!app) return false
  // A pending release carries its manifest to the deploy card instead
  // (APPS.md "Deploy pipeline"); approving the release approves it.
  if (app.deploy_state === DEPLOY_STATE.PENDING) return false
  // The server says whether there is anything to approve; an older proxy
  // does not send it and the actions alone decide, as before.
  const empty = app.manifest_empty ?? app.actions.length === 0
  return !empty && (!app.actions_approved || app.approval_stale)
}
