import { useId, useRef, useState } from 'react'

import { useChatAudioCapability } from '../../hooks/useChatAudioCapability'
import { useSaveState } from '../../hooks/useSaveState'
import { ApiError } from '../../lib/http/apiJson'
import Button from '../ui/Button'
import ChatCard from '../ui/ChatCard'
import { useDialogs } from '../ui/dialogs'
import { Input, Select } from '../ui/Field'
import InfoTip from '../ui/InfoTip'
import { SaveState } from '../ui/SettingsControls'
import Switch from '../ui/Switch'
import { KIND, findEntry, type CatalogEntry } from '../settings/catalog'
import { useSettingCard, useSettingValue, type SettingValue } from '../settings/useSettingValue'
import { whereLine } from '../settings/where'

/**
 * A setting's card in the chat: the settings tool (settings-mcp) asked for
 * it, and every viewer of the chat changes their own setting in it. The row
 * behind it holds the catalog's id and label alone; the value is read and
 * saved here with the viewer's own sign-in, whoever's turn asked for the
 * card. Whether the viewer changes the row is the server's answer
 * (`GET /v1/settings/card/{id}`): its refusal's sentence shows in the
 * control's place. An admin's setting saves behind a confirm that names the
 * effect. A row the catalog no longer has shows one sentence.
 */

const NOT_IN_THIS_VERSION = 'This setting is not in this version.'
const OPERATOR_MANAGES_IT = 'Your operator manages this setting.'
const POLICY_SETS_IT = 'The platform policy sets this one.'
const KEPT_IN_BROWSER = 'Kept in this browser.'
const READING = 'Reading the setting.'
const NOT_READ = 'The setting did not load. Reload the page to see the card.'
/** The statuses whose sentence is the server's answer to this viewer. */
const REFUSALS = [403, 404, 409]

const AUDIO_MODE_FIELDS: Record<string, 'tts' | 'stt'> = { tts_mode: 'tts', stt_mode: 'stt' }

interface Props {
  settingId: string
  label: string
  catalogVersion?: number
}

/** The sentence a refusal of the card's answer carries, else '' (an older
 *  proxy's or a proxy's own text is never shown). */
function refusalOf(error: unknown): string {
  if (!(error instanceof ApiError) || !REFUSALS.includes(error.status)) return ''
  return typeof error.detail === 'string' && error.detail.endsWith('.') ? error.detail : ''
}

export default function SettingCard({ settingId, label }: Props) {
  const entry = findEntry(settingId)
  const known = Boolean(entry && entry.value)
  const answer = useSettingCard(settingId, known)

  let line: string | null = null
  if (!known) line = NOT_IN_THIS_VERSION
  else if (answer.isLoading) line = READING
  else if (answer.isError) line = refusalOf(answer.error) || NOT_READ

  return (
    <ChatCard
      title={
        <>
          <span id={`setting-${entry?.id ?? 'gone'}`}>{entry?.label || label}</span>
          {entry && <InfoTip label={entry.label}>{entry.help}</InfoTip>}
        </>
      }
      sub={entry ? `${whereLine(entry)}${entry.note ? ` ${entry.note}` : ''}` : undefined}
      line={line ?? undefined}
      testId="setting-card"
    >
      {!line && <SettingControl entry={entry as CatalogEntry} settingId={settingId} />}
    </ChatCard>
  )
}

function SettingControl({ entry, settingId }: { entry: CatalogEntry; settingId: string }) {
  const state = useSettingValue(entry)
  const { stateOf, run } = useSaveState()
  const dialogs = useDialogs()
  const id = useId()
  // Where the focus lands after a save: Save disables itself once the value
  // is in, so a confirm's close would otherwise send it to the info button.
  const controlRef = useRef<HTMLDivElement>(null)
  const binding = entry.value && 'read' in entry.value ? entry.value : null
  const modeKind = binding ? AUDIO_MODE_FIELDS[binding.read.field] : undefined
  const capability = useChatAudioCapability(Boolean(modeKind))
  const policySetsIt = Boolean(modeKind) && capability.data !== undefined && capability.data[modeKind!] !== 'either'

  // A draft the person edits, re-seated whenever the read value changes.
  const [seen, setSeen] = useState<SettingValue>(state.value)
  const [draft, setDraft] = useState<SettingValue>(state.value)
  if (seen !== state.value) {
    setSeen(state.value)
    setDraft(state.value)
  }

  const choose = (next: SettingValue) => {
    setDraft(next)
    if (state.device) void run(settingId, () => state.save(next), { failTitle: 'The setting was not saved' })
  }

  // A text is saved trimmed, and never blank (the page refuses a blank name).
  const text = entry.kind === KIND.TEXT
  const saveDraft = async () => {
    // Only a viewer the server lets change the row reaches its control.
    if (binding?.confirm) {
      const ok = await dialogs.confirm({ title: entry.label, body: binding.confirm, confirmLabel: 'Save' })
      if (!ok) return
    }
    controlRef.current?.querySelector<HTMLElement>('button, select, input')?.focus()
    const next = text ? String(draft ?? '').trim() : draft
    void run(settingId, () => state.save(next), {
      failTitle: 'The setting was not saved', revert: () => setDraft(state.value),
    })
  }

  const blank = text && String(draft ?? '').trim() === ''
  const dirty = draft !== state.value && !blank
  const saving = stateOf(settingId) === 'saving'
  const labelId = `setting-${entry.id}`

  if (policySetsIt) return <p className="mt-1 text-sm text-secondary">{POLICY_SETS_IT}</p>
  if (state.managed) return <p className="mt-1 text-sm text-secondary">{OPERATOR_MANAGES_IT}</p>
  if (state.loading) return <p className="mt-1 text-sm text-tertiary">{READING}</p>

  let control: React.ReactNode
  if (entry.kind === KIND.TOGGLE) {
    control = <Switch checked={Boolean(draft)} onChange={(v) => choose(v)} disabled={saving} aria-labelledby={labelId} />
  } else if (entry.kind === KIND.CHOICE) {
    control = (
      <Select id={id} aria-labelledby={labelId} value={String(draft ?? '')} disabled={saving} onChange={(e) => choose(e.target.value)} className="max-w-xs">
        {(entry.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
    )
  } else if (entry.kind === KIND.NUMBER) {
    control = <Input id={id} type="number" aria-labelledby={labelId} value={draft === null ? '' : String(draft)} disabled={saving} onChange={(e) => setDraft(e.target.value === '' ? null : Number(e.target.value))} className="max-w-xs" />
  } else {
    control = <Input id={id} type="text" aria-labelledby={labelId} value={String(draft ?? '')} disabled={saving} onChange={(e) => setDraft(e.target.value)} className="max-w-xs" />
  }

  return (
    <div ref={controlRef} className="mt-2 flex flex-wrap items-center gap-3">
      {control}
      {state.device ? (
        <span className="text-xs text-tertiary">{KEPT_IN_BROWSER}</span>
      ) : (
        <Button variant="primary" size="sm" disabled={!dirty || saving} onClick={() => { void saveDraft() }}>Save</Button>
      )}
      <SaveState state={stateOf(settingId)} />
    </div>
  )
}
