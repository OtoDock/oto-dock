import { type McpInstanceField, type SshKey } from '../../api/mcps'
import type { PhoneRoute } from '../../api/phone'
import type { AudioProvider } from '../../api/audio'

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
  return (
          <div key={f.key}>
            <label className="text-xs text-p-text-light">
              {f.label}
              {f.required && <span className="text-red-400 ml-0.5">*</span>}
            </label>
            {f.input_type === 'ssh_key_select' ? (
              <select
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              >
                <option value="">No key</option>
                {sshKeys?.map(k => <option key={k.name} value={k.name}>{k.name}</option>)}
              </select>
            ) : f.input_type === 'phone_route_outbound_select' ? (
              // Dropdown sourced from enabled outbound phone
              // routes. Value = route.id (UUID), label = human-readable
              // name (falls back to "<direction> <id-prefix>" if route
              // has no name).
              <select
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              >
                <option value="">— Select an outbound route —</option>
                {outboundRoutes.map(r => (
                  <option key={r.id} value={r.id}>
                    {r.name || `outbound ${r.id.slice(0, 8)}`} ({r.agent})
                  </option>
                ))}
              </select>
            ) : f.input_type === 'stt_provider_select' ? (
              // transcribe-mcp: bind an STT provider to this instance. Value =
              // provider id (string); blank = the platform's default STT.
              <select
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              >
                <option value="">— Platform default —</option>
                {sttProviders.map(p => (
                  <option key={p.id} value={String(p.id)}>
                    {p.label} ({p.provider_name})
                  </option>
                ))}
              </select>
            ) : f.input_type === 'tts_provider_select' ? (
              // tts-mcp: bind a TTS provider to this instance. Value =
              // provider id (string); blank = the platform's default TTS.
              <select
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              >
                <option value="">— Platform default —</option>
                {ttsProviders.map(p => (
                  <option key={p.id} value={String(p.id)}>
                    {p.label} ({p.provider_name})
                  </option>
                ))}
              </select>
            ) : f.input_type === 'password' || f.secret ? (
              <input
                type="password"
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                placeholder={isEdit && editing.field_values[f.key] === '' ? '(unchanged)' : ''}
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              />
            ) : (
              <input
                type={f.input_type === 'number' ? 'number' : 'text'}
                value={editing.field_values[f.key] || ''}
                onChange={e => updateField(f.key, e.target.value)}
                placeholder={f.default || ''}
                className={`w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border bg-white dark:bg-gray-900 text-p-text ${
                  fieldErrors[f.key]
                    ? 'border-red-400 dark:border-red-700'
                    : 'border-p-border-light'
                }`}
              />
            )}
            {fieldErrors[f.key] && (
              <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                {fieldErrors[f.key]}
              </p>
            )}
          </div>
  )
}
