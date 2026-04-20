import { useState } from 'react'
import { usePlatformSettings, useSetLicense, useLicenseAction } from './PlatformPage.hooks'

// ---------------------------------------------------------------------------
// OtoDock tab — this install's relationship with the OtoDock cloud:
// license/billing plus, where offered, the hosted-relay connection.
// ---------------------------------------------------------------------------

function LicenseBillingCard() {
  const { data } = usePlatformSettings()
  const [licenseKey, setLicenseKey] = useState('')
  const setLicense = useSetLicense()
  const deactivateLicense = useLicenseAction('deactivate')
  const recheckLicense = useLicenseAction('recheck')
  const [licenseMsg, setLicenseMsg] = useState('')

  return (
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4">
        <h3 className="text-sm font-semibold text-p-text mb-1">License &amp; Billing</h3>
        <p className="text-xs text-p-text-light mb-3">
          {data?.cloud
            ? 'OtoDock Cloud — your plan is managed by OtoDock.'
            : data?.air_gapped
              ? `Air-gapped (isolated) install — offline license, no calls to OtoDock. Community: up to ${data?.license_max_users || 5} users free, unlimited agents.`
              : `Self-hosted. Community edition: up to ${data?.license_max_users || 5} users free, unlimited agents.`}
        </p>

        {/* Tier + seats + mode */}
        <div className="flex items-center gap-3 mb-3 flex-wrap">
          <span className={`text-xs font-medium px-2 py-0.5 rounded-sm ${
            data?.license_tier === 'community' ? 'bg-gray-100 dark:bg-gray-800 text-p-text-secondary' :
            'bg-brand-100 text-brand'
          }`}>
            {(data?.license_tier || 'community').charAt(0).toUpperCase() + (data?.license_tier || 'community').slice(1)}
          </span>
          <span className="text-sm text-p-text">{data?.license_users_count || 0} / {data?.license_max_users || 5} users</span>
          {data?.license_tier !== 'community' && data?.license_mode && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-gray-100 dark:bg-gray-800 text-p-text-secondary">
              {data.license_mode === 'subscription' ? 'Subscription' : 'Offline term'}
            </span>
          )}
          {data?.license_status === 'lifetime' && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand">Lifetime</span>
          )}
          {data?.license_valid_until && data?.license_status !== 'lifetime' && (
            <span className="text-xs text-p-text-light">expires {data.license_valid_until.slice(0, 10)}</span>
          )}
        </div>

        {/* Status banners */}
        {data?.license_status === 'unactivated' && (
          <div className="mb-3 text-[11px] px-2 py-1.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400">
            Activation pending — paid seats unlock once this key binds to the install. Running at the community cap until then.
          </div>
        )}
        {data?.license_status === 'grace_unreachable' && (
          <div className="mb-3 text-[11px] px-2 py-1.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400">
            Can&apos;t reach OtoDock to verify your subscription — retrying. Your plan stays active during the grace window.
          </div>
        )}
        {data?.license_status === 'lapsed' && (
          <div className="mb-3 text-[11px] px-2 py-1.5 rounded-sm bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">
            Subscription lapsed — new users are blocked (existing users keep working). Renew, then &quot;Re-check now&quot;.
          </div>
        )}
        {data?.license_status === 'grace' && (
          <div className="mb-3 text-[11px] px-2 py-1.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400">
            Expired {data?.license_days_since_expiry}d ago — renew soon (new users blocked).
          </div>
        )}
        {data?.license_status === 'expired' && (
          <div className="mb-3 text-[11px] px-2 py-1.5 rounded-sm bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">
            Expired {data?.license_days_since_expiry}d ago — new users &amp; agents blocked.
          </div>
        )}

        {/* License key entry — self-hosted only (cloud plans are managed by OtoDock) */}
        {!data?.cloud && (
          <div className="mb-4">
            <label className="block text-xs text-p-text-secondary mb-1">License Key</label>
            <div className="flex gap-2 flex-wrap">
              <input type="password" autoComplete="off" value={licenseKey} onChange={e => setLicenseKey(e.target.value)}
                placeholder={data?.has_license_key ? 'A license key is saved (hidden) — paste a new one to replace' : 'Enter license key'}
                className="flex-1 min-w-[14rem] px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
              <button
                onClick={() => setLicense.mutate(licenseKey, { onSuccess: (r) => { setLicenseMsg(r.message || 'Saved.'); setLicenseKey('') } })}
                disabled={setLicense.isPending}
                className="px-3 py-2 text-xs font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
                {setLicense.isPending ? 'Saving…' : 'Save'}
              </button>
            </div>
            {data?.has_license_key && !licenseMsg && (
              <p className="mt-1.5 text-[11px] text-p-text-light">A license key is saved (encrypted &amp; hidden).</p>
            )}
            {licenseMsg && <p className="mt-1.5 text-[11px] text-p-text-light">{licenseMsg}</p>}

            {/* Activation actions — subscription on a connected install only.
                Hidden on air-gapped / offline-term (nothing to activate). */}
            {!data?.air_gapped && data?.license_mode === 'subscription' && data?.has_license_key && (
              <div className="mt-2 flex items-center gap-3 flex-wrap">
                <span className="text-[11px] text-p-text-light">
                  {data?.license_activation_state === 'activated' ? 'Activated on this install' : 'Not activated'}
                  {data?.license_last_check_at && ` · last checked ${data.license_last_check_at.slice(0, 10)}`}
                </span>
                <button onClick={() => recheckLicense.mutate()} disabled={recheckLicense.isPending}
                  className="text-[11px] text-brand underline disabled:opacity-50">Re-check now</button>
                <button onClick={() => {
                    if (window.confirm('Move this license off this install? It releases the license so you can activate it on another install. This does NOT cancel your subscription.')) {
                      deactivateLicense.mutate()
                      setLicenseMsg('License released from this install — you can now activate it on another. Your subscription is unaffected.')
                    }
                  }}
                  disabled={deactivateLicense.isPending}
                  className="text-[11px] text-p-text-light underline disabled:opacity-50">Move license</button>
              </div>
            )}
            {data?.air_gapped && (
              <p className="mt-1.5 text-[11px] text-p-text-light italic">
                Air-gapped install — use an offline-term license (never contacts OtoDock).
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
      <LicenseBillingCard />
    </div>
  )
}
