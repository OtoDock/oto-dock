import { useId } from 'react'

import { type McpInstanceField, type SshKey } from '../../api/mcps'
import type { PhoneRoute } from '../../api/phone'
import type { AudioProvider } from '../../api/audio'
import { Input, Select } from '../ui/Field'

export function InstanceFieldInput({
  f,
  editing,
  updateField,
  isEdit,
  fieldErrors,
  sshKeys,
  outboundRoutes,
  sttProviders,
  ttsProviders,
}: {
  f: McpInstanceField
  editing: { id?: number; instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all: boolean; hosted_mode: 'self_managed' | 'hosted'; managed_by?: 'admin' | 'system' }
  updateField: (key: string, value: string) => void
  isEdit: boolean
  fieldErrors: Record<string, string>
  sshKeys?: SshKey[]
  outboundRoutes: PhoneRoute[]
  sttProviders: AudioProvider[]
  ttsProviders: AudioProvider[]
}) {
  const id = useId()
  const errorId = `${id}-error`
  return (
          <div key={f.key}>
            <label htmlFor={id} className="text-xs text-tertiary">
              {f.label}
              {f.required && <span className="text-status-danger-fg ml-0.5">*</span>}
            </label>
            {f.input_type === 'ssh_key_select' ? (
              <Select
                id={id}
                className="mt-1"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
              >
                <option value="">No key</option>
                {sshKeys?.map(k => <option key={k.name} value={k.name}>{k.name}</option>)}
              </Select>
            ) : f.input_type === 'phone_route_outbound_select' ? (
              // Dropdown sourced from enabled outbound phone
              // routes. Value = route.id (UUID), label = human-readable
              // name (falls back to "<direction> <id-prefix>" if route
              // has no name).
              <Select
                id={id}
                className="mt-1"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
              >
                <option value="">Select an outbound route</option>
                {outboundRoutes.map(r => (
                  <option key={r.id} value={r.id}>
                    {r.name || `outbound ${r.id.slice(0, 8)}`} ({r.agent})
                  </option>
                ))}
              </Select>
            ) : f.input_type === 'stt_provider_select' ? (
              // transcribe-mcp: bind an STT provider to this instance. Value =
              // provider id (string); blank = the platform's default STT.
              <Select
                id={id}
                className="mt-1"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
              >
                <option value="">Platform default</option>
                {sttProviders.map(p => (
                  <option key={p.id} value={String(p.id)}>
                    {p.label} ({p.provider_name})
                  </option>
                ))}
              </Select>
            ) : f.input_type === 'tts_provider_select' ? (
              // tts-mcp: bind a TTS provider to this instance. Value =
              // provider id (string); blank = the platform's default TTS.
              <Select
                id={id}
                className="mt-1"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
              >
                <option value="">Platform default</option>
                {ttsProviders.map(p => (
                  <option key={p.id} value={String(p.id)}>
                    {p.label} ({p.provider_name})
                  </option>
                ))}
              </Select>
            ) : f.input_type === 'password' || f.secret ? (
              <Input
                id={id}
                className="mt-1"
                type="password"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                placeholder={isEdit && editing.field_values[f.key] === '' ? 'Unchanged' : ''}
              />
            ) : (
              <Input
                id={id}
                className="mt-1"
                type={f.input_type === 'number' ? 'number' : 'text'}
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                placeholder={f.default || ''}
                invalid={!!fieldErrors[f.key]}
                aria-describedby={fieldErrors[f.key] ? errorId : undefined}
              />
            )}
            {fieldErrors[f.key] && (
              <p id={errorId} className="mt-1 text-2xs text-status-danger-fg">
                {fieldErrors[f.key]}
              </p>
            )}
          </div>
  )
}
