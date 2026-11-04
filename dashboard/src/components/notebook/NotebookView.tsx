import type { UseInfiniteQueryResult, InfiniteData } from '@tanstack/react-query'
import { useDeleteNotebookEntry, type NotebookEntry, type NotebookPage } from '../../api/notebook'
import { toastError } from '../../lib/http/apiJson'
import { useDialogs } from '../ui/dialogs'
import Button from '../ui/Button'
import { Select } from '../ui/Field'

// Where an entry was written, by the session's kind. The person is named only
// when the server sent the name (the author, the agent's managers, admins).
const WHERE: Record<string, (author: string) => string> = {
  chat: (author) => (author ? `In a chat with ${author}` : 'In a chat'),
  task: () => 'In a task',
  meeting: () => 'In a meeting',
  phone: (author) => (author ? `On a phone call with ${author}` : 'On a phone call'),
}

function whereWritten(entry: NotebookEntry): string {
  const words = WHERE[entry.surface]
  return words ? words(entry.author) : 'In a session'
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

interface Props {
  query: UseInfiniteQueryResult<InfiniteData<NotebookPage>>
  agent: string
  onAgentChange: (agent: string) => void
  /** The id prefix of the filter, unique on the page. */
  idPrefix: string
}

/** A Notebook's entries, newest first, with a filter by agent, the manager's
 *  delete and older pages on demand. */
export default function NotebookView({ query, agent, onAgentChange, idPrefix }: Props) {
  const dialogs = useDialogs()
  const del = useDeleteNotebookEntry()
  const pages = query.data?.pages ?? []
  const agents = pages[0]?.agents ?? []
  const names = new Map(agents.map((a) => [a.slug, a.display_name]))
  const entries = pages.flatMap((p) => p.entries)

  const remove = async (entry: NotebookEntry) => {
    const name = names.get(entry.agent) || entry.agent
    const ok = await dialogs.confirm({
      title: 'Delete this entry?',
      body: `The agents around ${name} stop reading it. The record keeps who deleted it and when.`,
      confirmLabel: 'Delete',
      tone: 'danger',
    })
    if (ok) await del.mutateAsync(entry.id).catch(toastError('The entry was not deleted'))
  }

  return (
    <div className="space-y-3">
      {agents.length > 1 && (
        <div className="flex items-center gap-2">
          <label htmlFor={`${idPrefix}-agent`} className="text-xs text-secondary">Agent</label>
          <Select id={`${idPrefix}-agent`} compact className="max-w-60" value={agent}
                  onChange={(e) => onAgentChange(e.target.value)}>
            <option value="">All agents</option>
            {agents.map((a) => <option key={a.slug} value={a.slug}>{a.display_name}</option>)}
          </Select>
        </div>
      )}
      {query.isLoading ? (
        <p className="text-sm text-tertiary">Loading…</p>
      ) : query.isError ? (
        <p className="text-sm text-status-danger-fg">{query.error.message}</p>
      ) : entries.length === 0 ? (
        <p className="text-sm text-tertiary">No entries yet.</p>
      ) : (
        <ul className="space-y-3">
          {entries.map((entry) => (
            <li key={entry.id} className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-xs text-tertiary">
                  <span className="font-semibold text-secondary">{names.get(entry.agent) || entry.agent}</span>
                  {' · '}{when(entry.created_at)}{' · '}{whereWritten(entry)}
                </p>
                <p className="text-sm text-primary break-words mt-0.5">{entry.text}</p>
              </div>
              {entry.can_delete && (
                <Button variant="ghost" size="xs" disabled={del.isPending}
                        aria-label={`Delete this entry by ${names.get(entry.agent) || entry.agent}`}
                        onClick={() => { void remove(entry) }}>
                  Delete
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {query.hasNextPage && (
        <Button size="xs" disabled={query.isFetchingNextPage} onClick={() => { void query.fetchNextPage() }}>
          {query.isFetchingNextPage ? 'Loading…' : 'Show older entries'}
        </Button>
      )}
    </div>
  )
}
