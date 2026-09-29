import { useState } from 'react'
import {
  type CheckDoc, type CheckItem, useSaveCheck, CHECK_KINDS_WORDS, CHECK_EVENTS, CHECK_PLACES,
} from '../../api/checks'
import { useExecutionLayers } from '../../api/agents'
import { engineLabel, orderedEngines } from '../../lib/engines'

// The check editor (proxy CHECKS.md "The check document"): a name, a
// sentence, mandatory or offered, what it applies to, when it runs, the
// rounds, and the sections — the judge's rubric in front, the script, the
// handler and the schema folded as advanced fields. A private check has
// no "mandatory".

const APPLIES = [
  { id: 'chats', label: 'chats' }, { id: 'tasks', label: 'tasks' }, { id: 'delegations', label: 'delegations' },
]
function csv(list: string[] | undefined): string { return (list ?? []).join(', ') }
function fromCsv(s: string): string[] { return s.split(',').map((x) => x.trim()).filter(Boolean) }

export function CheckEditorModal({ agent, existing, own, onClose }: {
  agent: string; existing?: CheckItem; own: boolean; onClose: () => void
}) {
  const save = useSaveCheck()
  // The judge's engine choices: the agent's default, then every engine the
  // platform registers, from the catalog (never a hard-coded list).
  const { data: layers } = useExecutionLayers()
  const engines = [
    { id: '', label: "the agent's default" },
    ...orderedEngines(layers).map((e) => ({ id: e.name, label: engineLabel(e) })),
  ]
  const d: CheckDoc = existing?.doc ?? { name: '' }
  const [name, setName] = useState(existing?.name ?? '')
  const [description, setDescription] = useState(d.description ?? '')
  const [mandatory, setMandatory] = useState(!!d.mandatory)
  const [applies, setApplies] = useState<string[]>(d.applies ?? ['chats', 'tasks', 'delegations'])
  const [always, setAlways] = useState(!!d.condition?.always)
  const [kinds, setKinds] = useState<string[]>(d.condition?.kinds ?? [])
  const [events, setEvents] = useState<string[]>(d.condition?.events ?? [])
  const [places, setPlaces] = useState<string[]>(d.condition?.places ?? [])
  const [globs, setGlobs] = useState(csv(d.condition?.globs))
  const [rounds, setRounds] = useState(d.rounds ?? 3)
  const [inputs, setInputs] = useState(csv(d.inputs))
  const [rubric, setRubric] = useState(d.judge?.rubric ?? '')
  const [engine, setEngine] = useState(d.judge?.engine ?? '')
  const [model, setModel] = useState(d.judge?.model ?? '')
  const [threshold, setThreshold] = useState(d.judge?.threshold == null ? '' : String(d.judge.threshold))
  const [mcps, setMcps] = useState(csv(d.judge?.mcps))
  const [judgeOn, setJudgeOn] = useState<'auto' | 'platform'>(d.judge?.judge_on ?? 'auto')
  const [judgeTimeout, setJudgeTimeout] = useState(d.judge?.timeout ?? 600)
  const [advanced, setAdvanced] = useState(!!(d.script || d.handler || d.schema))
  const [scriptRun, setScriptRun] = useState(d.script?.run ?? '')
  const [scriptTimeout, setScriptTimeout] = useState(d.script?.timeout ?? 600)
  const [scriptText, setScriptText] = useState('')
  const [handlerApp, setHandlerApp] = useState(d.handler?.app ?? '')
  const [handlerName, setHandlerName] = useState(d.handler?.handler ?? '')
  const [schemaText, setSchemaText] = useState(d.schema ? JSON.stringify(d.schema, null, 2) : '')
  const [error, setError] = useState('')

  const toggle = (list: string[], set: (v: string[]) => void, id: string) =>
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])

  const submit = () => {
    setError('')
    const doc: CheckDoc = {
      name: name.trim(), description: description.trim(), mandatory: own ? false : mandatory,
      applies, rounds,
      condition: always ? { always: true } : {
        ...(kinds.length ? { kinds } : {}), ...(events.length ? { events } : {}),
        ...(places.length ? { places } : {}), ...(fromCsv(globs).length ? { globs: fromCsv(globs) } : {}),
        // The command patterns have no field here (a regex may hold a
        // comma): they ride along unchanged rather than vanish on a save.
        ...(d.condition?.commands?.length ? { commands: d.condition.commands } : {}),
      },
      inputs: fromCsv(inputs),
    }
    if (rubric.trim()) {
      doc.judge = {
        rubric: rubric.trim(), engine, model: model.trim(),
        threshold: threshold.trim() === '' ? null : Number(threshold),
        mcps: fromCsv(mcps), judge_on: judgeOn, timeout: judgeTimeout,
      }
    }
    if (advanced && scriptRun.trim()) doc.script = { run: scriptRun.trim(), timeout: scriptTimeout }
    if (advanced && handlerApp.trim() && handlerName.trim()) doc.handler = { app: handlerApp.trim(), handler: handlerName.trim() }
    if (advanced && schemaText.trim()) {
      try { doc.schema = JSON.parse(schemaText) } catch { setError('The schema is not valid JSON'); return }
    }
    if (!doc.judge && !doc.script && !doc.handler && !doc.schema) {
      setError('A check needs instructions for the judge, a script, a handler or a schema'); return
    }
    save.mutate(
      { agent, name: doc.name, doc, script: scriptText.trim() ? scriptText : null, own },
      { onSuccess: onClose, onError: (e: Error) => setError(e.message) },
    )
  }

  const field = 'w-full px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm'
  const chip = (on: boolean) => `px-2 py-0.5 rounded-full text-xs border ${on ? 'bg-brand text-white border-brand' : 'border-p-border-light text-p-text-secondary'}`

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-0 sm:p-4" data-testid="check-editor">
      <div className="w-full sm:max-w-2xl max-h-[92vh] overflow-y-auto rounded-t-2xl sm:rounded-2xl bg-white dark:bg-p-surface border border-p-border-light p-4 space-y-3">
        <h2 className="text-lg font-semibold text-p-text">
          {existing ? `Edit ${existing.name}` : own ? 'New private check' : 'New check'}
        </h2>
        <p className="text-xs text-p-text-secondary">
          {own ? 'Your own check: it runs on your sessions when you attach it.'
            : 'The agent’s check. Mandatory, it runs on every session; otherwise it is offered, and people attach it to a chat or a task when they want it.'}
        </p>
        <label className="block text-sm">
          <span className="text-p-text-secondary">Name</span>
          <input className={field} value={name} onChange={(e) => setName(e.target.value)} disabled={!!existing}
                 placeholder="coding" data-testid="check-name" />
        </label>
        <label className="block text-sm">
          <span className="text-p-text-secondary">What it checks, in a sentence</span>
          <input className={field} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {!own && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={mandatory} onChange={(e) => setMandatory(e.target.checked)} data-testid="check-mandatory" />
            <span>Mandatory — runs on every session of this agent (nobody can detach it inside a session)</span>
          </label>
        )}
        <div className="text-sm">
          <span className="text-p-text-secondary">Applies to</span>
          <div className="flex flex-wrap gap-1.5 mt-1">
            {APPLIES.map((a) => (
              <button key={a.id} type="button" className={chip(applies.includes(a.id))} onClick={() => toggle(applies, setApplies, a.id)}>{a.label}</button>
            ))}
          </div>
        </div>
        <div className="text-sm space-y-1.5">
          <span className="text-p-text-secondary">When it runs</span>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={always} onChange={(e) => setAlways(e.target.checked)} data-testid="check-always" />
            <span>After every turn, whatever the turn did</span>
          </label>
          {!always && (
            <div className="space-y-2 rounded-lg border border-p-border-light p-2.5" data-testid="check-condition">
              <p className="text-xs text-p-text-secondary">
                Only after a turn that did one of these. Nothing chosen means any file written or any action.
              </p>
              <div className="space-y-1">
                <span className="text-xs text-p-text-secondary">Wrote files of a kind</span>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(CHECK_KINDS_WORDS).map(([id, label]) => (
                    <button key={id} type="button" className={chip(kinds.includes(id))} onClick={() => toggle(kinds, setKinds, id)}>{label}</button>
                  ))}
                </div>
              </div>
              <div className="space-y-1">
                <span className="text-xs text-p-text-secondary">Ran one of these</span>
                <div className="flex flex-wrap gap-1.5">
                  {CHECK_EVENTS.map((id) => (
                    <button key={id} type="button" className={chip(events.includes(id))} onClick={() => toggle(events, setEvents, id)}>a {id}</button>
                  ))}
                </div>
              </div>
              <div className="space-y-1">
                <span className="text-xs text-p-text-secondary">Wrote where</span>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(CHECK_PLACES).map(([id, label]) => (
                    <button key={id} type="button" className={chip(places.includes(id))} onClick={() => toggle(places, setPlaces, id)}>{label}</button>
                  ))}
                </div>
                <input className={field} value={globs} onChange={(e) => setGlobs(e.target.value)} placeholder="only under these paths, comma-separated: workspace/src/**" />
              </div>
            </div>
          )}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
          <label className="block">
            <span className="text-p-text-secondary">Fix rounds (0 = report only)</span>
            <input type="number" min={0} max={3} className={field} value={rounds} onChange={(e) => setRounds(Number(e.target.value))} />
          </label>
          <label className="block">
            <span className="text-p-text-secondary">Files the judge also reads (comma-separated)</span>
            <input className={field} value={inputs} onChange={(e) => setInputs(e.target.value)} placeholder="knowledge/style.md" />
          </label>
        </div>
        <label className="block text-sm">
          <span className="text-p-text-secondary">Instructions for the judge (what to judge, what a pass is)</span>
          <textarea className={`${field} min-h-[96px]`} value={rubric} onChange={(e) => setRubric(e.target.value)} data-testid="check-rubric"
                    placeholder="Every changed function has a test. Pass when nothing is missing." />
        </label>
        {rubric.trim() && (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-sm">
            <label className="block">
              <span className="text-p-text-secondary">Engine</span>
              <select className={field} value={engine} onChange={(e) => setEngine(e.target.value)}>
                {engines.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
              </select>
            </label>
            <label className="block">
              <span className="text-p-text-secondary">Model (blank = default)</span>
              <input className={field} value={model} onChange={(e) => setModel(e.target.value)} />
            </label>
            <label className="block">
              <span className="text-p-text-secondary">Pass at score ≥</span>
              <input className={field} value={threshold} onChange={(e) => setThreshold(e.target.value)} placeholder="0.7 (blank = the judge decides)" />
            </label>
            <label className="block">
              <span className="text-p-text-secondary">MCPs the judge may use</span>
              <input className={field} value={mcps} onChange={(e) => setMcps(e.target.value)} placeholder="file-tools" />
            </label>
            <label className="block">
              <span className="text-p-text-secondary">Where the judge runs</span>
              <select className={field} value={judgeOn} onChange={(e) => setJudgeOn(e.target.value as 'auto' | 'platform')}>
                <option value="auto">where the session runs</option>
                <option value="platform">on the platform (synced copy)</option>
              </select>
            </label>
            <label className="block">
              <span className="text-p-text-secondary">Judge timeout (s)</span>
              <input type="number" min={30} max={1800} className={field} value={judgeTimeout} onChange={(e) => setJudgeTimeout(Number(e.target.value))} />
            </label>
          </div>
        )}
        <button type="button" className="text-xs text-brand" onClick={() => setAdvanced((v) => !v)}>
          {advanced ? 'Hide the script, the app handler and the schema' : 'More: a script, an app handler, a JSON schema'}
        </button>
        {advanced && (
          <div className="space-y-2 text-sm">
            <div className="grid grid-cols-2 gap-2">
              <label className="block">
                <span className="text-p-text-secondary">Script file name</span>
                <input className={field} value={scriptRun} onChange={(e) => setScriptRun(e.target.value)} placeholder="lint.sh" />
              </label>
              <label className="block">
                <span className="text-p-text-secondary">Script timeout (s)</span>
                <input type="number" min={1} max={7200} className={field} value={scriptTimeout} onChange={(e) => setScriptTimeout(Number(e.target.value))} />
              </label>
            </div>
            <label className="block">
              <span className="text-p-text-secondary">Script (a #! line names the interpreter; exit 0 passes){existing?.script_sha256 ? ' — blank keeps the saved script' : ''}</span>
              <textarea className={`${field} min-h-[96px] font-mono text-xs`} value={scriptText} onChange={(e) => setScriptText(e.target.value)} />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block">
                <span className="text-p-text-secondary">Handler app (slug)</span>
                <input className={field} value={handlerApp} onChange={(e) => setHandlerApp(e.target.value)} />
              </label>
              <label className="block">
                <span className="text-p-text-secondary">Handler name</span>
                <input className={field} value={handlerName} onChange={(e) => setHandlerName(e.target.value)} />
              </label>
            </div>
            <label className="block">
              <span className="text-p-text-secondary">Schema (JSON the answer must match)</span>
              <textarea className={`${field} min-h-[72px] font-mono text-xs`} value={schemaText} onChange={(e) => setSchemaText(e.target.value)} />
            </label>
          </div>
        )}
        {error && <p className="text-sm text-red-600" data-testid="check-error">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="px-3 py-1.5 rounded-lg text-sm text-p-text-secondary" onClick={onClose}>Cancel</button>
          <button type="button" className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover disabled:opacity-50"
                  onClick={submit} disabled={save.isPending || !name.trim()} data-testid="check-save">
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
