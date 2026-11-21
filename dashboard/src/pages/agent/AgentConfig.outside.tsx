import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAgentFiles } from '../../api/agents'
import {
  useChangeOutsidePeople, useOutsidePeople,
  type OutsideAppChoice, type OutsidePeople, type OutsidePeopleChange, type OutsideTool,
} from '../../api/outside'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { ACCESS_LABEL, OUTSIDE_ACCESS, type FilesAccess, type OutsideAccess } from '../../lib/outside'
import { foldersUnder, isInside } from '../../lib/layout/knowledgeFolders'
import FolderTree from '../../components/common/FolderTree'
import Button from '../../components/ui/Button'
import Dialog from '../../components/ui/Dialog'
import InfoTip from '../../components/ui/InfoTip'
import { Select } from '../../components/ui/Field'
import Switch from '../../components/ui/Switch'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'
import { CheckboxRow, SaveState, SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

// ---------------------------------------------------------------------------
// External identities: what a caller on this agent's phone line, or another
// person who is not a member, gets. Managers only (the route is too). Each
// control saves on change and sends only what it changed, so it never undoes
// what the agent's own config tool or another manager set meanwhile.
// ---------------------------------------------------------------------------

/** The card's anchor: a notice about the picks links here. */
export const EXTERNAL_IDENTITIES_ID = 'external-identities'
/** The wrap-up runs on the conversation's own model. */
const SAME_MODEL = ''

const TOOL_LINE: Record<OutsideAccess, string> = {
  [OUTSIDE_ACCESS.READ_ONLY]: 'Read only tools are used.',
  [OUTSIDE_ACCESS.READ_WRITE]: 'Read and write tools are used.',
  [OUTSIDE_ACCESS.CUSTOM]: 'The agent set which parts of this tool are used.',
}
const NOTHING_READS = 'This tool has nothing that only reads, so nothing is used.'

/** The page as it reads once `change` lands, so the card shows a change
 *  at once while it saves. */
function applied(page: OutsidePeople, change: OutsidePeopleChange): OutsidePeople {
  const named = new Map((change.tools ?? []).map((t) => [t.mcp, t]))
  const removed = new Set(change.remove_tools ?? [])
  const tools = page.tools.map((t): OutsideTool => {
    if (removed.has(t.mcp)) return { ...t, access: '', functions: [], changed: false }
    const pick = named.get(t.mcp)
    return pick ? { ...t, access: pick.access, functions: pick.functions, changed: t.changed && pick.access === t.access } : t
  })
  let knowledge = page.knowledge.filter((k) => !(change.remove_knowledge ?? []).includes(k))
  for (const add of change.add_knowledge ?? []) {
    if (knowledge.some((k) => k === add || isInside(add, k))) continue
    knowledge = [...knowledge.filter((k) => !isInside(k, add)), add]
  }
  const removedApps = new Set(change.remove_apps ?? [])
  const apps = [
    ...page.apps.filter((a) => !removedApps.has(a.id)),
    ...page.app_choices
      .filter((a) => (change.add_apps ?? []).includes(a.id) && !page.apps.some((p) => p.id === a.id))
      .map((a) => ({ ...a, changed: false, problem: '' })),
  ]
  return {
    ...page,
    tools,
    apps,
    knowledge: knowledge.sort(),
    files: change.files ?? page.files,
    web: change.web ?? page.web,
    wrapup_enabled: change.wrapup_enabled ?? page.wrapup_enabled,
    shared_notes: change.shared_notes ?? page.shared_notes,
    wrapup_model: change.wrapup_model ?? page.wrapup_model,
  }
}

export function ExternalIdentitiesSection({ name }: { name: string }) {
  const { user } = useAuth()
  if (!canManageAgent(user, name)) return null
  return <ExternalIdentitiesCard name={name} />
}

function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="shrink-0 p-1 rounded-sm text-tertiary hover:text-status-danger-fg hover:bg-status-danger-bg transition-colors cursor-pointer"
    >
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  )
}

function ExternalIdentitiesCard({ name }: { name: string }) {
  const { data } = useOutsidePeople(name)
  const change = useChangeOutsidePeople(name)
  const { stateOf, run } = useSaveState()
  // The changes sent and not yet answered, shown on top of the server's page;
  // the requests run one after another.
  const [pending, setPending] = useState<OutsidePeopleChange[]>([])
  const chain = useRef<Promise<unknown>>(Promise.resolve())
  const [addingTools, setAddingTools] = useState(false)
  const [addingFolders, setAddingFolders] = useState(false)
  const [addingApps, setAddingApps] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)
  const location = useLocation()

  const loaded = !!data
  useEffect(() => {
    if (loaded && location.hash === `#${EXTERNAL_IDENTITIES_ID}`) {
      cardRef.current?.scrollIntoView?.({ block: 'start' })
    }
  }, [loaded, location.hash, location.key])

  if (!data) return null
  const view = pending.reduce(applied, data)

  const submit = (body: OutsidePeopleChange, key: string) => {
    setPending((p) => [...p, body])
    chain.current = chain.current.then(() => run(key, () => change.mutateAsync(body), {
      failTitle: 'This change was not saved',
    })).then((saved) => {
      setPending((p) => (saved ? p.filter((c) => c !== body) : []))
    })
  }

  const added = view.tools.filter((t) => t.access)
  const addable = view.tools.filter((t) => !t.access)
  // A saved model the engine no longer lists stays shown, so it can be changed.
  const models = view.wrapup_models ?? []
  const modelChoices = view.wrapup_model && !models.some((m) => m.id === view.wrapup_model)
    ? [...models, { id: view.wrapup_model, label: view.wrapup_model }]
    : models

  return (
    <div ref={cardRef} id={EXTERNAL_IDENTITIES_ID} className={`${CARD} p-4 scroll-mt-4`} data-testid="external-identities">
      <div className="mb-4">
        <p className="text-xs font-semibold text-secondary uppercase">
          External identities
          <InfoTip label="External identities">
            An external identity is anyone who reaches this agent and is not a member. A caller on its phone line is one.
            Visitors of a link shared from an app's menu are external identities too.
          </InfoTip>
          <span className="normal-case tracking-normal"><SaveState state={stateOf('outside')} /></span>
        </p>
        <p className="text-xs text-tertiary mt-0.5">
          A caller on this agent's phone line gets nothing until you pick it here.
          {view.wrapup_enabled && ' After each conversation, the agent remembers what happened.'}
        </p>
      </div>

      <div className="divide-y divide-subtle [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm font-medium text-primary">Tools</p>
            <Button size="xs" onClick={() => setAddingTools(true)}>Add tools</Button>
          </div>
          {added.length === 0 && <p className="text-xs text-tertiary">No tools are used in conversations.</p>}
          {added.map((t) => {
            const access = t.access as OutsideAccess
            const nothing = access === OUTSIDE_ACCESS.READ_ONLY && t.known_functions !== null
              && !t.known_functions.some((f) => f.reads_only)
            return (
              <div key={t.mcp} className="flex items-center justify-between gap-3" data-testid="outside-tool">
                <div className="min-w-0">
                  <p className="text-sm text-primary">
                    <label htmlFor={`outside-${t.mcp}`}>{t.label}</label>
                    {t.changed && (
                      <span className={`ml-2 ${BADGE_BASE} ${BADGE_TONE.warning}`} title="The tool's maker changed what Read only gives.">
                        Changed
                      </span>
                    )}
                  </p>
                  <p className={`text-xs ${nothing ? 'text-status-warning-fg' : 'text-tertiary'}`}>
                    {nothing ? NOTHING_READS : TOOL_LINE[access]}
                  </p>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <Select
                    id={`outside-${t.mcp}`}
                    compact
                    value={access}
                    onChange={(e) => {
                      const next = e.target.value as OutsideAccess
                      if (next !== access) submit({ tools: [{ mcp: t.mcp, access: next, functions: [] }] }, 'outside')
                    }}
                  >
                    <option value={OUTSIDE_ACCESS.READ_ONLY}>{ACCESS_LABEL[OUTSIDE_ACCESS.READ_ONLY]}</option>
                    <option value={OUTSIDE_ACCESS.READ_WRITE}>{ACCESS_LABEL[OUTSIDE_ACCESS.READ_WRITE]}</option>
                    {access === OUTSIDE_ACCESS.CUSTOM && (
                      <option value={OUTSIDE_ACCESS.CUSTOM}>{ACCESS_LABEL[OUTSIDE_ACCESS.CUSTOM]}</option>
                    )}
                  </Select>
                  <RemoveButton label={`Remove ${t.label}`} onClick={() => submit({ remove_tools: [t.mcp] }, 'outside')} />
                </div>
              </div>
            )
          })}
        </div>

        <div className="flex flex-col gap-4">
          <SettingRow
            label="This person's files"
            htmlFor="outside-files"
            description="Read and write lets the agent save files during the conversation."
          >
            <Select
              id="outside-files"
              compact
              value={view.files}
              onChange={(e) => submit({ files: e.target.value as FilesAccess }, 'outside')}
            >
              <option value={OUTSIDE_ACCESS.READ_ONLY}>{ACCESS_LABEL[OUTSIDE_ACCESS.READ_ONLY]}</option>
              <option value={OUTSIDE_ACCESS.READ_WRITE}>{ACCESS_LABEL[OUTSIDE_ACCESS.READ_WRITE]}</option>
            </Select>
          </SettingRow>
          <SettingRow label="Web search" labelId="outside-web-label" description="The engine's own web search and page reading.">
            <Switch aria-labelledby="outside-web-label" checked={view.web} onChange={(web) => submit({ web }, 'outside')} />
          </SettingRow>
          <SettingRow
            label="Remember each conversation"
            labelId="outside-wrapup-label"
            description="The agent runs once more after each conversation."
            info="The agent writes down how it went, the next steps and what to remember about the person. It can use this agent's tools for that, such as its mailbox. Off, nothing runs after a conversation with an external identity. Calls on the agent's own line are still written down."
          >
            <Switch
              aria-labelledby="outside-wrapup-label"
              checked={view.wrapup_enabled}
              onChange={(wrapup_enabled) => submit({ wrapup_enabled }, 'outside')}
            />
          </SettingRow>
          {view.wrapup_enabled && (
            <div className="flex flex-col gap-4 pl-4 border-l border-subtle">
              <SettingRow
                label="Notes for the team"
                labelId="outside-notes-label"
                description="Members' chats read these notes."
                info="After each conversation the agent may leave one line for the team. The newest 40 are kept. Only members see them."
              >
                <Switch
                  aria-labelledby="outside-notes-label"
                  checked={view.shared_notes}
                  onChange={(shared_notes) => submit({ shared_notes }, 'outside')}
                />
              </SettingRow>
              <SettingRow
                label="Model after a conversation"
                htmlFor="outside-wrapup-model"
                info="A lighter model costs less. It runs once after each conversation."
              >
                <Select
                  id="outside-wrapup-model"
                  compact
                  value={view.wrapup_model}
                  onChange={(e) => submit({ wrapup_model: e.target.value }, 'outside')}
                >
                  <option value={SAME_MODEL}>Same as the conversation</option>
                  {modelChoices.map((m) => <option key={m.id} value={m.id}>{m.label || m.id}</option>)}
                </Select>
              </SettingRow>
            </div>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-primary">Knowledge folders</p>
              <p className="text-xs text-tertiary">Only the folders you add are read in conversations.</p>
            </div>
            <Button size="xs" className="shrink-0" onClick={() => setAddingFolders(true)}>Add folders</Button>
          </div>
          {view.knowledge.length === 0 && <p className="text-xs text-tertiary">No folders are read in conversations.</p>}
          {view.knowledge.map((folder) => (
            <div key={folder} className="flex items-center gap-2 rounded-card border border-subtle px-3 py-1.5" data-testid="outside-folder">
              <span className="flex-1 min-w-0 truncate text-sm font-mono text-primary">{folder}</span>
              <RemoveButton label={`Remove ${folder}`} onClick={() => submit({ remove_knowledge: [folder] }, 'outside')} />
            </div>
          ))}
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Apps
                <InfoTip label="Apps">
                  A conversation uses only what an app opens to external identities. An app that reads a folder works only
                  when that folder is under Knowledge folders. This holds for its shared links too. Removing that folder
                  stops those links.
                </InfoTip>
              </p>
              <p className="text-xs text-tertiary">Only the apps you add are used in conversations.</p>
            </div>
            <Button size="xs" className="shrink-0" onClick={() => setAddingApps(true)}>Add apps</Button>
          </div>
          {view.apps.length === 0 && <p className="text-xs text-tertiary">No apps are used in conversations.</p>}
          {view.apps.map((app) => (
            <div key={app.id} className="flex items-center gap-2 rounded-card border border-subtle px-3 py-1.5" data-testid="outside-app">
              <div className="flex-1 min-w-0">
                <p className="text-sm text-primary truncate">
                  {app.title}
                  {app.changed && (
                    <span className={`ml-2 ${BADGE_BASE} ${BADGE_TONE.warning}`} title="A new release changed what this app opens to external identities.">
                      Changed
                    </span>
                  )}
                </p>
                {app.problem && <p className="text-xs text-tertiary">{app.problem}</p>}
              </div>
              <RemoveButton label={`Remove ${app.title}`} onClick={() => submit({ remove_apps: [app.id] }, 'outside')} />
            </div>
          ))}
        </div>
      </div>

      {addingTools && (
        <AddToolsDialog
          tools={addable}
          onClose={() => setAddingTools(false)}
          onAdd={(mcps) => {
            setAddingTools(false)
            submit({ tools: mcps.map((mcp) => ({ mcp, access: OUTSIDE_ACCESS.READ_ONLY, functions: [] })) }, 'outside')
          }}
        />
      )}
      {addingApps && (
        <AddAppsDialog
          apps={view.app_choices.filter((a) => !view.apps.some((p) => p.id === a.id))}
          onClose={() => setAddingApps(false)}
          onAdd={(ids) => {
            setAddingApps(false)
            submit({ add_apps: ids }, 'outside')
          }}
        />
      )}
      {addingFolders && (
        <AddFoldersDialog
          name={name}
          roots={view.knowledge_choices}
          picked={view.knowledge}
          onClose={() => setAddingFolders(false)}
          onAdd={(folders) => {
            setAddingFolders(false)
            submit({ add_knowledge: folders }, 'outside')
          }}
        />
      )}
    </div>
  )
}

function AddToolsDialog({ tools, onClose, onAdd }: {
  tools: OutsideTool[]
  onClose: () => void
  onAdd: (mcps: string[]) => void
}) {
  const [ticked, setTicked] = useState<string[]>([])
  return (
    <Dialog title="Add tools" onClose={onClose}>
      <div className="p-4 flex flex-col gap-3">
        {tools.length === 0 ? (
          <p className="text-sm text-tertiary">Every tool of this agent is added.</p>
        ) : (
          <>
            <p className="text-xs text-tertiary">Each tool you add starts as Read only.</p>
            <div className="flex flex-col gap-2 max-h-72 overflow-y-auto">
              {tools.map((t) => (
                <CheckboxRow
                  key={t.mcp}
                  id={`add-tool-${t.mcp}`}
                  label={t.label}
                  checked={ticked.includes(t.mcp)}
                  onChange={(on) => setTicked(on ? [...ticked, t.mcp] : ticked.filter((m) => m !== t.mcp))}
                />
              ))}
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={ticked.length === 0} onClick={() => onAdd(ticked)}>Add</Button>
        </div>
      </div>
    </Dialog>
  )
}

function AddAppsDialog({ apps, onClose, onAdd }: {
  apps: OutsideAppChoice[]
  onClose: () => void
  onAdd: (ids: string[]) => void
}) {
  const [ticked, setTicked] = useState<string[]>([])
  return (
    <Dialog title="Add apps" onClose={onClose}>
      <div className="p-4 flex flex-col gap-3">
        {apps.length === 0 ? (
          <p className="text-sm text-tertiary">This agent has no other app to add.</p>
        ) : (
          <>
            <p className="text-xs text-tertiary">Each app gives only what it opens to external identities.</p>
            <div className="flex flex-col gap-2 max-h-72 overflow-y-auto">
              {apps.map((a) => (
                <CheckboxRow
                  key={a.id}
                  id={`add-app-${a.slug}`}
                  label={a.title}
                  description={a.methods.length === 0 ? 'Opens nothing to external identities yet.' : undefined}
                  checked={ticked.includes(a.id)}
                  onChange={(on) => setTicked(on ? [...ticked, a.id] : ticked.filter((id) => id !== a.id))}
                />
              ))}
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={ticked.length === 0} onClick={() => onAdd(ticked)}>Add</Button>
        </div>
      </div>
    </Dialog>
  )
}

function AddFoldersDialog({ name, roots, picked, onClose, onAdd }: {
  name: string
  roots: string[]
  picked: string[]
  onClose: () => void
  onAdd: (folders: string[]) => void
}) {
  const { data: tree, isLoading } = useAgentFiles(name)
  const [ticked, setTicked] = useState<string[]>([])
  const rows = foldersUnder(tree, roots)
  // A folder inside an added or a ticked folder is read with it.
  const parentOf = (path: string) => [...picked, ...ticked].find((p) => isInside(path, p))
  const refusal = (path: string) =>
    parentOf(path) ? 'Its parent folder is added.' : picked.includes(path) ? 'This folder is added.' : null
  return (
    <Dialog title="Add folders" onClose={onClose}>
      <div className="p-4 flex flex-col gap-3">
        {isLoading && !tree ? (
          <p className="text-sm text-tertiary">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-tertiary">This agent's knowledge has no folders.</p>
        ) : (
          <FolderTree
            multiple
            label="Knowledge folders"
            rows={rows}
            selected={(path) => ticked.includes(path) || picked.includes(path) || !!parentOf(path)}
            onPick={(path) => setTicked(ticked.includes(path)
              ? ticked.filter((p) => p !== path)
              : [...ticked.filter((p) => !isInside(p, path)), path])}
            refusal={refusal}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={ticked.length === 0} onClick={() => onAdd(ticked)}>Add</Button>
        </div>
      </div>
    </Dialog>
  )
}
