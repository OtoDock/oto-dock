/**
 * Admin → Skills page.
 *
 * Lists installed standalone skill packages (admin MCP rows with
 * ``category === 'skill'`` — filtered OUT of McpServersPage so they don't
 * double-list). Bundled skills stay visible on their MCP's admin row; this
 * page only manages the standalone packages: versions, updates (the shared
 * ``update_one`` endpoint handles the skill branch server-side), delete
 * (shared DELETE /v1/admin/mcps/{name} — accepts category "skill"), and
 * access to the community-skills browser.
 */

import { useRef, useState } from 'react'
import { useAdminMcps, useCheckMcpUpdates, useUpdateMcp, useDeleteMcp, useInstallSkillZip, isUpdateOffer, McpServer, McpUpdateInfo } from '../../api/mcps'
import CommunitySkillsBrowser from '../../components/CommunitySkillsBrowser'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET, CARD, PAGE_TITLE } from '../../components/ui/classes'

export default function SkillsPage() {
  const { data: mcps, isLoading } = useAdminMcps()
  const [showBrowse, setShowBrowse] = useState(false)
  const { data: updateData, refetch: checkUpdates, isFetching: checkingUpdates } = useCheckMcpUpdates()
  const updates = updateData?.updates || {}
  const installZip = useInstallSkillZip()
  const fileRef = useRef<HTMLInputElement>(null)
  const [installMsg, setInstallMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const handleZipPicked = (file: File | null) => {
    if (!file) return
    setInstallMsg(null)
    installZip.mutate(file, {
      onSuccess: r => setInstallMsg({
        ok: true,
        text: `${r.status === 'updated' ? 'Updated' : 'Installed'} ${r.name} v${r.version}`
          + (r.has_scripts ? ', with bundled scripts' : ''),
      }),
      onError: e => setInstallMsg({ ok: false, text: (e as Error)?.message || 'The skill package was not installed.' }),
    })
    if (fileRef.current) fileRef.current.value = ''
  }

  if (isLoading) return <div className="text-sm text-tertiary">Loading skill packages...</div>

  const packages = (mcps ?? []).filter(m => m.category === 'skill')
  const updateCount = packages.filter(p => isUpdateOffer(updates[p.name])).length

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className={PAGE_TITLE}>Skills</h1>
        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
          {packages.length} installed
        </span>
        <div className="flex-1 min-w-0" />
        {updateCount > 0 && (
          <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>
            {updateCount} update{updateCount > 1 ? 's' : ''}
          </span>
        )}
        <Button variant="secondary" size="xs"
          onClick={() => checkUpdates()}
          disabled={checkingUpdates}
        >
          <svg className={`w-3.5 h-3.5 ${checkingUpdates ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <span className="hidden sm:inline">{checkingUpdates ? 'Checking…' : 'Check Updates'}</span>
          <span className="sm:hidden">{checkingUpdates ? '…' : 'Updates'}</span>
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept=".zip,application/zip,application/x-zip-compressed,application/octet-stream"
          className="hidden"
          onChange={e => handleZipPicked(e.target.files?.[0] ?? null)}
        />
        <Button variant="secondary" size="xs"
          onClick={() => fileRef.current?.click()}
          disabled={installZip.isPending}
          title="Upload a skill as a zip. It holds a package with a manifest.json, or a skill folder with SKILL.md."
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M12 4v12m0-12l-4 4m4-4l4 4" />
          </svg>
          <span className="hidden sm:inline">{installZip.isPending ? 'Installing…' : 'Install from Zip'}</span>
          <span className="sm:hidden">{installZip.isPending ? '…' : 'Zip'}</span>
        </Button>
        <Button variant="primary" size="xs"
          onClick={() => setShowBrowse(true)}
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
          </svg>
          <span className="hidden sm:inline">Browse Community Skills</span>
          <span className="sm:hidden">Browse</span>
        </Button>
      </div>

      <p className="text-xs text-tertiary mb-4">
        Skill packages installed from the community skills list or from a zip.
        A zip holds a package with a manifest.json, or a plain skill folder with
        SKILL.md. Turn skills on or off for each agent on its Skills page.
      </p>

      {installMsg && (
        <div className={`mb-3 rounded-card border px-2 py-1.5 ${installMsg.ok
          ? 'border-status-success-border bg-status-success-bg'
          : 'border-status-danger-border bg-status-danger-bg'}`}>
          <p className={`text-2xs whitespace-pre-wrap ${installMsg.ok
            ? 'text-status-success-fg'
            : 'text-status-danger-fg'}`}>{installMsg.text}</p>
        </div>
      )}

      {packages.length === 0 && (
        <p className="text-sm text-tertiary py-4">
          No skill packages installed yet. Browse the community skills to install one.
        </p>
      )}

      <div className="space-y-2">
        {packages.map(pkg => (
          <SkillPackageRow key={pkg.name} pkg={pkg} updateInfo={updates[pkg.name]} />
        ))}
      </div>

      <CommunitySkillsBrowser open={showBrowse} onClose={() => setShowBrowse(false)} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

function SkillPackageRow({ pkg, updateInfo }: { pkg: McpServer; updateInfo?: McpUpdateInfo }) {
  const dialogs = useDialogs()
  const updateMcp = useUpdateMcp()
  const deleteMcp = useDeleteMcp()
  const [error, setError] = useState<string | null>(null)

  const agentCount = pkg.agents?.length ?? 0
  const skillCount = pkg.skills?.length ?? 0

  const handleUpdate = () => {
    setError(null)
    updateMcp.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'The skill package was not updated.'),
    })
  }

  const handleDelete = async () => {
    if (!(await dialogs.confirm({
      title: `Delete "${pkg.label}"?`,
      body: 'This removes the skill package and turns it off for every agent. This cannot be undone.',
      confirmLabel: 'Delete',
      tone: 'danger',
    }))) return
    setError(null)
    deleteMcp.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'The skill package was not deleted.'),
    })
  }

  return (
    <div className={`${CARD} p-3`}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-sm font-medium text-primary">{pkg.label}</span>
            <span className="text-xs font-mono text-tertiary">{pkg.name}</span>
            {pkg.version && (
              <span className="text-3xs text-tertiary">v{pkg.version}</span>
            )}
            <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
              {skillCount} skill{skillCount === 1 ? '' : 's'}
            </span>
            {pkg.has_scripts && (
              <span
                className={`${BADGE_BASE} ${BADGE_TONE.warning}`}
                title="This package bundles executable helper scripts the agent can run"
              >
                Bundles scripts
              </span>
            )}
            {agentCount > 0 && (
              <span className={`${BADGE_BASE} ${BADGE_TONE.success}`}>
                Enabled on {agentCount} agent{agentCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {pkg.description && (
            <p className="text-xs text-tertiary mt-0.5">{pkg.description}</p>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {updateInfo?.reason === 'ahead' && (
            // Installed past the catalog: never an offer, never moved back on
            // its own; the way back is explicit (proxy MCP-FRAMEWORK.md
            // "Automatic MCP Updates").
            <Button variant="secondary" size="xs"
              onClick={async () => {
                if (!(await dialogs.confirm({
                  title: `Revert "${pkg.label}" to the community version ${updateInfo.latest}?`,
                  body: 'It stays on for the same agents.',
                  confirmLabel: 'Revert',
                }))) return
                handleUpdate()
              }}
              disabled={updateMcp.isPending}
              title={`Installed ${updateInfo.current} is newer than the community version ${updateInfo.latest}. Automatic updates leave it alone.`}
            >
              {updateMcp.isPending ? 'Reverting…' : `Revert to ${updateInfo.latest}`}
            </Button>
          )}
          {updateInfo && isUpdateOffer(updateInfo) && (
            <Button variant="secondary" size="xs"
              onClick={handleUpdate}
              disabled={updateMcp.isPending}
              title={updateInfo.current !== updateInfo.latest
                ? `Update ${updateInfo.current} → ${updateInfo.latest}`
                : 'The community version changed. Update to get it.'}
            >
              {updateMcp.isPending ? 'Updating…' : 'Update'}
            </Button>
          )}
          <button
            onClick={handleDelete}
            disabled={deleteMcp.isPending}
            className={BUTTON_DANGER_QUIET}
          >
            {deleteMcp.isPending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-2 rounded-card border border-status-danger-border bg-status-danger-bg px-2 py-1.5">
          <p className="text-2xs text-status-danger-fg whitespace-pre-wrap">{error}</p>
        </div>
      )}
    </div>
  )
}
