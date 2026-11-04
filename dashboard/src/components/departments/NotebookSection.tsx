import { useId, useState } from 'react'
import { useDepartmentNotebook } from '../../api/notebook'
import NotebookHeader from '../notebook/NotebookHeader'
import NotebookView from '../notebook/NotebookView'

/** The department card's Notebook: the entries of the members this person
 *  can open, whatever the department's mode, newest first. Folded until
 *  opened and fetched on the first open. */
export default function NotebookSection({ departmentId }: { departmentId: string }) {
  const [open, setOpen] = useState(false)
  const [agent, setAgent] = useState('')
  const query = useDepartmentNotebook(departmentId, agent, open)
  const sectionId = useId()
  return (
    <div>
      <NotebookHeader open={open} onToggle={() => setOpen((v) => !v)} controls={sectionId}>
        Notes from the department&apos;s agents about their work. Each agent reads the ones of the agents it works with.
      </NotebookHeader>
      {open && (
        <div id={sectionId} className="mt-3">
          <NotebookView query={query} agent={agent} onAgentChange={setAgent} idPrefix={`dept-notebook-${departmentId}`} />
        </div>
      )}
    </div>
  )
}
