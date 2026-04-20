// Shared TypeScript interfaces for the admin Setup (PlatformPage) tabs.

export interface PlatformSettings {
  company_name: string
  platform_instructions: string
  platform_timezone: string
  session_timeout: string
  jwt_expiry_hours: string
  session_idle_timeout: string     // seconds; idle-reap across all session kinds
  allow_user_paired_machines: boolean
  remote_fallback_user_override: boolean
  remote_fallback_agent_default: boolean
  // Global interactive kill-switch — default OFF (interactive terminal
  // sessions are opt-in per installation)
  interactive_cli_enabled: boolean
  // Storage & retention
  session_retention_enabled: boolean
  session_retention_days: string
  // Automatic MCP updates (weekly; community MCPs)
  mcp_auto_update_enabled: boolean
  // Storage quotas (MB / file-count; 0 = unlimited). storage_quotas_enforced
  // reflects the kernel tier — false means measurement + warnings only.
  quota_shared_folder_mb: string
  quota_user_folder_mb: string
  quota_shared_folder_inodes: string
  quota_user_folder_inodes: string
  storage_quotas_enforced: boolean
  // Security
  smtp_host: string
  smtp_port: string
  smtp_user: string
  smtp_from: string
  smtp_tls: string
  smtp_password_set: boolean
  turnstile_site_key: string
  turnstile_secret_key_set: boolean
  turnstile_managed: boolean
  has_license_key: boolean
  license_tier: string
  license_max_users: number
  license_users_count: number
  license_status: string            // valid|grace|expired|lifetime|unactivated|grace_unreachable|lapsed
  license_valid_until: string       // ISO expiry, or ""
  license_days_since_expiry: number
  license_lifetime: boolean
  license_company_name: string
  license_mode: string              // subscription | offline_term
  license_activation_state: string  // none | activated
  license_check_status: string
  license_last_check_at: string
  air_gapped: boolean               // effective (forced false on cloud)
  cloud: boolean                    // deployment axis
  forced_keys: string[]             // operator-pinned platform settings (hidden + immutable)
  password_min_score: string
  password_min_length: string
  // Require a second factor for local-password accounts (OIDC exempt)
  require_2fa: boolean
  // Passkey sign-in: 'passwordless' (primary button) | 'second_factor' (after password only)
  passkey_login_mode: string
}

export interface ConcurrencyBucket {
  active: number
  limit: number
}

export interface SatelliteStat {
  machine_id: string
  name: string
  online: boolean
  active_sessions: number
  max_sessions: number | null
  cpu_pct: number
  mem_pct: number
}

export interface ConcurrencyStats {
  // Fully automatic live-RAM admission — no count ceiling. *_mb are megabytes;
  // fit_heavy/fit_light = how many more heavy/light sessions currently fit.
  sessions: {
    active: number
    reserved_mb: number
    budget_mb: number
    available_mb: number
    total_mb: number
    fit_heavy: number
    fit_light: number
  }
  tasks: { active: number }
  by_surface: { chat: number; task: number; meeting: number; phone: number }
  satellites: SatelliteStat[]       // per-machine remote counts (RemoteMachinesPage)
}
export interface StorageUsage {
  agents_bytes: number
  session_files_bytes: number
  codex_junk_bytes: number
  recover_bin_bytes: number
  sessions_dir_bytes: number
  logs_bytes: number
  retention: {
    enabled: boolean
    days: number
    last_sweep: Record<string, number | string | boolean> | null
  }
}
