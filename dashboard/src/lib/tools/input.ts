// A tool call's input as the engines send it: an open JSON object whose keys
// depend on the tool (roles.ts reads them by payload kind). Readers narrow
// each key where they read it.

import { isRecord } from '../json/guards'

export type ToolInput = Record<string, unknown>

/** A key's text: the value when it is a string, else '' (payloadText's rule). */
export function inputText(input: unknown, key: string): string {
  if (!isRecord(input)) return ''
  const v = input[key]
  return typeof v === 'string' ? v : ''
}
