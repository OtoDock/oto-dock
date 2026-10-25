import { useState } from 'react'
import { usePlatformSettings, useSetLicense, useLicenseAction } from './PlatformPage.hooks'
import { useReleaseCheck, useReleaseNotice } from '../../components/admin/releaseNotice'
import { useAuth } from '../../contexts/AuthContext'
import { serverVersion } from '../../lib/session/serverInfo'
import { useDialogs } from '../../components/ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// OtoDock tab — this install's relationship with the OtoDock cloud:
// license/billing plus, where offered, the hosted-relay connection; and the
// install itself: the running version and the daily release check.
// ---------------------------------------------------------------------------

export function ThisInstallCard() {
  const { authConfig } = useAuth()
  // On the OtoDock cloud the operator upgrades: the version alone, no check.
  const cloud = !!authConfig?.cloud
  const { data } = useReleaseNotice(true)
  const check = useReleaseCheck()
  const running = data?.running || serverVersion() || 'unknown'

  let status: string
  if (!data || cloud) status = ''
  else if (!data.enabled) status = `Release checks are off: ${data.reason}.`
  else if (data.newer) status = `OtoDock ${data.latest} is available.`
  else if (data.latest) status = `Up to date. The latest release is ${data.latest}.`
  else status = 'No release check has run yet.'

  return (
    <div className={`${CARD} p-4`}>
      <h3 className="text-sm font-semibold text-primary mb-1">This install</h3>
      <p className="text-sm text-primary" data-testid="running-version">OtoDock {running}</p>
      {status && <p className="text-xs text-tertiary mt-1">{status}</p>}
      {data?.enabled && !cloud && (
        <div className="mt-2 flex items-center gap-3 flex-wrap text-2xs text-tertiary">
          {data.newer && data.url && (
            <a href={data.url} target="_blank" rel="noopener noreferrer" className="text-link underline">Release notes</a>
          )}
          {data.checked_at && <span>checked {data.checked_at.slice(0, 16).replace('T', ' ')}</span>}
          <button onClick={() => { check.mutateAsync().catch(toastError('The release check was not run')) }} disabled={check.isPending} className="text-link underline disabled:opacity-50">
            {check.isPending ? 'Checking…' : 'Check now'}
          </button>
          {data.error && <span className="text-status-warning-fg">Last check failed: {data.error}</span>}
        </div>
      )}
    </div>
  )
}

function LicenseBillingCard() {
  const dialogs = useDialogs()
  const { data } = usePlatformSettings()
  const [licenseKey, setLicenseKey] = useState('')
  const setLicense = useSetLicense()
  const deactivateLicense = useLicenseAction('deactivate')
  const recheckLicense = useLicenseAction('recheck')
  const [licenseMsg, setLicenseMsg] = useState('')

  return (
      <div className={`${CARD} p-4`}>
        <h3 className="text-sm font-semibold text-primary mb-1">License and Billing</h3>
        <p className="text-xs text-tertiary mb-3">
          {data?.cloud
            ? 'OtoDock Cloud. OtoDock manages your plan.'
            : data?.air_gapped
              ? `Isolated, air-gapped install. It uses an offline license and never calls OtoDock. Community: up to ${data?.license_max_users || 5} users free, unlimited agents.`
              : `Self-hosted. Community edition: up to ${data?.license_max_users || 5} users free, unlimited agents.`}
        </p>

        {/* Tier + seats + mode */}
        <div className="flex items-center gap-3 mb-3 flex-wrap">
          <span className={`${BADGE_BASE} ${BADGE_TONE[data?.license_tier === 'community' ? 'neutral' : 'info']}`}>
            {(data?.license_tier || 'community').charAt(0).toUpperCase() + (data?.license_tier || 'community').slice(1)}
          </span>
          <span className="text-sm text-primary">{data?.license_users_count || 0} / {data?.license_max_users || 5} users</span>
          {data?.license_tier !== 'community' && data?.license_mode && (
            <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
              {data.license_mode === 'subscription' ? 'Subscription' : 'Offline term'}
            </span>
          )}
          {data?.license_status === 'lifetime' && (
            <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>Lifetime</span>
          )}
          {data?.license_valid_until && data?.license_status !== 'lifetime' && (
            <span className="text-xs text-tertiary">expires {data.license_valid_until.slice(0, 10)}</span>
          )}
        </div>

        {/* Status banners */}
        {data?.license_status === 'unactivated' && (
          <div className="mb-3 text-xs px-3 py-2 rounded-card bg-status-warning-bg text-status-warning-fg">
            Activation pending. Paid seats unlock once this key is tied to this install. Until then, the community limit applies.
          </div>
        )}
        {data?.license_status === 'grace_unreachable' && (
          <div className="mb-3 text-xs px-3 py-2 rounded-card bg-status-warning-bg text-status-warning-fg">
            Can&apos;t reach OtoDock to check your subscription. Trying again. Your plan stays active during the grace period.
          </div>
        )}
        {data?.license_status === 'lapsed' && (
          <div className="mb-3 text-xs px-3 py-2 rounded-card bg-status-danger-bg text-status-danger-fg">
            Your subscription lapsed. New users are blocked. Existing users keep working. Renew, then press &quot;Re-check now&quot;.
          </div>
        )}
        {data?.license_status === 'grace' && (
          <div className="mb-3 text-xs px-3 py-2 rounded-card bg-status-warning-bg text-status-warning-fg">
            Expired {data?.license_days_since_expiry}d ago. New users are blocked, so renew soon.
          </div>
        )}
        {data?.license_status === 'expired' && (
          <div className="mb-3 text-xs px-3 py-2 rounded-card bg-status-danger-bg text-status-danger-fg">
            Expired {data?.license_days_since_expiry}d ago. New users and agents are blocked.
          </div>
        )}

        {/* License key entry — self-hosted only (cloud plans are managed by OtoDock) */}
        {!data?.cloud && (
          <div className="mb-4">
            <label className="block text-xs text-secondary mb-1">License Key</label>
            <div className="flex gap-2 flex-wrap">
              <Input type="password" autoComplete="off" value={licenseKey} onChange={e => setLicenseKey(e.target.value)}
                placeholder={data?.has_license_key ? 'A license key is saved and hidden. Paste a new one to replace it.' : 'Enter license key'}
                className="flex-1 min-w-[14rem]" />
              <Button variant="primary" size="xs"
                onClick={() => { setLicense.mutateAsync(licenseKey).then((r) => { setLicenseMsg(r.message || 'Saved.'); setLicenseKey('') }).catch(toastError('The license key was not saved')) }}
                disabled={setLicense.isPending}>
                {setLicense.isPending ? 'Saving…' : 'Save'}
              </Button>
            </div>
            {data?.has_license_key && !licenseMsg && (
              <p className="mt-1.5 text-2xs text-tertiary">A license key is saved, encrypted and hidden.</p>
            )}
            {licenseMsg && <p className="mt-1.5 text-2xs text-tertiary">{licenseMsg}</p>}

            {/* Activation actions — subscription on a connected install only.
                Hidden on air-gapped / offline-term (nothing to activate). */}
            {!data?.air_gapped && data?.license_mode === 'subscription' && data?.has_license_key && (
              <div className="mt-2 flex items-center gap-3 flex-wrap">
                <span className="text-2xs text-tertiary">
                  {data?.license_activation_state === 'activated' ? 'Activated on this install' : 'Not activated'}
                  {data?.license_last_check_at && ` · last checked ${data.license_last_check_at.slice(0, 10)}`}
                </span>
                <button onClick={() => { recheckLicense.mutateAsync().catch(toastError('The license was not re-checked')) }} disabled={recheckLicense.isPending}
                  className="text-2xs text-link underline disabled:opacity-50">Re-check now</button>
                <button onClick={async () => {
                    if (await dialogs.confirm({
                      title: 'Move this license off this install?',
                      body: 'It releases the license so you can activate it on another install. This does not cancel your subscription.',
                      confirmLabel: 'Move license',
                    })) {
                      deactivateLicense.mutateAsync()
                        .then(() => setLicenseMsg('License released from this install. You can now activate it on another. Your subscription is unaffected.'))
                        .catch(toastError('The license was not moved'))
                    }
                  }}
                  disabled={deactivateLicense.isPending}
                  className="text-2xs text-tertiary underline disabled:opacity-50">Move license</button>
              </div>
            )}
            {data?.air_gapped && (
              <p className="mt-1.5 text-2xs text-tertiary italic">
                Air-gapped install. Use an offline-term license, which never contacts OtoDock.
              </p>
            )}
          </div>
        )}
      </div>
  )
}

export default function OtodockTab() {
  return (
    <div className="space-y-6">
      <ThisInstallCard />
      <LicenseBillingCard />
    </div>
  )
}
