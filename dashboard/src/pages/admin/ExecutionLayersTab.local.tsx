/**
 * Shared "Local models" section — self-hosted OpenAI-compatible endpoints
 * (Ollama, llama.cpp, LM Studio, vLLM, LiteLLM) listed ONCE and enabled per
 * engine. Rendered inside both the Direct LLM API and the Codex CLI cards:
 * the rows are the same everywhere, only Discover acts for the card's engine.
 */

import { useState } from 'react'
import {
  useAddLocalEndpoint,
  useAdminLocalEndpoints,
  useDeleteLocalEndpoint,
  useSetLocalEndpointEngine,
  type LocalEndpointGroup,
} from '../../api/executionLayers'
import { Badge, PROVIDER_LABELS } from './ExecutionLayersTab.widgets'

export const LOCAL_ENGINES = [
  { id: 'direct-llm', label: 'Direct LLM API' },
  { id: 'codex-cli', label: 'Codex CLI' },
] as const

const LOCAL_PROVIDERS = [
  { id: 'ollama', label: 'Ollama', url: 'http://localhost:11434/v1' },
  { id: 'openai_compatible', label: 'OpenAI-compatible endpoint', url: 'http://localhost:1234/v1' },
] as const

export interface LocalDiscoverTarget {
  id: string
  provider: string
  layers: string[]
}

const INPUT = 'w-full px-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30'

function enabledEngines(g: LocalEndpointGroup): string[] {
  return LOCAL_ENGINES.map((e) => e.id).filter((id) => g.engines[id]?.status === 'active')
}

export function LocalModelsSection({ layer, onDiscover }: {
  layer: string
  onDiscover: (target: LocalDiscoverTarget) => void
}) {
  const { data: groups } = useAdminLocalEndpoints()
  const [showAdd, setShowAdd] = useState(false)
  const list = groups ?? []

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Local models</h4>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="text-xs text-brand hover:text-brand-hover transition-colors"
        >
          + Local endpoint
        </button>
      </div>
      <p className="text-[11px] text-p-text-secondary leading-snug mb-1">
        Self-hosted OpenAI-compatible servers (Ollama, llama.cpp, LM Studio, vLLM).
      </p>
      {list.length === 0 && !showAdd && (
        <p className="text-sm text-p-text-light py-2">No local endpoints connected.</p>
      )}
      <div className="space-y-0.5">
        {list.map((g) => (
          <LocalEndpointRow key={g.group} group={g} layer={layer} onDiscover={onDiscover} />
        ))}
      </div>
      {showAdd && <AddLocalEndpointForm currentLayer={layer} onDone={() => setShowAdd(false)} />}
    </div>
  )
}

function LocalEndpointRow({ group: g, layer, onDiscover }: {
  group: LocalEndpointGroup
  layer: string
  onDiscover: (target: LocalDiscoverTarget) => void
}) {
  const setEngine = useSetLocalEndpointEngine()
  const del = useDeleteLocalEndpoint()
  const rows = Object.values(g.engines)
  const manageable = rows.every((e) => e.is_mine)
  const here = g.engines[layer]
  const canDiscover = here?.status === 'active'

  return (
    <div className="flex flex-wrap items-center gap-2 sm:gap-3 py-2 px-3 rounded-lg hover:bg-p-bg-hover/50 group">
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium text-p-text truncate">{g.label || g.endpoint_url}</span>
          <Badge variant="blue">{PROVIDER_LABELS[g.provider] || g.provider}</Badge>
          {g.has_api_key && <Badge>key set</Badge>}
        </div>
        {g.label && <p className="text-xs text-p-text-light truncate font-mono">{g.endpoint_url}</p>}
        <div className="flex items-center gap-3 mt-1">
          {LOCAL_ENGINES.map((e) => (
            <label
              key={e.id}
              className="flex items-center gap-1 text-xs text-p-text-light cursor-pointer"
              title={`Make this endpoint available on ${e.label}`}
            >
              <input
                type="checkbox"
                aria-label={e.label}
                checked={g.engines[e.id]?.status === 'active'}
                disabled={!manageable || setEngine.isPending}
                onChange={(ev) => setEngine.mutate({ group: g.group, layer: e.id, enabled: ev.target.checked })}
              />
              {e.label}
            </label>
          ))}
          {!manageable && <span className="text-xs text-p-text-light">Shared by another admin</span>}
        </div>
        {setEngine.isError && <p className="text-xs text-red-500">{(setEngine.error as Error).message}</p>}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={() => canDiscover && here && onDiscover({ id: here.id, provider: g.provider, layers: enabledEngines(g) })}
          disabled={!canDiscover}
          className="text-xs text-brand hover:text-brand-hover transition-colors disabled:opacity-40 sm:opacity-0 sm:group-hover:opacity-100"
          title={canDiscover ? 'Discover the models this server offers' : 'Enable the endpoint for this engine first'}
        >
          Discover
        </button>
        {manageable && (
          <button
            onClick={async () => {
              // The server judges "in use" on its live bindings (the counter
              // shown here can read stale) and answers 409 only for sessions
              // really running — the admin may still remove it and let them move.
              if (!confirm('Remove this local endpoint from every engine?')) return
              try {
                await del.mutateAsync({ group: g.group })
              } catch (e) {
                const err = e as Error & { status?: number }
                if (err.status !== 409) return
                if (confirm(`${err.message}\n\nRemove it anyway? Its running sessions move to another subscription, or stop until one is connected.`)) {
                  del.mutate({ group: g.group, force: true })
                }
              }
            }}
            className="text-xs text-red-500 hover:text-red-600 transition-colors sm:opacity-0 sm:group-hover:opacity-100"
          >
            Remove
          </button>
        )}
      </div>
    </div>
  )
}

export function AddLocalEndpointForm({ currentLayer, onDone }: { currentLayer: string; onDone: () => void }) {
  const [provider, setProvider] = useState<string>(LOCAL_PROVIDERS[1].id)
  const [label, setLabel] = useState('')
  const [url, setUrl] = useState<string>(LOCAL_PROVIDERS[1].url)
  const [apiKey, setApiKey] = useState('')
  // Both engines on by default (operator decision): the admin unticks what
  // it should not serve.
  const [layers, setLayers] = useState<string[]>(LOCAL_ENGINES.map((e) => e.id))
  const addMut = useAddLocalEndpoint()

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
      { onSuccess: () => { setLabel(''); setApiKey(''); onDone() } },
    )
  }

  return (
    <div className="mt-3 p-3 bg-p-bg rounded-lg border border-p-border-light space-y-2" data-current-layer={currentLayer}>
      <select value={provider} onChange={(e) => pickProvider(e.target.value)} className={INPUT} aria-label="Provider">
        {LOCAL_PROVIDERS.map((p) => (
          <option key={p.id} value={p.id}>{p.label}</option>
        ))}
      </select>
      <input type="text" placeholder="Label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} className={INPUT} />
      <input type="url" placeholder="Endpoint URL" aria-label="Endpoint URL" value={url} onChange={(e) => setUrl(e.target.value)} className={`${INPUT} font-mono`} />
      <input
        type="password"
        placeholder="API key (only if the server requires one)"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        className={`${INPUT} font-mono`}
      />
      <div className="flex items-center gap-3 text-xs text-p-text-light">
        <span>Use with:</span>
        {LOCAL_ENGINES.map((e) => (
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
      <p className="text-[11px] text-p-text-secondary leading-snug">
        On a containerized (Docker) install, use the host's LAN IP or
        <span className="font-mono"> host.docker.internal</span> — not
        <span className="font-mono"> localhost</span> (that resolves inside the
        proxy container, not your machine).
      </p>
      <div className="flex gap-2">
        <button
          onClick={handleSubmit}
          disabled={!url.trim() || layers.length === 0 || addMut.isPending}
          className="px-3 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
        >
          {addMut.isPending ? 'Adding...' : 'Add'}
        </button>
        <button onClick={onDone} className="px-3 py-1.5 text-sm rounded-lg text-p-text-secondary hover:bg-p-bg-hover transition-colors">
          Cancel
        </button>
      </div>
      {addMut.isError && <p className="text-xs text-red-500">{(addMut.error as Error).message}</p>}
    </div>
  )
}
