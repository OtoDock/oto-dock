// How a delegate's landed files are named in the delegating chat.

import { scopeWorkspace, userOf } from '../layout/tree'

/** A landed path as the delegating chat's workspace names it: the scope
 * workspace prefix (`workspace/`, `users/<u>/workspace/`) stripped, so the
 * label matches the callback note (`inbox/<worker>/report.md`). */
export function resultFileLabel(path: string): string {
  const prefix = `${scopeWorkspace(userOf(path))}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}
