import { useState, useEffect, useLayoutEffect, useRef, type JSX } from 'react'
import {
  ASK_MODE, AUTO_REVIEW_MODE, DONT_ASK_CHIP, DONT_ASK_MODE, PLAN_MODE, currentMode, modeOffered,
} from '../../lib/engines/modes'
import IconDropdown, { type IconDropdownGroup } from '../ui/IconDropdown'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, POPOVER } from '../ui/classes'
import { useDialogs } from '../ui/dialogs'
import { emitAppToast } from '../../lib/appToasts'

export interface ActiveAgent {
  id: string
  description: string
  type: string
  startTime: number
  background?: boolean
}

interface CacheStats {
  cacheRead: number
  cacheWrite: number
  inputTokens: number
  outputTokens: number
}

interface Props {
  streaming: boolean
  /** Session is spawning (MCP install / daemon start) — shows a "Getting ready…"
   * badge until warmup_ready. Distinct from streaming/thinking. */
  warming?: boolean
  startTime: number | null
  thinkingActive: boolean
  compressingActive: boolean
  activeAgents: ActiveAgent[]
  mode: string
  model: string
  modelValue?: string  // compound value for dropdown matching (layer::model_id)
  costUsd: number
  /** false = the chat's newest turn ran on a subscription or a local model:
   * the cost popup adds its subscription note. Undefined is the same as true. */
  costBilled?: boolean
  /** The tool fees' share of costUsd (money whatever the model runs on). */
  toolCostUsd?: number
  contextUsed: number
  contextMax: number
  cacheStats?: CacheStats
  meetingActive?: boolean
  /** The permission modes the active engine declares (`permission_modes` on
   * its descriptor — Codex and Direct LLM declare no Plan). Undefined (the
   * catalog has not loaded) offers every mode this bar knows. */
  permissionModes?: string[]
  modelOptions?: { value: string; label: string }[]
  modelGroups?: ModelGroup[]
  /** Interactive CLI toggle. When `interactiveAvailable`
   * the Model popup shows an on/off switch at the top; ON runs the chat as the
   * native TUI under a PTY, OFF runs headless `-p`. Hidden entirely for agents
   * without a claude-code-cli layer. `interactiveDisabled` locks it while a
   * session is live. */
  interactiveAvailable?: boolean
  interactiveOn?: boolean
  interactiveDisabled?: boolean
  onInteractiveToggle?: (next: boolean) => void
  /** View-toggle: while a live interactive terminal
   * exists (`richViewAvailable`), a button flips the view between the terminal
   * and the DB rich conversation history WITHOUT killing the session.
   * `richViewActive` is true while the rich history is shown. Hidden entirely
   * when no live terminal exists (the normal message list is already shown). */
  richViewAvailable?: boolean
  richViewActive?: boolean
  onToggleRichView?: () => void
  /** Interactive mode manages permissions inside the TUI (Shift+Tab) on top of
   * the platform's always-on baseline + path enforcement, so the dashboard
   * permission-mode dropdown is hidden (it can't drive the live TUI). */
  hidePermissions?: boolean
  /** In interactive mode the turn metrics (live timer, context gauge, cost) come
   * from the `-p` pump, which doesn't run — the TUI owns its own status line. Any
   * leftover values (e.g. a prior `-p` turn persisted to this chat before it was
   * switched to interactive) would render stale badges, so hide all three. The
   * "Getting ready…" warmup badge is unaffected (it still shows during spawn). */
  interactiveActive?: boolean
  /** A live interactive PTY's model can't be changed from here (use /model in
   * the TUI), so the Model dropdown shows only the active model read-only.
   * The popup still opens for the interactive switch. Task-run chats are
   * NOT locked any more (1.5): their picker follows the standard rule —
   * the active engine's models while the run/session is alive, the
   * cross-engine expansion + confirm once dead (follow-up turns resolve
   * from the chat row; the backend gates in-flight runs + the continue
   * tier authoritatively). */
  modelLocked?: boolean
  /** Task-run chats: the permission chip shows the RUN's stored mode (the
   * scheduler's 'auto' → Don't Ask) read-only — the popup lists only the
   * active mode and selection is a no-op. */
  modeLocked?: boolean
  /** Why the pickers are locked on a chat that runs as the agent (the
   * server's drive-gate sentence): shown on the mode and model triggers
   * after their label and on the terminal switch. */
  lockReason?: string
  /** Optional content rendered on the LEFT of the status row, filling the space
   * that is otherwise the flex spacer (so the model control stays right-aligned).
   * Used by the interactive terminal control-key bar —
   * it scrolls horizontally within this slot, ~20px before the model control. */
  leftSlot?: JSX.Element
  /** Optional icon pill rendered before the view toggle on a live terminal
   * (then the model button) and before the mode icon headless: the chat's
   * "Runs on" control, built by the page. */
  targetSlot?: JSX.Element
  onModeChange: (mode: string) => void
  onModelChange: (model: string) => void
  onCompactContext?: () => void
  /** Fired when the Model dropdown opens — the chat page uses it to lazily
   * re-probe session liveness (headless idle-reap emits no frame, so the
   * cross-engine options would otherwise stay hidden on a chat that died
   * while being viewed). */
  onModelMenuOpen?: () => void
}

// --- Permission mode config ---
// Keyed in menu order (Ask, Auto, Don't Ask, Plan): the dropdown lists them so.
const MODE_CONFIG: Record<string, { label: string; bg: string; text: string; border: string; icon: JSX.Element }> = {
  [ASK_MODE]: {
    label: 'Ask',
    bg: 'bg-surface-muted', text: 'text-secondary', border: 'border-subtle',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" /></svg>,
  },
  [AUTO_REVIEW_MODE]: {
    label: 'Auto',
    bg: 'bg-action-subtle', text: 'text-link', border: 'border-action-subtle-hover',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" /></svg>,
  },
  [DONT_ASK_MODE]: {
    label: "Don't Ask",
    ...DONT_ASK_CHIP,
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>,
  },
  [PLAN_MODE]: {
    label: 'Plan',
    bg: 'bg-accent-agent-bg', text: 'text-accent-agent-fg', border: 'border-accent-agent-border',
    icon: <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" /></svg>,
  },
}

const MODE_OPTIONS = Object.entries(MODE_CONFIG).map(([value, c]) => ({ value, label: c.label }))

/** The model chip's letter: the initial of the model's display name (the
 *  descriptor's row — "Fable 5.1" → F, "Opus 5" → O), else of its id (a chat
 *  pinned to a retired id the catalog no longer serves). */
function getModelLetter(model: string, label?: string): string {
  const source = (label || model).trim()
  return (source[0] || '?').toUpperCase()
}

// Default fallback options (used when modelOptions prop not provided)
const DEFAULT_MODEL_OPTIONS = [
  { value: 'claude-fable-5-1', label: 'Fable 5.1 (1M)' },
  { value: 'claude-opus-5-5', label: 'Opus 5.5 (1M)' },
  { value: 'claude-sonnet-5-5', label: 'Sonnet 5.5 (1M)' },
  { value: 'claude-haiku-5-5', label: 'Haiku 5.5 (1M)' },
]

// The mode and model pickers share the icon dropdown in components/ui.
type ModelGroup = IconDropdownGroup

// Interactive-terminal on/off switch — rendered at the top of the Model popup.
// A row: label + sliding switch. Disabled while a session is live;
// switching a running session involves a kill+rewarm.
function InteractiveToggle({ on, disabled, disabledReason, onToggle }: {
  on: boolean
  disabled: boolean
  disabledReason?: string
  onToggle?: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => { if (!disabled) onToggle?.(!on) }}
      title={disabled
        ? disabledReason || 'Switching…'
        : on
          ? 'Interactive terminal is on. Turn it off to go back to normal mode.'
          : 'Run this chat in an interactive terminal'}
      className={`w-full flex items-center justify-between gap-2 px-3 py-2 border-b border-subtle text-left transition-colors ${
        disabled ? 'opacity-50 cursor-not-allowed' : 'hover:bg-surface-hover'
      }`}
    >
      <span className="flex flex-col min-w-0">
        <span className="text-xs font-medium text-primary">Interactive terminal</span>
        <span className="text-3xs text-tertiary leading-tight truncate">The agent runs live in a terminal</span>
      </span>
      <span className={`relative inline-block w-9 h-5 rounded-full shrink-0 transition-colors ${on ? 'bg-action' : 'bg-control-track'}`}>
        <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-action-fg shadow-elevation-1 transition-transform ${on ? 'translate-x-4' : ''}`} />
      </span>
    </button>
  )
}

export default function ChatStatusBar({
  streaming,
  warming,
  startTime,
  thinkingActive,
  compressingActive,
  activeAgents,
  mode,
  model,
  costUsd,
  costBilled,
  toolCostUsd,
  contextUsed,
  contextMax,
  cacheStats,
  meetingActive,
  permissionModes: permissionModesProp,
  modelOptions: modelOptionsProp,
  modelGroups,
  modelValue: modelValueProp,
  interactiveAvailable,
  interactiveOn,
  interactiveDisabled,
  onInteractiveToggle,
  richViewAvailable,
  richViewActive,
  onToggleRichView,
  hidePermissions,
  interactiveActive,
  modelLocked,
  modeLocked,
  lockReason,
  leftSlot,
  targetSlot,
  onModeChange,
  onModelChange,
  onCompactContext,
  onModelMenuOpen,
}: Props) {
  const dialogs = useDialogs()
  const liveRef = useRef({ streaming, meetingActive })
  useLayoutEffect(() => { liveRef.current = { streaming, meetingActive } })
  const resolvedModelOptions = modelOptionsProp && modelOptionsProp.length > 0
    ? modelOptionsProp.filter(m => m.value !== '')  // exclude "System Default" from runtime dropdown
    : DEFAULT_MODEL_OPTIONS
  // The timer's tick, keyed by the turn it counts: a new turn reads 0 until
  // its own first tick, an ended turn reads 0 at once.
  const [tick, setTick] = useState<{ start: number | null; s: number }>({ start: null, s: 0 })
  const elapsed = streaming && startTime && tick.start === startTime ? tick.s : 0
  const [contextPopupOpen, setContextPopupOpen] = useState(false)
  const contextRef = useRef<HTMLDivElement>(null)
  const [costPopupOpen, setCostPopupOpen] = useState(false)
  const costRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!streaming || !startTime) return
    const interval = setInterval(() => {
      setTick({ start: startTime, s: (Date.now() - startTime) / 1000 })
    }, 100)
    return () => clearInterval(interval)
  }, [streaming, startTime])

  // Close the context and cost popups on an outside click
  useEffect(() => {
    if (!contextPopupOpen && !costPopupOpen) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      if (contextRef.current && !contextRef.current.contains(t)) setContextPopupOpen(false)
      if (costRef.current && !costRef.current.contains(t)) setCostPopupOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [contextPopupOpen, costPopupOpen])

  const fgAgents = activeAgents.filter(a => !a.background && a.type !== 'delegate' && a.type !== 'bgcommand')
  const bgAgents = activeAgents.filter(a => a.background && a.type !== 'bgcommand')
  const bgCommands = activeAgents.filter(a => a.type === 'bgcommand')
  const delegatedAgents = activeAgents.filter(a => a.type === 'delegate')
  const hasActiveAgents = fgAgents.length > 0 || bgAgents.length > 0 || bgCommands.length > 0 || delegatedAgents.length > 0

  return (
    <div className="px-3 py-1.5 text-xs select-none">
      {/* Agent + meeting badges — own row on mobile, inline on desktop */}
      {(hasActiveAgents || meetingActive) && (
        <div className="flex items-center gap-2 mb-1.5 justify-end md:hidden">
          <AgentBadge agents={fgAgents} label="Foreground" {...AGENT_WORK} />
          <AgentBadge agents={bgAgents} label="Background" {...AGENT_WORK} />
          <AgentBadge agents={bgCommands} label="Commands" {...COMMANDS} />
          <AgentBadge agents={delegatedAgents} label="Delegated" {...AGENT_WORK} />
          {meetingActive && (
            <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-agent-bg border border-accent-agent-border text-accent-agent-fg whitespace-nowrap shrink-0">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
              Meeting
            </span>
          )}
        </div>
      )}

      {/* Main status row */}
      <div className="flex items-center gap-2">
        {/* Timer badge — hidden during warmup (the "Getting ready…" badge shows
            instead) and in interactive mode (no -p turn → no meaningful timer). */}
        {streaming && startTime && !warming && !interactiveActive && (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-surface-muted text-secondary tabular-nums font-mono whitespace-nowrap shrink-0">
            <svg className="w-3 h-3 text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {elapsed < 60 ? `${elapsed.toFixed(0)}s` : `${Math.floor(elapsed / 60)}m ${Math.floor(elapsed % 60)}s`}
          </span>
        )}

        {/* Context gauge / Thinking / Compressing — swap on mobile to save space.
            Priority: compressing > thinking > context gauge. */}
        {warming ? (
          <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-surface-muted text-secondary whitespace-nowrap shrink-0">
            <span className="inline-block w-2 h-2 border-[1.5px] border-status-info-solid border-t-transparent rounded-full animate-spin" />
            Getting ready…
          </span>
        ) : compressingActive ? (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-status-info-bg border border-status-info-border text-status-info-fg whitespace-nowrap shrink-0">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-status-info-solid animate-pulse" />
            Compressing
          </span>
        ) : thinkingActive ? (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-agent-bg border border-accent-agent-border text-accent-agent-fg whitespace-nowrap shrink-0">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
            Thinking
          </span>
        ) : contextMax > 0 && !interactiveActive && (() => {
          const pct = Math.round((contextUsed / contextMax) * 100)
          const fmtK = (n: number) => n >= 1000000 ? `${(n / 1000000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(0)}K` : String(n)
          const usedK = fmtK(contextUsed)
          const maxK = fmtK(contextMax)
          const bgColor = pct >= 90 ? 'bg-status-danger-bg border border-status-danger-border'
            : pct >= 80 ? 'bg-status-warning-bg border border-status-warning-border'
            : 'bg-surface-muted border border-transparent'
          const textColor = pct >= 90 ? 'text-status-danger-fg'
            : pct >= 80 ? 'text-status-warning-fg'
            : 'text-secondary'
          return (
            <div className="relative shrink-0" ref={contextRef}>
              <button
                onClick={() => setContextPopupOpen(!contextPopupOpen)}
                className={`flex items-center gap-1 px-2 py-0.5 rounded-full ${bgColor} ${textColor} tabular-nums font-mono whitespace-nowrap cursor-pointer`}
              >
                <svg className="w-3 h-3" viewBox="0 0 16 16">
                  <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" opacity="0.2" />
                  <circle
                    cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2"
                    strokeDasharray={`${(pct / 100) * 37.7} 37.7`}
                    strokeLinecap="round"
                    transform="rotate(-90 8 8)"
                  />
                </svg>
                {pct}%
              </button>
              {contextPopupOpen && (
                <div className={`absolute bottom-full mb-1 left-0 md:left-1/2 md:-translate-x-1/2 w-52 ${POPOVER} py-2 px-3 z-50`}>
                  <p className="text-3xs font-semibold text-tertiary uppercase tracking-wider mb-1.5">Context Usage</p>
                  {/* Progress bar */}
                  <div className="w-full h-1.5 rounded-full bg-surface-muted mb-2">
                    <div
                      className={`h-full rounded-full transition-all ${pct >= 90 ? 'bg-status-danger-solid' : pct >= 80 ? 'bg-status-warning-solid' : 'bg-status-info-solid'}`}
                      style={{ width: `${Math.min(pct, 100)}%` }}
                    />
                  </div>
                  <div className="flex justify-between text-2xs text-secondary mb-2">
                    <span className="font-mono">{usedK} / {maxK}</span>
                    <span className={`font-semibold ${textColor}`}>{pct}%</span>
                  </div>
                  {cacheStats && (cacheStats.cacheRead > 0 || cacheStats.cacheWrite > 0 || cacheStats.outputTokens > 0) && (
                    <>
                      <div className="border-t border-subtle pt-1.5 mt-1">
                        <p className="text-3xs font-semibold text-tertiary uppercase tracking-wider mb-1">Last Turn</p>
                      </div>
                      <div className="space-y-0.5 text-2xs text-secondary">
                        {cacheStats.cacheRead > 0 && (
                          <div className="flex justify-between">
                            <span>Cache Read</span>
                            <span className="font-mono">{fmtK(cacheStats.cacheRead)}</span>
                          </div>
                        )}
                        {cacheStats.cacheWrite > 0 && (
                          <div className="flex justify-between">
                            <span>Cache Write</span>
                            <span className="font-mono">{fmtK(cacheStats.cacheWrite)}</span>
                          </div>
                        )}
                        {cacheStats.outputTokens > 0 && (
                          <div className="flex justify-between">
                            <span>Output</span>
                            <span className="font-mono">{fmtK(cacheStats.outputTokens)}</span>
                          </div>
                        )}
                      </div>
                    </>
                  )}
                  {onCompactContext && pct > 0 && !meetingActive && (
                    <div className="border-t border-subtle pt-1.5 mt-1.5">
                      <button
                        onClick={async () => {
                          if (!(await dialogs.confirm({
                            title: 'Compress the context?',
                            body: 'This summarizes the conversation to free up the context window.',
                            confirmLabel: 'Compress',
                          }))) return
                          setContextPopupOpen(false)
                          // A reply or a meeting may have started while the dialog was open.
                          if (liveRef.current.streaming || liveRef.current.meetingActive) {
                            emitAppToast({ severity: 'info', title: 'The context was not compressed', body: 'A reply is running. Try again when it ends.' })
                            return
                          }
                          onCompactContext()
                        }}
                        disabled={streaming}
                        className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs} w-full`}
                      >
                        Compress Context
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })()}

        {/* Meeting badge — desktop only (mobile shows in row above) */}
        {meetingActive && (
          <span className="hidden md:flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-agent-bg border border-accent-agent-border text-accent-agent-fg whitespace-nowrap shrink-0">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
            Meeting
          </span>
        )}

        {/* Agent badges — inline on desktop, hidden on mobile (shown in row above) */}
        <div className="hidden md:contents">
          <AgentBadge agents={fgAgents} label="Foreground" {...AGENT_WORK} />
          <AgentBadge agents={bgAgents} label="Background" {...AGENT_WORK} />
          <AgentBadge agents={bgCommands} label="Commands" {...COMMANDS} />
          <AgentBadge agents={delegatedAgents} label="Delegated" {...AGENT_WORK} />
        </div>

        {/* Left slot (interactive control-key bar) fills the spacer so the model
            control stays right-aligned; scrolls horizontally within itself. */}
        {leftSlot
          ? <div className="flex-1 min-w-0 mr-3">{leftSlot}</div>
          : <div className="flex-1" />}

        {/* Cost: the money this chat has cost, an API key's or the relay's
            turns and every tool fee, never a subscription's or a local
            model's activity estimate (costUsd is already that sum). A round
            icon that opens the amount, so a long figure never crowds the
            row. Hidden in interactive mode (cost accrues inside the TUI, not
            via the -p pump; a leftover value would be stale). */}
        {costUsd > 0 && !interactiveActive && (() => {
          const tools = toolCostUsd ?? 0
          const modelCost = Math.max(0, costUsd - tools)
          const label = `$${costUsd.toFixed(2)}`
          return (
            <div className="relative shrink-0" ref={costRef}>
              <button
                type="button"
                onClick={() => setCostPopupOpen(!costPopupOpen)}
                aria-expanded={costPopupOpen}
                aria-label={`Cost ${label}`}
                title={`Cost ${label}`}
                className="flex items-center justify-center w-7 h-7 rounded-full bg-surface-muted text-secondary bg-linear-to-b from-transparent to-transparent hover:from-surface-hover hover:to-surface-hover transition-colors cursor-pointer"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </button>
              {costPopupOpen && (
                <div className={`absolute bottom-full mb-1 right-0 md:right-auto md:left-1/2 md:-translate-x-1/2 w-48 ${POPOVER} py-2 px-3 z-50`}>
                  <p className="text-3xs font-semibold text-tertiary uppercase tracking-wider mb-1">Cost</p>
                  <p className="text-base font-semibold text-primary tabular-nums font-mono">{label}</p>
                  {tools > 0 && (
                    <div className="mt-1.5 space-y-0.5 text-2xs text-secondary">
                      {modelCost > 0 && (
                        <div className="flex justify-between"><span>Model</span><span className="font-mono">${modelCost.toFixed(2)}</span></div>
                      )}
                      <div className="flex justify-between"><span>Tools</span><span className="font-mono">${tools.toFixed(2)}</span></div>
                    </div>
                  )}
                  {costBilled === false && (
                    <p className="mt-1.5 text-2xs text-tertiary">The model runs on a subscription. Only tool fees are billed.</p>
                  )}
                </div>
              )}
            </div>
          )
        })()}

        {/* The chat's "Runs on" control, when the page provides one: before
            the view toggle on a live terminal, before the mode icon headless. */}
        {targetSlot}

        {/* View toggle — flip the live terminal ⇄ the DB
            rich conversation history WITHOUT killing the session. Shown only
            while a terminal is live (richViewAvailable); the session keeps
            running (the terminal stays mounted, hidden) in either view. */}
        {richViewAvailable && (
          <button
            type="button"
            onClick={() => onToggleRichView?.()}
            aria-pressed={!!richViewActive}
            title={richViewActive ? 'Back to the live terminal' : 'Show the conversation history'}
            className={`flex items-center justify-center w-7 h-7 rounded-control border transition-colors cursor-pointer ${
              richViewActive
                ? 'bg-action-subtle border-action-subtle-hover text-link'
                : 'bg-surface-muted border-subtle text-secondary hover:text-primary'
            }`}
          >
            {richViewActive ? (
              // Terminal glyph — click returns to the live terminal.
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M4 5h16a1 1 0 011 1v12a1 1 0 01-1 1H4a1 1 0 01-1-1V6a1 1 0 011-1z" />
              </svg>
            ) : (
              // Chat-bubble glyph (speech bubble + text lines) — click shows
              // the rich conversation history.
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M4.6 5h14.8A1.6 1.6 0 0121 6.6v8.3a1.6 1.6 0 01-1.6 1.6h-6.9L8 20v-3.5H4.6A1.6 1.6 0 013 14.9V6.6A1.6 1.6 0 014.6 5z" />
                <path strokeLinecap="round" strokeWidth={2} d="M7.5 9.2h9M7.5 12.4h5.5" />
              </svg>
            )}
          </button>
        )}

        {/* Mode icon dropdown — hidden in interactive mode (the TUI owns its
            permission mode; our baseline + path enforcement still apply). */}
        {!hidePermissions && (() => {
          const mc = MODE_CONFIG[currentMode(mode)] || MODE_CONFIG[ASK_MODE]
          const offered = permissionModesProp ?? Object.keys(MODE_CONFIG)
          const modeOptions = modeLocked
            ? [{ value: mode, label: mc.label }]
            : MODE_OPTIONS.filter(o => modeOffered(offered, o.value, { meeting: meetingActive }))
          return (
            <IconDropdown
              label="Permissions"
              value={mode}
              options={modeOptions}
              onChange={modeLocked ? () => {} : onModeChange}
              trigger={
                <span
                  className={`flex items-center justify-center w-7 h-7 rounded-control border ${mc.bg} ${mc.border} ${mc.text} hover:brightness-95 transition-colors cursor-pointer`}
                  title={`Mode: ${mc.label}${lockReason ? ` · ${lockReason}` : ''}`}
                >
                  {mc.icon}
                </span>
              }
            />
          )
        })()}

        {/* Model icon dropdown */}
        {(() => {
          const matchedModel = resolvedModelOptions.find(m => m.value === model)
          const modelLabel = matchedModel?.label || model || 'Unknown'
          // The tier word lives in the trigger's tooltip only; the rows show
          // the four-dot mark.
          const modelTier = modelGroups?.flatMap(g => g.models).find(m => m.value === (modelValueProp || model))?.tierLabel
          const modelLetter = getModelLetter(model, matchedModel?.label)
          // A live interactive PTY's model can't be changed from here (use /model
          // in the TUI), so show only the active model read-only — the popup still
          // opens for the interactive switch.
          const activeValue = modelValueProp || model
          const lockedGroups = modelLocked
            ? [{ layer: 'active', layerLabel: '', models: [{ value: activeValue, label: modelLabel }] }]
            : modelGroups
          return (
            <IconDropdown
              label="Model"
              value={activeValue}
              options={modelLocked ? undefined : (!modelGroups ? resolvedModelOptions : undefined)}
              groups={lockedGroups}
              onChange={modelLocked ? () => {} : onModelChange}
              onOpen={onModelMenuOpen}
              topSlot={interactiveAvailable
                ? <InteractiveToggle on={!!interactiveOn} disabled={!!interactiveDisabled}
                    disabledReason={lockReason} onToggle={onInteractiveToggle} />
                : undefined}
              trigger={
                <span
                  className="flex items-center justify-center w-7 h-7 rounded-control border bg-surface-muted border-subtle text-secondary hover:text-primary transition-colors cursor-pointer text-xs font-semibold"
                  title={`Model: ${modelLabel}${modelTier ? ` · ${modelTier}` : ''}${lockReason ? ` · ${lockReason}` : ''}`}
                >
                  {modelLetter}
                </span>
              }
            />
          )
        })()}
      </div>
    </div>
  )
}

// The running-work badges: subagents, background agents and delegated work
// are agent work; a background shell command is information.
const AGENT_WORK = { bgClass: 'bg-accent-agent-bg border-accent-agent-border', textClass: 'text-accent-agent-fg', spinnerClass: 'border-accent-agent-solid' }
const COMMANDS = { bgClass: 'bg-status-info-bg border-status-info-border', textClass: 'text-status-info-fg', spinnerClass: 'border-status-info-solid' }

function AgentBadge({
  agents,
  label,
  bgClass,
  textClass,
  spinnerClass,
}: {
  agents: ActiveAgent[]
  label: string
  bgClass: string
  textClass: string
  spinnerClass: string
}) {
  if (agents.length === 0) return null
  return (
    <span
      className={`flex items-center gap-1 px-2 py-0.5 rounded-full border ${bgClass} ${textClass}`}
      title={agents.map(a => a.description).join('\n')}
    >
      <span className={`inline-block w-2 h-2 border-[1.5px] ${spinnerClass} border-t-transparent rounded-full animate-spin`} />
      {agents.length} {label}
    </span>
  )
}
