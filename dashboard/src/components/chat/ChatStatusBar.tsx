import { useState, useEffect, useLayoutEffect, useRef, type JSX } from 'react'
import { createPortal } from 'react-dom'
import {
  ASK_MODE, AUTO_REVIEW_MODE, DONT_ASK_CHIP, DONT_ASK_MODE, PLAN_MODE, modeOffered,
} from '../../lib/engines/modes'
import IconDropdown, { type IconDropdownGroup } from '../ui/IconDropdown'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, POPOVER } from '../ui/classes'
import { useDialogs } from '../ui/dialogs'
import { emitAppToast } from '../../lib/appToasts'
import { BG_COMMAND_BLOCK } from '../../lib/kinds/block'
import { usePortalMenu } from '../../hooks/usePortalMenu'
import { PlaceNote } from './RunsOnChip'

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
  /** Why the mode button takes no pick now (a turn runs on the live
   * terminal, or it starts again): the button looks unavailable and a click
   * opens this note, the machine button's own busy pattern. */
  modeBusyReason?: string
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
  /** Below `md`, the slot the badges float in over the chat's bottom right
   *  corner (`ChatBottomPanel`'s), while the message list or the terminal
   *  holds the pane. Null keeps them in a row of their own above the status
   *  row. */
  badgeFloat?: HTMLElement | null
  /** The terminal holds the pane: its floated badges take the compact form,
   *  so they cover less of the engine's last line. */
  terminalShown?: boolean
  /** Optional icon pill rendered after the view toggle on a live terminal
   * and before the mode icon: the chat's "Runs on" control, built by the
   * page. */
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
 *  descriptor's row — "Fable 5.2" → F, "Opus 5" → O), else of its id (a chat
 *  pinned to a retired id the catalog no longer serves). */
function getModelLetter(model: string, label?: string): string {
  const source = (label || model).trim()
  return (source[0] || '?').toUpperCase()
}

// Default fallback options (used when modelOptions prop not provided)
const DEFAULT_MODEL_OPTIONS = [
  { value: 'claude-fable-5-2', label: 'Fable 5.2 (1M)' },
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
  modeBusyReason,
  interactiveActive,
  modelLocked,
  modeLocked,
  lockReason,
  leftSlot,
  badgeFloat,
  terminalShown,
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

  const fgAgents = activeAgents.filter(a => !a.background && a.type !== 'delegate' && a.type !== BG_COMMAND_BLOCK)
  const bgAgents = activeAgents.filter(a => a.background && a.type !== BG_COMMAND_BLOCK)
  const bgCommands = activeAgents.filter(a => a.type === BG_COMMAND_BLOCK)
  const delegatedAgents = activeAgents.filter(a => a.type === 'delegate')
  const hasActiveAgents = fgAgents.length > 0 || bgAgents.length > 0 || bgCommands.length > 0 || delegatedAgents.length > 0

  const badgeKinds: BadgeKind[] = [
    { label: 'Foreground', agents: fgAgents, tone: AGENT_WORK, glyph: null },
    { label: 'Background', agents: bgAgents, tone: AGENT_WORK, glyph: GLYPH.background },
    { label: 'Commands', agents: bgCommands, tone: COMMANDS, glyph: GLYPH.commands },
    { label: 'Delegated', agents: delegatedAgents, tone: AGENT_WORK, glyph: GLYPH.delegated },
  ]

  // The live chips inline on desktop (a phone floats them, or shows them in
  // the row above): the Meeting chip, then the running work. One block, which
  // the row places by whether it holds the terminal's keys. A chip keeps its
  // width: in a narrow window the key bar scrolls instead.
  const liveChips = (
    <div className="hidden md:contents *:shrink-0 *:whitespace-nowrap">
      {meetingActive && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent-agent-bg border border-accent-agent-border text-accent-agent-fg">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
          Meeting
        </span>
      )}
      <AgentBadge agents={fgAgents} label="Foreground" {...AGENT_WORK} />
      <AgentBadge agents={bgAgents} label="Background" {...AGENT_WORK} />
      <AgentBadge agents={bgCommands} label="Commands" {...COMMANDS} />
      <AgentBadge agents={delegatedAgents} label="Delegated" {...AGENT_WORK} />
    </div>
  )

  return (
    <div className="px-3 py-1.5 text-xs select-none">
      {/* Agent + meeting badges: on a phone over the chat's bottom right
          corner, so nothing moves when one comes or goes (a row of their
          own where the pane holds no chat), inline on desktop. */}
      {badgeFloat ? createPortal(
        <FloatBadges kinds={badgeKinds} meetingActive={!!meetingActive} terminalShown={!!terminalShown} />,
        badgeFloat,
      ) : (hasActiveAgents || meetingActive) && (
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

        {/* Left slot (interactive control-key bar) fills the spacer so the model
            control stays right-aligned. The chips follow the left group, as
            they follow the gauge with no key bar: the keys at the far left,
            the chips right after them, a little apart, then the free space.
            When the row is short the free space goes first, then the keys
            scroll within their bar: no chip and no control shrinks. The key
            area positions the bar's More keys panel, so it keeps the area's
            width. */}
        {leftSlot ? (
          <div className="relative flex-1 min-w-0 mr-3 flex items-center gap-2">
            <div className="min-w-0 md:mr-1">{leftSlot}</div>
            {liveChips}
          </div>
        ) : (
          <>
            {liveChips}
            <div className="flex-1" />
          </>
        )}

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

        {/* View toggle — flip the live terminal ⇄ the DB
            rich conversation history WITHOUT killing the session. Shown only
            while a terminal is live (richViewAvailable), first of the row, so
            the row is the same in both views; the session keeps running (the
            terminal stays mounted, hidden) in either view. */}
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

        {/* The chat's "Runs on" control, when the page provides one: after
            the view toggle, before the mode icon. */}
        {targetSlot}

        {/* Mode icon dropdown, in both views. On a live terminal a pick starts
            it again in that mode; while a turn runs there it takes no pick and
            says why (the machine button's busy pattern). */}
        {(() => {
          const mc = MODE_CONFIG[mode] || MODE_CONFIG[ASK_MODE]
          if (modeBusyReason) {
            return (
              <PlaceNote
                lines={[`Mode: ${mc.label}`, modeBusyReason]}
                testId="mode-busy"
                disabled
                className={`flex items-center justify-center w-7 h-7 shrink-0 rounded-control border ${mc.bg} ${mc.border} ${mc.text} aria-disabled:opacity-60`}
              >
                {mc.icon}
              </PlaceNote>
            )
          }
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
type BadgeTone = { bgClass: string; textClass: string; spinnerClass: string }
const AGENT_WORK: BadgeTone = { bgClass: 'bg-accent-agent-bg border-accent-agent-border', textClass: 'text-accent-agent-fg', spinnerClass: 'border-accent-agent-solid' }
const COMMANDS: BadgeTone = { bgClass: 'bg-status-info-bg border-status-info-border', textClass: 'text-status-info-fg', spinnerClass: 'border-status-info-solid' }

/** One kind of work a badge counts. `glyph` names it in the compact form
 *  (null: the working ring, Foreground's). */
interface BadgeKind {
  label: string
  agents: ActiveAgent[]
  tone: BadgeTone
  glyph: string | null
}

/** The compact form's glyphs (24-unit outline paths): the work's kind at a
 *  glance where a label does not fit. */
const GLYPH = {
  background: 'M6.429 9.75L2.25 12l4.179 2.25m0-4.5l5.571 3 5.571-3m-11.142 0L2.25 7.5 12 2.25l9.75 5.25-4.179 2.25m0 0L21.75 12l-4.179 2.25m0 0l4.179 2.25L12 21.75 2.25 16.5l4.179-2.25m11.142 0l-5.571 3-5.571-3',
  commands: 'M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z',
  delegated: 'M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25',
  meeting: 'M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 01-.825-.242m9.345-8.334a2.126 2.126 0 00-.476-.095 48.64 48.64 0 00-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0011.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155',
}

function Glyph({ path }: { path: string }) {
  return (
    <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75} d={path} />
    </svg>
  )
}

// A floated pill sits on the page colour under its own tint: its fill is
// solid over the chat and the terminal, in light and dark.
const FLOAT_PILL_BASE = 'pointer-events-auto rounded-full bg-surface-page shadow-elevation-1'

/** The badges floated over a phone's chat. Several pills, or the terminal
 *  under them, take the compact form (the kind's glyph and the count), so
 *  they stay in one row in the right half and cover less of the engine's
 *  last line; one pill over the history keeps its label. */
function FloatBadges({ kinds, meetingActive, terminalShown }: { kinds: BadgeKind[]; meetingActive: boolean; terminalShown: boolean }) {
  const shown = kinds.filter((k) => k.agents.length > 0)
  const compact = terminalShown || shown.length + (meetingActive ? 1 : 0) > 1
  return (
    <>
      {shown.map((k) => <FloatBadge key={k.label} kind={k} compact={compact} />)}
      {meetingActive && (
        <span className={FLOAT_PILL_BASE}>
          <span
            data-testid="float-badge-meeting"
            role="img"
            aria-label="Meeting"
            title="Meeting"
            className="flex items-center gap-1 px-2 py-0.5 rounded-full border bg-accent-agent-bg border-accent-agent-border text-accent-agent-fg text-xs whitespace-nowrap select-none"
          >
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
            {compact ? <Glyph path={GLYPH.meeting} /> : 'Meeting'}
          </span>
        </span>
      )}
    </>
  )
}

/** A floated badge: a tap opens its list upward (the kind and each item's
 *  words), since a phone shows no hover title. */
function FloatBadge({ kind, compact }: { kind: BadgeKind; compact: boolean }) {
  const { open, buttonRef, panelRef, style, portalRoot, toggle, onKeyDown } = usePortalMenu({ align: 'right', prefer: 'above' })
  const name = `${kind.agents.length} ${kind.label}`
  return (
    <span className={FLOAT_PILL_BASE}>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-label={name}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid={`float-badge-${kind.label.toLowerCase()}`}
        className={`flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs whitespace-nowrap select-none ${kind.tone.bgClass} ${kind.tone.textClass} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/50`}
      >
        {compact && kind.glyph
          ? <Glyph path={kind.glyph} />
          : <span className={`inline-block w-2 h-2 border-[1.5px] ${kind.tone.spinnerClass} border-t-transparent rounded-full animate-spin`} />}
        {compact ? kind.agents.length : name}
      </button>
      {open && createPortal(
        <div
          ref={panelRef}
          role="menu"
          aria-label={name}
          style={style}
          onKeyDown={onKeyDown}
          className={`${POPOVER} w-64 max-w-[calc(100vw-1.5rem)] py-1.5 text-xs`}
        >
          <p className="px-3 py-1 font-medium text-primary">{name}</p>
          {kind.agents.map((a) => (
            <div key={a.id} role="menuitem" tabIndex={-1} className="px-3 py-1 text-secondary break-words focus:outline-none focus-visible:bg-surface-hover">
              {a.description}
            </div>
          ))}
        </div>,
        portalRoot,
      )}
    </span>
  )
}

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
