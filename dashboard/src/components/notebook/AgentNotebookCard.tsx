import { useId, useState } from 'react'
import { useAgentNotebook } from '../../api/notebook'
import { CARD } from '../ui/classes'
import NotebookHeader from './NotebookHeader'
import NotebookView from './NotebookView'

/** The agent page's Notebook: the entries this agent reads in every session,
 *  its own and those of the agents one delegation step away. Folded until
 *  opened and fetched on the first open. Everyone who can open the agent
 *  sees it; a manager of the posting agent deletes. */
export default function AgentNotebookCard({ name }: { name: string }) {
  const [open, setOpen] = useState(false)
  const [agent, setAgent] = useState('')
  const query = useAgentNotebook(name, agent, open)
  const sectionId = useId()
  return (
    <div className={`${CARD} p-4`}>
      <NotebookHeader open={open} onToggle={() => setOpen((v) => !v)} controls={sectionId}>
        Notes from this agent and the agents it works with. The agent reads the latest ones in every session.
      </NotebookHeader>
      {open && (
        <div id={sectionId} className="mt-3">
          <NotebookView query={query} agent={agent} onAgentChange={setAgent} idPrefix={`notebook-${name}`} />
        </div>
      )}
    </div>
  )
}
