/**
 * Agent Settings → Skills tab.
 *
 * Two sections. "Installed skills" lists standalone skill packages — the
 * skills a manager actively chose — with an enable toggle per skill
 * (autosave per toggle, one PATCH in flight — the AgentMcps pattern;
 * enabling a skill from an unassigned package auto-assigns it server-side).
 * "From this agent's MCPs" lists the skills bundled inside assigned MCPs,
 * collapsed by default: bundled skills ride their MCP's enablement and are
 * not individually toggleable here. Context exclusions are declared by the
 * skill's author in its manifest and shown as a passive hint, not edited
 * here.
 */

import { useState, useEffect, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { useAgentSkills, useSetAgentSkill, AgentSkill } from '../../api/mcps'
import CommunitySkillsBrowser from '../../components/CommunitySkillsBrowser'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'

interface SkillPatch {
  enabled: boolean
  exclude_from: string[]
}

/** Loading-mode pill, shared by both sections. */
function LoadingBadge({ loading }: { loading: string }) {
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded-sm ${
        loading === 'always'
          ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400'
          : 'bg-gray-100 dark:bg-gray-800 text-p-text-light'
      }`}
      title={loading === 'always'
        ? 'Full skill body is inlined into the system prompt'
        : 'Loaded on demand when the task matches the skill description'}
    >
      {loading === 'always' ? 'always in context' : 'on demand'}
    </span>
  )
}

/** Author-declared context exclusions, shown as information — not editable. */
function ExclusionHint({ contexts }: { contexts: string[] }) {
  if (contexts.length === 0) return null
  return (
    <p className="text-[10px] text-p-text-light mt-1">
      Not loaded in: {contexts.join(', ')} sessions
    </p>
  )
}

export default function AgentSkills() {
  const { name } = useParams<{ name: string }>()
  const { data: skills, isLoading, refetch } = useAgentSkills(name!)
  const setSkill = useSetAgentSkill()
  const { user } = useAuth()
  const canManage = canManageAgent(user, name!)

  // `local` mirrors the in-flight enabled state the manager is editing
  // (standalone skills only — bundled skills have no toggle).
  const [local, setLocal] = useState<Record<string, SkillPatch>>({})
  const [saved, setSaved] = useState(false)
  const [showBrowse, setShowBrowse] = useState(false)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [mcpOpen, setMcpOpen] = useState(false)

  // Autosave bookkeeping: one PATCH in flight at a time — rapid clicks queue
  // the LATEST patch per skill so an out-of-order older write can't regress
  // a newer one (the AgentMcps saveSet pattern, keyed per skill).
  const busyRef = useRef(false)
  const queuedRef = useRef(new Map<string, SkillPatch>())

  useEffect(() => {
    // Don't clobber a just-clicked toggle while its autosave is in flight;
    // the post-save refetch lands with the write settled and syncs cleanly.
    if (skills && !busyRef.current && queuedRef.current.size === 0) {
      const next: Record<string, SkillPatch> = {}
      for (const s of skills) {
        next[s.id] = { enabled: s.enabled, exclude_from: s.exclude_from }
      }
      setLocal(next)
    }
  }, [skills])

  if (isLoading || !skills) {
    return <div className="text-sm text-p-text-light">Loading...</div>
  }

  const savePatch = (skillId: string, patch: SkillPatch) => {
    if (busyRef.current) { queuedRef.current.set(skillId, patch); return }
    busyRef.current = true
    setSkill.mutate(
      { agent: name!, skillId, enabled: patch.enabled, exclude_from: patch.exclude_from },
      {
        onSuccess: () => {
          setSaved(true)
          setTimeout(() => setSaved(false), 2000)
        },
        onError: err => {
          setError((err as Error)?.message || 'Failed to save skill')
          setTimeout(() => setError(null), 5000)
          // Resync the toggles to the server's truth on any failure.
          refetch()
        },
        onSettled: () => {
          busyRef.current = false
          const next = queuedRef.current.entries().next()
          if (!next.done) {
            const [qid, qpatch] = next.value
            queuedRef.current.delete(qid)
            savePatch(qid, qpatch)
          }
        },
      },
    )
  }

  const handleToggle = (skill: AgentSkill) => {
    const cur = local[skill.id] ?? { enabled: skill.enabled, exclude_from: skill.exclude_from }
    // Exclusions are author-declared; the toggle only changes `enabled`.
    const patch = { ...cur, enabled: !cur.enabled }
    setLocal(prev => ({ ...prev, [skill.id]: patch }))
    savePatch(skill.id, patch)
  }

  const q = query.trim().toLowerCase()
  const matches = (s: AgentSkill) =>
    !q ||
    s.id.toLowerCase().includes(q) ||
    (s.description || '').toLowerCase().includes(q) ||
    (s.mcp_label || '').toLowerCase().includes(q)

  const standalone = skills.filter(s => s.standalone && matches(s))
  const bundled = skills.filter(s => !s.standalone && matches(s))
  const bundledTotal = skills.filter(s => !s.standalone).length
  // A search must be able to surface bundled rows even while collapsed.
  const mcpExpanded = mcpOpen || (q !== '' && bundled.length > 0)

  return (
    <div>
      {!canManage && (
        <div className="mb-6 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-xl px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <strong>Read-only.</strong> Skill toggles are owner-only.
        </div>
      )}

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center mb-4">
        <div className="flex items-center gap-3">
          <h2 className="text-base font-bold text-p-text">Skills</h2>
          {saved && <span className="text-xs text-green-600">Saved</span>}
        </div>
        <div className="flex items-center gap-2 sm:ml-auto sm:flex-1 sm:max-w-md">
          <div className="relative flex-1 min-w-0">
            <svg className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-p-text-light pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
            </svg>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search skills…"
              className="w-full pl-8 pr-7 py-1.5 text-sm rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
            />
            {query && (
              <button
                onClick={() => setQuery('')}
                aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center rounded-sm text-p-text-light hover:text-p-text hover:bg-p-surface-hover"
              >
                ×
              </button>
            )}
          </div>
          <button
            onClick={() => setShowBrowse(true)}
            className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text hover:bg-p-surface-hover transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
            </svg>
            Browse<span className="hidden sm:inline"> community skills</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 px-3 py-2 rounded-lg border border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-900/20 text-xs text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <p className="text-xs text-p-text-light mb-4">
        Skills teach the agent how to use its tools well. "Always in context"
        skills sit in the system prompt; "on demand" skills load only when the
        task matches their description.
      </p>

      {/* ── Installed skills — standalone packages the manager controls ── */}
      <div className="mb-6">
        <h3 className="text-sm font-semibold text-p-text mb-2">Installed skills</h3>
        {standalone.length === 0 ? (
          <p className="text-xs text-p-text-light py-2">
            {q
              ? 'No installed skills match your search.'
              : 'No standalone skills installed yet — browse the community catalog to add some.'}
          </p>
        ) : (
          <div className="space-y-2">
            {standalone.map(skill => {
              const state = local[skill.id] ?? { enabled: skill.enabled, exclude_from: skill.exclude_from }
              return (
                <div
                  key={skill.id}
                  className="px-3 py-2.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface hover:bg-p-surface-hover/50 transition-colors"
                >
                  <label className="flex items-start gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={state.enabled}
                      disabled={!canManage}
                      onChange={() => handleToggle(skill)}
                      className="mt-0.5 w-4 h-4 rounded-sm border-gray-300 text-brand focus:ring-brand accent-brand"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center flex-wrap gap-1.5">
                        <span className="text-sm font-mono font-semibold text-p-text">{skill.id}</span>
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded-sm bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400"
                          title={`Standalone skill package: ${skill.mcp_label}`}
                        >
                          {skill.mcp_label}
                        </span>
                        <LoadingBadge loading={skill.loading} />
                      </div>
                      {skill.description && (
                        <p className="text-xs text-p-text-light mt-0.5">{skill.description}</p>
                      )}
                      <ExclusionHint contexts={state.exclude_from} />
                    </div>
                  </label>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ── Bundled skills — ride their MCP's enablement, no toggles.
             Expandable pill card (the User Settings → AI Engines pattern);
             rows render full-width inside it, never indented. ── */}
      {bundledTotal > 0 && (
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
          <button
            type="button"
            onClick={() => setMcpOpen(open => !open)}
            aria-expanded={mcpExpanded}
            className="w-full flex items-center justify-between gap-3 p-4 text-left"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="text-sm font-semibold text-p-text">From this agent's MCPs</h3>
                <span className="text-xs px-2 py-0.5 rounded-lg bg-p-bg dark:bg-gray-800 text-p-text-secondary font-medium">
                  {bundledTotal}
                </span>
              </div>
              <p className="text-xs text-p-text-light mt-0.5">
                Bundled skills are part of their MCP — active whenever the MCP
                is enabled for this agent.
              </p>
            </div>
            <svg
              className={`w-4 h-4 shrink-0 text-p-text-light transition-transform ${mcpExpanded ? 'rotate-180' : ''}`}
              fill="none" stroke="currentColor" viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {mcpExpanded && (
            <div className="px-3 pb-3 sm:px-4 sm:pb-4 space-y-2">
              {bundled.map(skill => (
                <div
                  key={skill.id}
                  className="px-3 py-2.5 rounded-lg border border-p-border-light bg-p-bg dark:bg-p-surface-hover/40"
                >
                  <div className="flex items-center flex-wrap gap-1.5">
                    <span className="text-sm font-mono font-semibold text-p-text">{skill.id}</span>
                    <span className="text-xs text-p-text-light">from {skill.mcp_label}</span>
                    <LoadingBadge loading={skill.loading} />
                  </div>
                  {skill.description && (
                    <p className="text-xs text-p-text-light mt-0.5">{skill.description}</p>
                  )}
                  <ExclusionHint contexts={skill.exclude_from} />
                </div>
              ))}
              {bundled.length === 0 && (
                <p className="text-xs text-p-text-light py-2">No bundled skills match your search.</p>
              )}
            </div>
          )}
        </div>
      )}

      {skills.length === 0 && (
        <p className="text-sm text-p-text-light py-4">
          No skills available for this agent yet. Assign MCPs that bundle
          skills, or browse community skills.
        </p>
      )}

      <CommunitySkillsBrowser
        open={showBrowse}
        onClose={() => setShowBrowse(false)}
        agentSlug={name}
      />
    </div>
  )
}
