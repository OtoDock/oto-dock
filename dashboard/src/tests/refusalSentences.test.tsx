// A refused call shows the server's sentence, never its raw body: one hook
// per file that reads its refusals through lib/http/apiJson, driven through a
// refusal that names its reason, through a reverse proxy's error page, which
// carries none (the hook's own sentence shows then), and through a call that
// got no answer at all (the hook's own sentence too).
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { ReactNode } from 'react'
import ts from 'typescript'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ refreshUser: vi.fn(async () => {}) }) }))

import { useRenameAgentPath, useSaveAgentFile } from '@/api/agents'
import { useInstallCommunityMcp, useApproveMcpRequest } from '@/api/community'
import { useClearMyMemory } from '@/api/memory'
import { useApp, useApps } from '@/api/apps'
import { useDeleteNotification } from '@/api/notifications'
import { useSetAgentServiceBinding } from '@/api/serviceAccounts'
import { fetchSigningSecret } from '@/api/subscriptions'
import { useDeleteTrigger } from '@/api/triggers'
import { useRevokeAgentApiKey } from '@/api/agentApiKeys'
import { useRevokeUserApiKey } from '@/api/userApiKeys'
import { useInstallCommunityAgent, useTakeNewVersion } from '@/api/communityAgents'
import { ApiError } from '@/lib/http/apiJson'

const answer = (status: number, body: string) => new Response(body, { status, statusText: 'Bad Gateway' })

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

/** Runs a mutation hook's call and answers the error it was refused with. */
async function refusedWith(call: () => Promise<unknown>): Promise<ApiError> {
  return call().then(() => { throw new Error('the call was not refused') }, (e: unknown) => e as ApiError)
}

type Case = [name: string, fallback: string, run: () => Promise<unknown>]

const mutation = <A,>(useHook: () => { mutateAsync: (a: A) => Promise<unknown> }, arg: A) => async () => {
  const { result } = renderHook(useHook, { wrapper: wrapper() })
  return result.current.mutateAsync(arg)
}

const CASES: Case[] = [
  ['save a file', 'Could not save the file', mutation(useSaveAgentFile, { agent: 'a', path: 'x.md', content: '' })],
  ['rename', 'Could not rename it', mutation(useRenameAgentPath, { agent: 'a', oldPath: 'x', newPath: 'y' })],
  ['install an MCP', 'Could not install the MCP', mutation(useInstallCommunityMcp, 'github-mcp')],
  ['approve a request', 'Could not approve the request', mutation(useApproveMcpRequest, { id: 3 })],
  ['clear my memory', 'Could not clear your memory', mutation(useClearMyMemory, undefined)],
  ['delete a notification', 'Could not delete the notification', mutation(useDeleteNotification, 'n1')],
  ['bind an account', 'Could not set the account',
    mutation(useSetAgentServiceBinding, { agentName: 'a', mcpName: 'github-mcp', accountLabel: 'alice' })],
  ['read a signing secret', 'Could not read the signing secret', () => fetchSigningSecret('s1')],
  ['delete a trigger', 'Could not delete the trigger', mutation(useDeleteTrigger, 't1')],
  ['revoke an agent key', 'Could not revoke the key', mutation(useRevokeAgentApiKey, { agent: 'a', keyId: 'k1' })],
  ['revoke a personal key', 'Could not revoke the key', mutation(useRevokeUserApiKey, 'k1')],
  ['install an agent', 'Could not install the agent',
    mutation(useInstallCommunityAgent, { template_slug: 'personal-assistant' })],
  ['take a new version', 'Could not take the new version',
    mutation(useTakeNewVersion, { agent_slug: 'a', new_path: 'config/community/2.0.0/agent.md' })],
]

beforeEach(() => h.apiFetch.mockReset())

describe('a refusal reads as a sentence', () => {
  it.each(CASES)("%s shows the server's sentence, then its own", async (_name, fallback, run) => {
    h.apiFetch.mockResolvedValueOnce(answer(404, '{"detail":"Trigger not found"}'))
    const named = await refusedWith(run)
    expect(named).toBeInstanceOf(ApiError)
    expect(named.message).toBe('Trigger not found')
    expect(named.status).toBe(404)

    h.apiFetch.mockResolvedValueOnce(answer(502, '<html><body>Bad gateway</body></html>'))
    expect((await refusedWith(run)).message).toBe(fallback)

    // No answer at all: the browser's words never show.
    h.apiFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const offline = await refusedWith(run)
    expect(offline.message).toBe(fallback)
    expect(offline.status).toBe(0)
  })

  it('a list read the same way', async () => {
    h.apiFetch.mockResolvedValue(answer(403, '{"detail":"Access denied for agent \'a\'"}'))
    const { result } = renderHook(() => useApps('a'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error?.message).toBe("Access denied for agent 'a'")
  })

  it("an app that is gone is no app, not an error", async () => {
    h.apiFetch.mockResolvedValue(answer(404, '{"detail":"App not found"}'))
    const { result } = renderHook(() => useApp('gone'), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toBeNull()
  })

  it("the install's 409 keeps the suggested slug for the retry", async () => {
    h.apiFetch.mockResolvedValueOnce(answer(409, JSON.stringify({
      detail: { error: 'slug_taken', suggested_slug: 'personal-assistant-2', message: 'The slug is taken' },
    })))
    const e = await refusedWith(mutation(useInstallCommunityAgent, { template_slug: 'personal-assistant' }))
    expect(e.message).toBe('The slug is taken')
    expect(e.status).toBe(409)
    expect((e.body as { detail: { suggested_slug: string } }).detail.suggested_slug).toBe('personal-assistant-2')
  })
})

// Every fallback above reads as the house writes: short sentences, no dash,
// no semicolon.
describe('the fallback sentences', () => {
  it.each(CASES)('%s: "%s" is a plain sentence', (_name, fallback) => {
    expect(fallback).not.toMatch(/[—–;]|\s-\s/)
    for (const sentence of fallback.split(/(?<=[.!?])\s+/)) {
      expect(sentence.split(/\s+/).length).toBeLessThanOrEqual(15)
    }
  })
})

// A refused action a person started shows its failure somewhere: a mutation
// call with no error path fails in silence (the call site decides, the hooks
// carry no onError). Read by the TypeScript compiler, parse only.
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** Calls whose failure is shown where the code cannot see it, by file and
 * mutation, each with its reason. */
const SHOWN_ELSEWHERE: Record<string, string> = {
  'components/sharing/SharePopover.tsx create': "withConfirm's catch shows the refusal",
  'components/sharing/SharePopover.tsx patch': "withConfirm's catch shows the refusal",
  'components/workspace/WorkspaceOverlay.tsx renamePath': 'InlineRename shows the refusal under the name',
  'pages/admin/UsagePage.tsx setLimit': 'both periods are saved in one Promise.all whose refusal shows one toast',
  'pages/admin/UsagePage.tsx deleteLimit': 'both periods are saved in one Promise.all whose refusal shows one toast',
  'pages/agent/AgentSchedules.modals.tsx editMode': 'TaskSettingsModal awaits onSaveExtra in its submit and shows the refusal inline',
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) { if (e.name !== 'tests') sourceFiles(p, out) }
    else if (/\.tsx?$/.test(e.name) && !/\.(test|d)\.tsx?$/.test(e.name)) out.push(p)
  }
  return out
}

const hasOnError = (o: ts.Node | undefined): boolean => !!o && ts.isObjectLiteralExpression(o)
  && o.properties.some((p) => p.name?.getText() === 'onError')

/** An arrow the page calls on an event, with nobody to take its promise:
 * an `on*={() => …}` attribute, or the function a useCallback builds. */
function handlerArrow(arrow: ts.ArrowFunction): boolean {
  const p = arrow.parent
  if (ts.isJsxExpression(p) && ts.isJsxAttribute(p.parent)) return true
  return ts.isCallExpression(p) && p.arguments[0] === arrow && /^(React\.)?use(Callback|Memo)$/.test(p.expression.getText())
}

/** A `.catch` or `.then` handler that shows something: not `() => {}`. */
function shows(h: ts.Expression | undefined): boolean {
  if (!h) return false
  if (ts.isArrowFunction(h) || ts.isFunctionExpression(h)) {
    if (ts.isBlock(h.body)) return h.body.statements.length > 0
    return !(h.body.kind === ts.SyntaxKind.NullKeyword || ts.isIdentifier(h.body) && h.body.text === 'undefined')
  }
  return true
}

/** The calls of `src/` (or of `text`) that fail in silence, as `file:line receiver.method`. */
function silentMutations(rel: string, text: string): string[] {
  const sf = ts.createSourceFile(rel, text, ts.ScriptTarget.Latest, true, rel.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const declared = new Map<string, ts.Expression | undefined>()
  // `const { mutate: save } = update`: a call of `save` is a call on `update`.
  const aliases = new Map<string, { receiver: string, method: string }>()
  const collect = (n: ts.Node) => {
    if (ts.isVariableDeclaration(n)) {
      if (ts.isIdentifier(n.name)) declared.set(n.name.text, n.initializer)
      else if (ts.isObjectBindingPattern(n.name) && n.initializer && ts.isIdentifier(n.initializer)) {
        for (const el of n.name.elements) {
          const method = (el.propertyName ?? el.name).getText()
          if ((method === 'mutate' || method === 'mutateAsync') && ts.isIdentifier(el.name)) {
            aliases.set(el.name.text, { receiver: n.initializer.text, method })
          }
        }
      }
    }
    ts.forEachChild(n, collect)
  }
  collect(sf)
  const inlineOnError = (r: string) => {
    const init = declared.get(r)
    return !!init && ts.isCallExpression(init) && init.expression.getText() === 'useMutation' && hasOnError(init.arguments[0])
  }
  const caught = (call: ts.Node): boolean => {
    let node: ts.Node = call
    for (;;) {
      const p = node.parent
      if (ts.isParenthesizedExpression(p) || ts.isAwaitExpression(p) || ts.isVoidExpression(p)) { node = p; continue }
      if (ts.isPropertyAccessExpression(p) && p.expression === node && ts.isCallExpression(p.parent)) {
        const args = p.parent.arguments
        if (p.name.text === 'catch') return shows(args[0])
        if (p.name.text === 'then' && args.length >= 2) return shows(args[1])
        if (p.name.text === 'then' || p.name.text === 'finally') { node = p.parent; continue }
        return false
      }
      // Returned to a caller that owns the failure, but never an event
      // handler's arrow (an `on*` attribute, or one built with useCallback).
      if (ts.isReturnStatement(p)) return true
      if (ts.isArrowFunction(p) && p.body === node) return !handlerArrow(p)
      if (ts.isArrayLiteralExpression(p) && ts.isCallExpression(p.parent)) {
        const callee = p.parent.expression.getText()
        if (callee === 'Promise.allSettled') return true
        if (callee === 'Promise.all') { node = p.parent; continue }
      }
      if (ts.isCallExpression(p) && p.expression.getText() === 'Promise.allSettled') return true
      break
    }
    for (let a: ts.Node | undefined = call.parent; a && !ts.isSourceFile(a); a = a.parent) {
      if (ts.isTryStatement(a) && a.catchClause && a.tryBlock.pos <= call.pos && call.end <= a.tryBlock.end) return true
      if (ts.isFunctionLike(a) && !(a.parent && ts.isCallExpression(a.parent) && /\.(map|forEach)$/.test(a.parent.expression.getText()))) break
    }
    return false
  }
  const found: string[] = []
  const visit = (n: ts.Node) => {
    if (ts.isCallExpression(n)) {
      let receiver = ''
      let method = ''
      if (ts.isPropertyAccessExpression(n.expression)) {
        method = n.expression.name.text
        const e = n.expression.expression
        receiver = ts.isPropertyAccessExpression(e) ? e.name.text : e.getText()
      } else if (ts.isIdentifier(n.expression) && aliases.has(n.expression.text)) {
        ({ receiver, method } = aliases.get(n.expression.text)!)
      }
      if (method !== 'mutate' && method !== 'mutateAsync') { ts.forEachChild(n, visit); return }
      const shown = SHOWN_ELSEWHERE[`${rel} ${receiver}`] !== undefined || inlineOnError(receiver)
      const opts = n.arguments[1]
      const optsOnError = hasOnError(opts) || (!!opts && ts.isIdentifier(opts) && hasOnError(declared.get(opts.text)))
      const rendered = new RegExp(`\\b${receiver.replace(/[^\w$]/g, '\\$&')}\\.(error|isError)\\b`).test(text)
      const line = sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1
      if (method === 'mutate' && !(shown || optsOnError || rendered)) found.push(`${rel}:${line} ${receiver}.mutate`)
      if (method === 'mutateAsync' && !(shown || caught(n))) found.push(`${rel}:${line} ${receiver}.mutateAsync`)
    }
    ts.forEachChild(n, visit)
  }
  visit(sf)
  return found
}

describe('a refused action never fails in silence', () => {
  it('finds each silent shape, and passes each shown one', () => {
    const sample = [
      'const a = useThing()',
      'const b = useMutation({ mutationFn, onError: (e) => setErr(e) })',
      'const { mutate: saveIt } = a',
      'const opts = { onError: (e) => setErr(e) }',
      'a.mutate(1)',                                        // silent
      'a.mutateAsync(2)',                                   // silent
      'a.mutateAsync(3).catch(() => {})',                   // swallowed
      'saveIt(4)',                                          // silent, through the alias
      'a.mutate(5, { onError: (e) => setErr(e) })',
      'a.mutate(6, opts)',
      'b.mutate(7)',
      'a.mutateAsync(8).then(done).catch(toastError("The thing was not saved"))',
      'void a.mutateAsync(9).then(done, toastError("x"))',
      'async function f() { try { await a.mutateAsync(10) } catch (e) { setErr(e) } }',
      'const g = () => a.mutateAsync(11)',
      'Promise.all([a.mutateAsync(12), a.mutateAsync(13)]).then(close, toastError("x"))',
      'Promise.allSettled([a.mutateAsync(14)])',
      'const b1 = <button onClick={() => a.mutateAsync(15)} />',           // silent, a handler
      'const h = useCallback(() => a.mutateAsync(16), [a])',              // silent, a handler
      'run(() => a.mutateAsync(17))',                                     // the caller takes it
      "run('k', () => a.mutateAsync(18), { failTitle: 'x' })",            // the save state shows it
    ].join('\n')
    expect(silentMutations('sample.tsx', sample).map((f) => f.split(' ')[0])).toEqual([
      'sample.tsx:5', 'sample.tsx:6', 'sample.tsx:7', 'sample.tsx:8', 'sample.tsx:18', 'sample.tsx:19',
    ])
  })

  it('every mutation call in the dashboard shows its failure', () => {
    const found = sourceFiles(SRC).flatMap((f) => silentMutations(path.relative(SRC, f).split(path.sep).join('/'), readFileSync(f, 'utf8')))
    expect(found).toEqual([])
  })

  it('every call the list excuses still exists', () => {
    for (const key of Object.keys(SHOWN_ELSEWHERE)) {
      const [file, receiver] = key.split(' ')
      expect(readFileSync(path.join(SRC, file), 'utf8')).toContain(`${receiver}.mutateAsync(`)
    }
  })
})
