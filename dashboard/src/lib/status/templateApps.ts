/**
 * What the template panels say about the apps and checks a template brings:
 * the line that counts and names them, who gets each app with its own
 * program said in the same sentence (always in view where the approval is:
 * the yes covers it), and the approval's own sentence. The store reads the
 * catalog's entries, the install dialogs the preview's rows: both fit here.
 */

import type { PinnedApp } from '../../api/apps'
import type { PreviewApp, PreviewCheck } from '../../api/communityAgents'
import { SCOPE } from '../visibility'

/** An app as the panels read it: the install preview's row, or the catalog's
 *  own entry in the store (no manifest row, a line of description). */
export type TemplateApp = Pick<PreviewApp, 'slug' | 'title' | 'visibility'> & Partial<Omit<PreviewApp, 'row'>> & {
  row?: PinnedApp
  description?: string
  /** The store: the catalog's picture of the app, shown under Details beside its line. */
  picture?: string
  change?: 'new' | 'changed' | 'same'
}

/** A check as the panels read it. */
export type TemplateCheck = Pick<PreviewCheck, 'name' | 'description'> & Partial<PreviewCheck>

/** "A", "A and B", "A, B and C". */
export function namesInProse(names: string[]): string {
  if (names.length <= 1) return names[0] ?? ''
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export function appsCountLine(apps: TemplateApp[]): string {
  return `${apps.length === 1 ? '1 app' : `${apps.length} apps`}: ${namesInProse(apps.map(a => a.title))}`
}

/** A check's description is a sentence or two: the line counts them, Details names them. */
export function checksCountLine(checks: TemplateCheck[]): string {
  return checks.length === 1 ? '1 check' : `${checks.length} checks`
}

/** What Details says of an app whose manifest asks for nothing. */
export function appAsksNothing(app: TemplateApp): string {
  return app.row?.has_server || app.adds_server
    ? 'It asks for nothing beyond its own page and its program.'
    : 'It asks for nothing beyond its own page.'
}

/** Who gets the app, and its own program when it brings one, as one sentence. */
export function appWhoSentence(app: TemplateApp): string {
  const program = app.adds_server ? ', with its own program on your OtoDock' : ''
  const who = app.visibility === SCOPE.USER
    ? `Every member of this agent gets their own ${app.title}${program}.`
    : `${app.title} is shared by everyone who uses this agent${program}.`
  return app.owner_approval ? `${who} Each member approves their own copy.` : who
}

export function appChangeSentence(app: TemplateApp): string {
  if (app.change === 'new') return 'New in this version.'
  if (app.change === 'changed') return 'Changed in this version.'
  return ''
}

/** The checkbox's sentence. An admin approves for every member; anyone else
 *  for themselves and the agent (a department's agents), and each member
 *  approves their own copy. */
export function approvalSentence(scope: 'everyone' | 'own', agents = 1): string {
  return scope === 'everyone'
    ? 'Approve for every member.'
    : `Approve for you and the ${agents === 1 ? 'agent' : 'agents'}. Each member approves their own copy.`
}

/** A department's one line for the apps and checks of all its agents. */
export function appsAndChecksCountLine(apps: TemplateApp[], checks: TemplateCheck[]): string {
  const a = apps.length === 1 ? '1 app' : `${apps.length} apps`
  if (!checks.length) return a
  if (!apps.length) return checksCountLine(checks)
  return `${a} and ${checksCountLine(checks)}`
}

/** Said in view when apps are counted together: how many bring their own program. */
export function appsServerCountSentence(apps: TemplateApp[]): string {
  const n = apps.filter(a => a.adds_server).length
  if (!n) return ''
  if (apps.length === 1) return 'It comes with its own program on your OtoDock.'
  if (n === apps.length) return 'Each comes with its own program on your OtoDock.'
  return n === 1 ? 'One of them comes with its own program on your OtoDock.'
    : `${n} of them come with their own program on your OtoDock.`
}
