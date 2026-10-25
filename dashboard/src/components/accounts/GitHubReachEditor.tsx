/**
 * An agent's GitHub reach: the repositories the agent may reach and whether
 * it may only read. OtoDock refuses everything past it, in the GitHub tool
 * and in git and gh; an installation of an admin's own GitHub App is
 * narrowed by GitHub as well.
 *
 * `GitHubReachView` is the one view of it, fed its listing and its save:
 * under the service-account select (Agent Settings, MCPs) through
 * `GitHubReachEditor` here, and under each agent of a person's own account
 * card (User Settings, Integrations) through `AgentOverrideList`.
 *
 * A GitHub-App account offers its repositories as a checklist with a
 * search; a token or OAuth account has no list, so only Read only.
 */

import { useState } from 'react'
import { useSaveState } from '../../hooks/useSaveState'
import {
  useAgentServiceRepositories,
  useReachCopied,
  useSetAgentServiceBinding,
  type ReachRepositoriesResponse,
} from '../../api/serviceAccounts'
import Button from '../ui/Button'
import { Input } from '../ui/Field'
import { CARD_BORDERED } from '../ui/classes'
import InfoTip from '../ui/InfoTip'
import { SaveState } from '../ui/SettingsControls'

/** What the picker lists, as a query answers it. */
export interface ReachListing {
  data?: ReachRepositoriesResponse
  isLoading: boolean
  isError: boolean
  error: unknown
}

export interface ReachChange {
  repositories?: string[]
  readOnly?: boolean
}

interface ViewProps {
  repositories: string[]
  readOnly: boolean
  /** The account is a GitHub-App one: it has repositories to pick from. */
  pickable: boolean
  /** Shown, not changed (a co-manager's binding). */
  viewOnly: boolean
  listing: ReachListing
  /** The picker opened or closed: the listing is asked for while it is open. */
  onPickerChange: (open: boolean) => void
  save: (change: ReachChange) => Promise<{ warning?: string }>
  pending: boolean
  title?: string
  /** A line shown above the controls until the next saved change. */
  notice?: string
  /** Whose controls these are, where several sit on one page (an agent's
   * name): it goes into the switch's and the button's accessible names. */
  label?: string
}

const SHOWN_NAMES = 5

/** The names as the server's sentences list them: sorted, in lower case,
 * "a, b and c", past five "and N more". */
function summary(names: string[]): string {
  if (names.length === 0) return 'All repositories the account reaches.'
  const sorted = [...new Set(names.map((n) => n.trim().toLowerCase()))].sort()
  if (sorted.length > SHOWN_NAMES) {
    return `Only ${sorted.slice(0, SHOWN_NAMES).join(', ')} and ${sorted.length - SHOWN_NAMES} more.`
  }
  const last = sorted[sorted.length - 1]
  return `Only ${sorted.length === 1 ? last : `${sorted.slice(0, -1).join(', ')} and ${last}`}.`
}

export function GitHubReachView({
  repositories,
  readOnly,
  pickable,
  viewOnly,
  listing,
  onPickerChange,
  save,
  pending,
  title,
  notice: standing,
  label,
}: ViewProps) {
  const [picking, setPicking] = useState(false)
  const [chosen, setChosen] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [notice, setNotice] = useState<{ text: string; warning: boolean } | null>(null)
  const [changed, setChanged] = useState(false)
  const { stateOf, run } = useSaveState()

  const pick = (open: boolean) => {
    setPicking(open)
    onPickerChange(open)
  }

  // A refusal shows under the controls, where the answer's warning shows.
  const submit = (key: string, change: ReachChange) => {
    setNotice(null)
    return run(key, async () => {
      const answer = await save(change)
      setChanged(true)
      if (answer.warning) setNotice({ text: answer.warning, warning: true })
    }, {
      onFail: (e) => setNotice({ text: e instanceof Error ? e.message : 'The change was refused', warning: false }),
    })
  }

  const openPicker = () => {
    setChosen(repositories)
    setSearch('')
    pick(true)
  }

  const toggle = (name: string) =>
    setChosen((prev) =>
      prev.some((n) => n.toLowerCase() === name.toLowerCase())
        ? prev.filter((n) => n.toLowerCase() !== name.toLowerCase())
        : [...prev, name],
    )

  const savePick = async () => {
    if (await submit('repositories', { repositories: chosen })) pick(false)
  }

  const needle = search.trim().toLowerCase()
  const installations = listing.data?.installations ?? []

  return (
    <div className={`${CARD_BORDERED} mt-2 px-2 py-1.5 text-xs`} data-testid="github-reach">
      {title && <div className="text-3xs uppercase tracking-wide text-tertiary">{title}</div>}
      {standing && !changed && (
        <div className="mt-1 text-secondary" data-testid="github-reach-notice">
          {standing}
        </div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <span>Repositories: {summary(pickable ? repositories : [])}<SaveState state={stateOf('repositories')} /></span>
        {pickable && !viewOnly && !picking && (
          <button
            type="button"
            className="text-link hover:text-link-hover underline disabled:opacity-50"
            disabled={pending}
            onClick={openPicker}
            aria-label={label ? `Change repositories for ${label}` : undefined}
          >
            Change
          </button>
        )}
      </div>
      {!pickable && (
        <div className="mt-0.5 text-tertiary">This account has no list of repositories to pick from.</div>
      )}

      {picking && (
        <div className="mt-2 rounded-card bg-surface-raised shadow-elevation-2 p-2">
          <Input compact
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search repositories"
            aria-label="Search repositories"
          />
          <label className="mt-2 flex items-center gap-2">
            <input
              type="checkbox"
              checked={chosen.length === 0}
              onChange={() => setChosen([])}
              className="accent-action"
            />
            All repositories the account reaches
          </label>
          {listing.isLoading && <div className="mt-2 text-tertiary">Loading the repositories.</div>}
          {listing.isError && (
            <div className="mt-2 text-status-danger-fg">{(listing.error as Error).message}</div>
          )}
          {listing.data && !listing.data.pickable && (
            <div className="mt-2 text-tertiary">
              This account has no list of repositories to pick from. The list kept from the account
              bound before still applies. Tick All repositories and Save to clear it.
            </div>
          )}
          {listing.data?.from_meta && (
            <div className="mt-2 text-status-warning-fg">
              GitHub could not be reached. This is the list OtoDock saw last.
            </div>
          )}
          <div className="mt-2 max-h-48 overflow-y-auto">
            {installations.map((inst) => {
              const names = inst.repositories.filter((n) => !needle || n.toLowerCase().includes(needle))
              return (
                <div key={inst.id} className="mb-1">
                  <div className="text-tertiary">{inst.account}</div>
                  {names.map((name) => (
                    <label key={name} className="flex items-center gap-2 pl-2">
                      <input
                        type="checkbox"
                        checked={chosen.some((n) => n.toLowerCase() === name.toLowerCase())}
                        onChange={() => toggle(name)}
                        className="accent-action"
                      />
                      {name}
                    </label>
                  ))}
                  {inst.truncated && (
                    <div className="pl-2 text-tertiary">The list stopped here. More repositories exist.</div>
                  )}
                </div>
              )
            })}
          </div>
          <div className="mt-2 flex gap-2">
            <Button variant="primary" size="xs"
              disabled={pending}
              onClick={() => void savePick()}
            >
              Save
            </Button>
            <Button variant="ghost" size="xs" onClick={() => pick(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      <div className="mt-2 flex items-center">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={readOnly}
            disabled={viewOnly || pending}
            onChange={(e) => void submit('read_only', { readOnly: e.target.checked })}
            aria-label={label ? `Read only for ${label}` : 'Read only'}
            className="accent-action"
          />
          <span>Read only</span>
        </label>
        <InfoTip label={label ? `Read only for ${label}` : 'Read only'}>The agent can read on GitHub but cannot change anything.</InfoTip>
        <SaveState state={stateOf('read_only')} />
      </div>
      {notice && (
        <div className={`mt-1 ${notice.warning ? 'text-status-warning-fg' : 'text-status-danger-fg'}`}>
          {notice.text}
        </div>
      )}
    </div>
  )
}

interface Props {
  agentName: string
  mcpName: string
  accountLabel: string
  installationId: string
  repositories: string[]
  readOnly: boolean
  /** The account is a GitHub-App one: it has repositories to pick from. */
  pickable: boolean
  /** A co-manager's binding: shown, not changed. */
  viewOnly: boolean
}

/** The agent's reach under its service-account select. */
export function GitHubReachEditor({
  agentName,
  mcpName,
  accountLabel,
  installationId,
  repositories,
  readOnly,
  pickable,
  viewOnly,
}: Props) {
  const setBinding = useSetAgentServiceBinding()
  const [picking, setPicking] = useState(false)
  const listing = useAgentServiceRepositories(agentName, mcpName, accountLabel, installationId, picking)
  const copied = useReachCopied(agentName, mcpName)
  // The view shows a refusal or a warning under the controls.
  const save = (change: ReachChange) =>
    setBinding.mutateAsync({ agentName, mcpName, accountLabel, installationId, ...change })

  return (
    <GitHubReachView
      repositories={repositories}
      readOnly={readOnly}
      pickable={pickable}
      viewOnly={viewOnly}
      listing={listing}
      onPickerChange={setPicking}
      save={save}
      pending={setBinding.isPending}
      title="GitHub reach"
      notice={
        copied
          ? 'Starts from the repositories you chose for your own chats with this agent. Change it here.'
          : undefined
      }
    />
  )
}
