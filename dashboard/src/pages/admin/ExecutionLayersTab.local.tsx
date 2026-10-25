/**
 * Shared "Local models" section — self-hosted OpenAI-compatible endpoints
 * (Ollama, llama.cpp, LM Studio, vLLM, LiteLLM) listed ONCE and enabled per
 * engine. Rendered inside the card of every engine whose descriptor accepts
 * `local_endpoint` rows: the rows are the same everywhere, only Discover
 * acts for the card's engine.
 */

import { Fragment, useRef, useState, type ReactNode } from 'react'
import {
  useAddLocalEndpoint,
  useAdminExecutionLayers,
  useAdminLocalEndpoints,
  useDeleteLocalEndpoint,
  useSetLocalEndpointEngine,
  type LocalEndpointGroup,
} from '../../api/executionLayers'
import { acceptsLocalEndpoints, engineLabel, sortEngineRows } from '../../lib/engines'
import { ENGINE_SUBSCRIPTION_STATUS } from '../../lib/status/engineSubscription'
import { Badge, SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import InfoTip from '../../components/ui/InfoTip'
import { PROVIDER_LABELS } from './ExecutionLayersTab.labels'
import { useDialogs } from '../../components/ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import { useInvokedForm } from '../../components/common/useInvokedForm'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'

interface LocalEngine {
  id: string
  label: string
}

/** The engines a self-hosted endpoint may serve — those whose descriptor
 *  lists `local_endpoint` among its auth types — in engine order. Read from
 *  the admin catalog (the same query the tab renders from). */
function useLocalEngines(): LocalEngine[] {
  const { data: layers } = useAdminExecutionLayers()
  return sortEngineRows(layers ?? [])
    .filter((l) => acceptsLocalEndpoints(l.capabilities))
    .map((l) => ({ id: l.name, label: engineLabel(l.capabilities) }))
}

const LOCAL_PROVIDERS = [
  { id: 'ollama', label: 'Ollama', url: 'http://localhost:11434/v1' },
  { id: 'openai_compatible', label: 'OpenAI-compatible server', url: 'http://localhost:1234/v1' },
] as const

export interface LocalDiscoverTarget {
  id: string
  provider: string
  layers: string[]
}

function enabledEngines(g: LocalEndpointGroup, engines: LocalEngine[]): string[] {
  return engines.map((e) => e.id).filter((id) => g.engines[id]?.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE)
}

export function LocalModelsSection({ layer, onDiscover, belowRow }: {
  layer: string
  onDiscover: (target: LocalDiscoverTarget) => void
  /** What opens under a server's row (its discover panel), by the id of
   *  the server's entry on this engine. */
  belowRow?: (id: string) => ReactNode
}) {
  const { data: groups } = useAdminLocalEndpoints()
  const engines = useLocalEngines()
  const [showAdd, setShowAdd] = useState(false)
  const list = groups ?? []

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">Local models</h4>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="text-xs text-link hover:text-link-hover transition-colors"
        >
          + Local server
        </button>
      </div>
      <p className="text-2xs text-secondary leading-snug mb-1">
        Self-hosted OpenAI-compatible servers, like Ollama, llama.cpp, LM Studio and vLLM.
      </p>
      {/* The add form opens under its button, above the servers. */}
      {showAdd && <AddLocalEndpointForm currentLayer={layer} engines={engines} onDone={() => setShowAdd(false)} />}
      {list.length === 0 && !showAdd && (
        <p className="text-sm text-tertiary py-2">No local servers connected.</p>
      )}
      <div className="space-y-0.5">
        {list.map((g) => {
          const here = g.engines[layer]
          return (
            <Fragment key={g.group}>
              <LocalEndpointRow group={g} layer={layer} engines={engines} onDiscover={onDiscover} />
              {here && belowRow?.(here.id)}
            </Fragment>
          )
        })}
      </div>
    </div>
  )
}

function LocalEndpointRow({ group: g, layer, engines, onDiscover }: {
  group: LocalEndpointGroup
  layer: string
  engines: LocalEngine[]
  onDiscover: (target: LocalDiscoverTarget) => void
}) {
  const dialogs = useDialogs()
  const setEngine = useSetLocalEndpointEngine()
  const del = useDeleteLocalEndpoint()
  const { stateOf, run } = useSaveState()
  const rows = Object.values(g.engines)
  const manageable = rows.every((e) => e.is_mine)
  const here = g.engines[layer]
  const canDiscover = here?.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE

  return (
    <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-2 px-3 rounded-card hover:bg-surface-hover group">
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium text-primary truncate">{g.label || g.endpoint_url}</span>
          <Badge variant="info">{PROVIDER_LABELS[g.provider] || g.provider}</Badge>
          {g.has_api_key && <Badge>Key set</Badge>}
        </div>
        {g.label && <p className="text-xs text-tertiary truncate font-mono">{g.endpoint_url}</p>}
        <div className="flex items-center gap-3 mt-1">
          {engines.map((e) => (
            <span key={e.id} className="inline-flex items-center">
              <label className="flex items-center gap-1 text-xs text-tertiary cursor-pointer">
                <input
                  type="checkbox"
                  aria-label={e.label}
                  checked={g.engines[e.id]?.status === ENGINE_SUBSCRIPTION_STATUS.ACTIVE}
                  disabled={!manageable || setEngine.isPending}
                  onChange={(ev) => {
                    const enabled = ev.target.checked
                    void run(e.id, () => setEngine.mutateAsync({ group: g.group, layer: e.id, enabled }),
                      { failTitle: 'The local server setting was not saved' })
                  }}
                />
                {e.label}
              </label>
              <InfoTip label={e.label}>{`Make this server available on ${e.label}`}</InfoTip>
              <SaveState state={stateOf(e.id)} />
            </span>
          ))}
          {!manageable && <span className="text-xs text-tertiary">Shared by another admin</span>}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={() => canDiscover && here && onDiscover({ id: here.id, provider: g.provider, layers: enabledEngines(g, engines) })}
          disabled={!canDiscover}
          className="text-xs text-link hover:text-link-hover transition-colors disabled:opacity-40 sm:opacity-0 sm:group-hover:opacity-100"
          title={canDiscover ? 'Discover the models this server offers' : 'Turn this server on for this engine first'}
        >
          Discover
        </button>
        {manageable && (
          <button
            onClick={async () => {
              // The server judges "in use" on its live bindings (the counter
              // shown here can read stale) and answers 409 only for sessions
              // really running — the admin may still remove it and let them move.
              if (!(await dialogs.confirm({ title: 'Remove this local server from every engine?', confirmLabel: 'Remove', tone: 'danger' }))) return
              try {
                await del.mutateAsync({ group: g.group })
              } catch (e) {
                const err = e as Error & { status?: number }
                if (err.status !== 409) {
                  toastError('The local server was not removed')(e)
                  return
                }
                if (await dialogs.confirm({
                  title: 'Remove it anyway?',
                  body: `${err.message}\n\nIts running sessions move to another subscription, or stop until one is connected.`,
                  confirmLabel: 'Remove anyway',
                  tone: 'danger',
                })) {
                  void del.mutateAsync({ group: g.group, force: true }).catch(toastError('The local server was not removed'))
                }
              }
            }}
            className="text-xs text-status-danger-fg hover:underline sm:opacity-0 sm:group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>
    </div>
  )
}

export function AddLocalEndpointForm({ currentLayer, engines, onDone }: {
  currentLayer: string
  engines: LocalEngine[]
  onDone: () => void
}) {
  const [provider, setProvider] = useState<string>(LOCAL_PROVIDERS[1].id)
  const [label, setLabel] = useState('')
  const [url, setUrl] = useState<string>(LOCAL_PROVIDERS[1].url)
  const [apiKey, setApiKey] = useState('')
  // Every engine that accepts a local endpoint is on by default (operator
  // decision): the admin unticks what it should not serve.
  const [layers, setLayers] = useState<string[]>(() => engines.map((e) => e.id))
  const addMut = useAddLocalEndpoint()
  const formRef = useRef<HTMLDivElement>(null)
  const form = useInvokedForm(formRef)

  const pickProvider = (id: string) => {
    const p = LOCAL_PROVIDERS.find((x) => x.id === id)
    setProvider(id)
    if (p && LOCAL_PROVIDERS.some((x) => x.url === url)) setUrl(p.url)
  }
  const toggleLayer = (id: string, on: boolean) =>
    setLayers((prev) => (on ? [...new Set([...prev, id])] : prev.filter((x) => x !== id)))

  const handleSubmit = () => {
    if (!url.trim() || layers.length === 0) return
    addMut.mutate(
      {
        provider, label: label.trim(), endpoint_url: url.trim(), layers,
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      },
      { onSuccess: () => { setLabel(''); setApiKey(''); form.close(onDone) } },
    )
  }

  return (
    <div
      ref={formRef}
      onKeyDown={(e) => form.onKeyDown(e, onDone)}
      className="mt-3 p-3 bg-surface-raised shadow-elevation-2 rounded-card space-y-2"
      data-current-layer={currentLayer}
    >
      <Select value={provider} onChange={(e) => pickProvider(e.target.value)} aria-label="Provider">
        {LOCAL_PROVIDERS.map((p) => (
          <option key={p.id} value={p.id}>{p.label}</option>
        ))}
      </Select>
      <Input type="text" placeholder="Label, optional" value={label} onChange={(e) => setLabel(e.target.value)} />
      <Input type="url" placeholder="Server address" aria-label="Server address" value={url} onChange={(e) => setUrl(e.target.value)} className="font-mono" />
      <Input
        type="password"
        placeholder="API key, only if the server requires one"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        className="font-mono"
      />
      <div className="flex items-center gap-3 text-xs text-tertiary">
        <span>Use with:</span>
        {engines.map((e) => (
          <label key={e.id} className="flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              aria-label={`Use with ${e.label}`}
              checked={layers.includes(e.id)}
              onChange={(ev) => toggleLayer(e.id, ev.target.checked)}
            />
            {e.label}
          </label>
        ))}
      </div>
      <p className="text-2xs text-secondary leading-snug">
        On a Docker install, use the host's LAN IP or
        <span className="font-mono"> host.docker.internal</span>. Do not use
        <span className="font-mono"> localhost</span>: it points inside the
        OtoDock container, not your machine.
      </p>
      <div className="flex gap-2">
        <Button variant="primary" size="sm"
          onClick={handleSubmit}
          disabled={!url.trim() || layers.length === 0 || addMut.isPending}
        >
          {addMut.isPending ? 'Adding...' : 'Add'}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => form.close(onDone)}>
          Cancel
        </Button>
      </div>
      {addMut.isError && <p className="text-xs text-status-danger-fg">{(addMut.error as Error).message}</p>}
    </div>
  )
}
