/**
 * The admin OAuth-app credential kinds — mirrored from the proxy's leaf
 * `proxy/storage/identity/oauth_app_kinds.py` (`tests/core/test_kinds.py`
 * binds this file to it; edit both).
 *
 * A manifest's `app_kinds` block names the kinds of vendor application an
 * admin may register for a provider (an OAuth App, a GitHub App); the
 * integrations list says which kind a connect goes through
 * (`oauth_meta.app_kind`), and a GitHub-App account lists the installations
 * the person can reach. A GitHub App installation can be an agent's service
 * identity, and a subscription's target (the app's webhook delivers).
 */

export const OAUTH_APP_KIND = {
  OAUTH_APP: 'oauth_app',
  GITHUB_APP: 'github_app',
} as const
export type OAuthAppKind = (typeof OAUTH_APP_KIND)[keyof typeof OAUTH_APP_KIND]

/** Which token the MCP server gets for an installation binding. */
export const MCP_TOKEN = {
  USER: 'user',
  INSTALLATION: 'installation',
} as const

/** A subscription whose target is a GitHub App installation. */
export const INSTALLATION_TARGET_KIND = 'installation'

/** The effective registration mode of such a subscription. */
export const REGISTRATION_MODE_APP = 'app'

/** The admin field input type of a pasted private key. */
export const INPUT_MULTILINE_SECRET = 'multiline_secret'

export function isGitHubApp(kind?: string | null): boolean {
  return kind === OAUTH_APP_KIND.GITHUB_APP
}
