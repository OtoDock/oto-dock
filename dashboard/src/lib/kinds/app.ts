/**
 * The app kind — mirrored from the proxy's authority `proxy/storage/db_apps.py`
 * (`tests/core/test_kinds.py` binds this file to it; edit both).
 *
 * `pinned_apps.kind` is `file` (one html document, the column's default) or
 * `folder` (a tree with releases, a database and maybe a server — APPS.md).
 * Every site that used to ask `kind === 'folder'` asks the capability it means.
 */

export const APP_KIND = {
  FILE: 'file',
  FOLDER: 'folder',
} as const
export type AppKindName = (typeof APP_KIND)[keyof typeof APP_KIND]

export interface AppKindFacts {
  /** Releases addressed by tree hash, a preview copy, a document by hash. */
  servesTree: boolean
  /** A database that rolls back and is deleted with the app. */
  keepsData: boolean
  /** May run a server: inbound routes, logs, handlers, triggers, the viewer token. */
  mayServe: boolean
  /** Secrets set by a person (the Settings panel). */
  hasSettings: boolean
  /** The working copy can be viewed as a preview build. */
  hasPreviewBuild: boolean
  /** "Delete app and its data" — the app, its releases and its database. */
  deletable: boolean
}

export const APP_KINDS: Record<AppKindName, AppKindFacts> = {
  [APP_KIND.FILE]: { servesTree: false, keepsData: false, mayServe: false, hasSettings: false, hasPreviewBuild: false, deletable: false },
  [APP_KIND.FOLDER]: { servesTree: true, keepsData: true, mayServe: true, hasSettings: true, hasPreviewBuild: true, deletable: true },
}

/** The kind's facts for an app row; an absent or unknown kind is the column's default, `file`. */
export function appKind(app: { kind?: string | null }): AppKindFacts {
  return APP_KINDS[app.kind === APP_KIND.FOLDER ? APP_KIND.FOLDER : APP_KIND.FILE]
}
