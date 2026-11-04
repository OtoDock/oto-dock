/**
 * The settings catalog: every setting and action a person can name in the
 * settings areas, where it lives, what it does and who may change it. One
 * source for three readers: the coverage test renders the User Settings
 * sections against it, `design/settings-map.mjs` renders it into the
 * platform skill's settings map and into the JSON the proxy serves at
 * `GET /v1/settings/catalog`, and the settings card in the chat looks a
 * row up by its id.
 *
 * A leaf file: Node reads it with type stripping for the generator, so it
 * holds plain data and types only and imports nothing at run time. The
 * role words are literals typed against `ROLE`, and the catalog test
 * asserts each equals its `ROLE` value.
 *
 * A new User Settings control adds its row here in the same commit.
 */
import type { ROLE } from '../../lib/permissions.ts'
import type { UserSettingsSectionId } from './sections.ts'

export const SETTINGS_CATALOG_VERSION = 1

export const AREA = { USER: 'user', AGENT: 'agent', ADMIN: 'admin' } as const
export type SettingsArea = (typeof AREA)[keyof typeof AREA]

/** What the control is. `action` is a button that opens or does
 *  something, `item` a row that repeats per account, machine or key,
 *  `readonly` a value the page shows and nothing changes. */
export const KIND = {
  TOGGLE: 'toggle', CHOICE: 'choice', TEXT: 'text', NUMBER: 'number',
  ACTION: 'action', ITEM: 'item', READONLY: 'readonly',
} as const
export type ControlKind = (typeof KIND)[keyof typeof KIND]

/** The platform role words the catalog uses, typed against the mirror
 *  without importing it: `member` is any signed-in person on their own
 *  account, `admin` the admin area. `reads` and `writes` are the PLATFORM
 *  role and never an agent's tier: an agent's tier is its own field. */
export const CATALOG_ROLE = {
  MEMBER: 'member',
  ADMIN: 'admin',
} as const satisfies { MEMBER: typeof ROLE.MEMBER; ADMIN: typeof ROLE.ADMIN }
export type CatalogRole = (typeof CATALOG_ROLE)[keyof typeof CATALOG_ROLE]

export interface ChoiceOption {
  value: string
  label: string
}

/** A value the card reads and writes over the proxy with the viewer's
 *  own sign-in: a GET whose JSON holds `field`, a write whose body is
 *  `{[field]: value}`. `default` stands in when the read answers no
 *  field. `confirm` is the sentence an admin confirms before the save.
 *  `cloudManaged` marks a platform setting the operator owns on a cloud
 *  install (the page hides it there, the card says so). */
export interface RestBinding {
  read: { path: string; field: string }
  write: { path: string; field: string; method?: 'PUT' | 'POST' | 'PATCH' }
  default?: boolean | string | number | null
  confirm?: string
  cloudManaged?: boolean
}

/** A value kept in this browser alone. */
export interface DeviceBinding {
  device: 'theme' | 'chat-activity'
}

export interface CatalogEntry {
  /** `<area>.<section>.<name>`, stable: the MCP, the route and the card key on it. */
  id: string
  area: SettingsArea
  /** The section's path segment (`sections.ts`). */
  section: string
  /** Admin Setup's tab. */
  tab?: string
  /** The heading the control sits under. */
  group: string
  /** The control's visible label, or the button's text. */
  label: string
  /** Other visible names the control takes. */
  names?: readonly string[]
  kind: ControlKind
  options?: readonly ChoiceOption[]
  /** What it does: the text behind the info button, or one sentence. */
  help: string
  /** When it is hidden or off: one sentence. */
  note?: string
  /** A per-item row: what one row is. */
  item?: string
  /** The lowest role that sees it. */
  reads: CatalogRole
  /** The lowest role that changes it, null when nothing does. */
  writes: CatalogRole | null
  /** Present when the card in the chat can show and save it. */
  value?: RestBinding | DeviceBinding
}

const UI_PREFS = '/v1/users/me/ui-prefs'
const AUDIO_PREFS = '/v1/users/me/audio-prefs'

type UserEntry = Omit<CatalogEntry, 'area' | 'section' | 'reads' | 'writes'> & { reads?: CatalogRole; writes?: CatalogRole | null }

function user(section: UserSettingsSectionId, entry: UserEntry): CatalogEntry {
  return { area: AREA.USER, section, reads: CATALOG_ROLE.MEMBER, writes: CATALOG_ROLE.MEMBER, ...entry }
}

const GENERAL: readonly CatalogEntry[] = [
  user('general', {
    id: 'user.general.display-name', group: 'Profile', label: 'Display Name', kind: KIND.TEXT,
    help: 'The name other people see on your messages.',
    value: { read: { path: '/auth/me', field: 'display_name' }, write: { path: '/v1/users/me/profile', field: 'display_name' } },
  }),
  user('general', {
    id: 'user.general.email', group: 'Profile', label: 'Email', names: ['Update Email', 'Confirm password to change email'], kind: KIND.TEXT,
    help: 'The address you sign in with. Changing it asks for your password.',
    note: 'A local account only. An account from a sign-in provider shows it read only.',
  }),
  user('general', {
    id: 'user.general.role', group: 'Profile', label: 'Role', kind: KIND.READONLY, writes: null,
    help: 'Your platform role. An administrator changes it under Admin, Users.',
  }),
  user('general', {
    id: 'user.general.change-password', group: 'Security', label: 'Change Password',
    names: ['Current password', 'New password', 'Confirm new password'], kind: KIND.ACTION,
    help: 'Sets a new password after you confirm the current one.',
    note: 'A local account only.',
  }),
  user('general', {
    id: 'user.general.add-passkey', group: 'Security', label: 'Add a passkey',
    names: ['+ Add another passkey', 'Create passkey', 'Name, e.g. This laptop'], kind: KIND.ACTION,
    help: 'Adds a passkey to sign in with, after you confirm your password.',
    note: 'A local account only, and only when OtoDock is open at its secure address.',
  }),
  user('general', {
    id: 'user.general.passkey', group: 'Security', label: 'Passkey', names: ['Rename', 'Remove', 'Passkey name', 'Password'],
    kind: KIND.ITEM, item: 'a passkey',
    help: 'Renames or removes one of your passkeys. Each change asks for your password.',
  }),
  user('general', {
    id: 'user.general.authenticator-app', group: 'Security', label: 'Use an authenticator app',
    names: ['+ Set up an authenticator app', 'Verify and enable', 'Remove', '000000'], kind: KIND.ACTION,
    help: 'Adds time-based codes as a second factor, with recovery codes to keep. Remove takes it off again.',
    note: 'A local account only.',
  }),
  user('general', {
    id: 'user.general.theme', group: 'Appearance', label: 'Theme', kind: KIND.CHOICE,
    options: [{ value: 'system', label: 'System' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }],
    help: 'Choose how the dashboard looks. Kept in this browser.',
    value: { device: 'theme' },
  }),
  user('general', {
    id: 'user.general.chat-activity', group: 'Appearance', label: 'Chat activity', kind: KIND.CHOICE,
    options: [{ value: 'compact', label: 'Compact' }, { value: 'detailed', label: 'Detailed' }],
    help: 'Compact folds tool activity into expandable summaries. Kept in this browser.',
    value: { device: 'chat-activity' },
  }),
  user('general', {
    id: 'user.general.subscription-usage-alerts', group: 'Notifications', label: 'Subscription usage alerts', kind: KIND.TOGGLE,
    help: 'This watches your connected Claude and ChatGPT accounts. You get a notification when one passes 90% of its weekly limit. You get another when it reaches the limit.',
    value: { read: { path: UI_PREFS, field: 'subscription_usage_alerts' }, write: { path: UI_PREFS, field: 'subscription_usage_alerts' }, default: true },
  }),
  user('general', {
    id: 'user.general.share-focus', group: 'Apps', label: 'Agents know which app I am looking at', kind: KIND.TOGGLE,
    help: 'While an app is on your screen, the agent you talk to learns its name. Nobody else learns what you are looking at.',
    value: { read: { path: UI_PREFS, field: 'share_focus_with_agents' }, write: { path: UI_PREFS, field: 'share_focus_with_agents' }, default: true },
  }),
  user('general', {
    id: 'user.general.agents-may-open-apps', group: 'Apps', label: 'Agents may open apps on my screen', kind: KIND.TOGGLE,
    help: 'An agent can bring up one of its apps for you. In the chat you are in, it opens right away. Anywhere else, you get a notice with an Open button.',
    value: { read: { path: UI_PREFS, field: 'agents_may_open_apps' }, write: { path: UI_PREFS, field: 'agents_may_open_apps' }, default: true },
  }),
  user('general', {
    id: 'user.general.wake-word', group: 'Wake word', label: 'Wake word', names: ['Off', 'On'], kind: KIND.TOGGLE,
    help: 'To start a voice conversation, say “Hey OtoDock” on any dashboard page. You can also say “Hey” plus an agent’s name. Detection runs only on this device. Audio never leaves it until a conversation starts.',
    note: 'The microphone is only on while the dashboard is open.',
    value: { read: { path: AUDIO_PREFS, field: 'wake_word_enabled' }, write: { path: AUDIO_PREFS, field: 'wake_word_enabled' }, default: false },
  }),
  user('general', {
    id: 'user.general.wake-word-diagnostics', group: 'Wake word', label: 'Record diagnostics',
    names: ['Stop recording diagnostics', 'Troubleshooting'], kind: KIND.ACTION,
    help: 'Records what the wake word hears on this device, for troubleshooting.',
    note: 'Under Troubleshooting, shown while the wake word is on.',
  }),
  user('general', {
    id: 'user.general.clear-memory', group: 'Memory', label: 'Clear my memory across all agents', kind: KIND.ACTION,
    help: 'Deletes the personal memories agents captured about you, across every agent. It asks you to type a confirmation first.',
  }),
]

const INTEGRATIONS: readonly CatalogEntry[] = [
  user('integrations', {
    id: 'user.integrations.add-account', group: 'Connected Accounts', label: 'Add an account', names: ['Add another account'], kind: KIND.ACTION,
    help: 'Signs you in to a service your agents use on your behalf, or saves its credentials. A service that takes one account offers no second.',
    note: 'A service is listed when a tool of one of your agents uses it.',
  }),
  user('integrations', {
    id: 'user.integrations.account', group: 'Connected Accounts', label: 'Connected account',
    names: ['Set as default', 'This is the default account', 'Edit', 'Close', 'Connect', 'Reconnect', 'Disconnect', 'Remove', 'Save', 'Add account', 'Save token', 'Grant for whole tenant', 'Get device code'],
    kind: KIND.ITEM, item: 'a connected account of a service',
    help: 'One account of a service: its label, the services it reaches, the default star, and Reconnect, Disconnect or Remove. Agents not bound to another account use the default.',
    note: 'A personal access token takes an optional account label. An account whose sign-in can no longer be renewed shows Reconnect needed with the reason.',
  }),
  user('integrations', {
    id: 'user.integrations.agent-binding', group: 'Use this account for specific agents', label: 'Use this account for specific agents',
    names: ['Change', 'Read only', 'All repositories the account reaches', 'Search repositories'],
    kind: KIND.ITEM, item: 'an agent the account serves',
    help: 'Binds one of your agents to this account. On GitHub you can limit the repositories the agent reaches in your chats, or make it read only.',
  }),
  user('integrations', {
    id: 'user.integrations.installations', group: 'Installations', label: 'Install on GitHub', names: ['Change on GitHub', 'Refresh'],
    kind: KIND.ITEM, item: 'a GitHub App installation',
    help: 'Picks the repositories on GitHub this account reaches. Until the app is installed somewhere, the account reaches nothing.',
    note: 'A GitHub account connected through a GitHub App.',
  }),
  user('integrations', {
    id: 'user.integrations.subscribe-to-events', group: 'Active subscriptions', label: '+ Subscribe to events',
    names: ['Subscribe as', 'Subscribe to', 'Event types', 'Create subscription'], kind: KIND.ACTION,
    help: 'Subscribes to a service\'s events, for yourself or for an agent this account serves. A personal subscription fires your own triggers and apps.',
    note: 'A service with an event API. Subscribing as an agent needs the manager role on it, and a Personal-only agent is not offered.',
  }),
  user('integrations', {
    id: 'user.integrations.subscription', group: 'Active subscriptions', label: 'Subscription', names: ['Copy URL', 'Copy secret', 'Delete'],
    kind: KIND.ITEM, item: 'an event subscription',
    help: 'One event subscription: its address and secret to copy, and Delete.',
  }),
  user('integrations', {
    id: 'user.integrations.create-api-key', group: 'API Keys', label: '+ Create key',
    names: ['Create', 'Name', 'triggers: fire your own webhook triggers', 'Copy to clipboard'], kind: KIND.ACTION,
    help: 'Makes a personal API key for your webhook triggers. The key shows once.',
  }),
  user('integrations', {
    id: 'user.integrations.api-key', group: 'API Keys', label: 'API key', names: ['Revoke'], kind: KIND.ITEM, item: 'a personal API key',
    help: 'One of your personal API keys, with when it was made and last used. Revoke ends it at once.',
  }),
]

const REMOTE_MACHINES: readonly CatalogEntry[] = [
  user('remote-machines', {
    id: 'user.remote-machines.pair-machine', group: 'Remote Machines', label: 'Pair Machine',
    names: ['Pair', 'Machine Name', 'Allow full filesystem access', 'Operating system'], kind: KIND.ACTION,
    help: 'Connects your own computer, so agents can run on it. The pair command runs once on that machine.',
    note: 'Hidden when the build lacks the feature. A notice replaces the page when an administrator turned user pairing off.',
  }),
  user('remote-machines', {
    id: 'user.remote-machines.remove-machine', group: 'Remote Machines', label: 'Remove machine', names: ['Confirm'],
    kind: KIND.ITEM, item: 'a machine you paired',
    help: 'Unpairs the machine. A machine an administrator paired for the platform is removed under Admin, Remote Machines.',
  }),
  user('remote-machines', {
    id: 'user.remote-machines.allow-full-fs', group: 'Filesystem access', label: 'Allow full FS',
    kind: KIND.ITEM, item: 'a machine you paired',
    help: 'On, agents can read and write any path your OS user can reach. Off, they stay in their agent folders and your home folder. The home folder limit is a guardrail, not full isolation.',
  }),
  user('remote-machines', {
    id: 'user.remote-machines.device-control', group: 'Device control', label: 'Computer control',
    names: ['Browser control', 'App connectors', 'Grant'], kind: KIND.ITEM, item: 'a machine you paired',
    help: 'Grants agents the mouse, keyboard and screen, a browser on this computer, or the applications on it. Turn one on only on a computer you trust them with.',
  }),
  user('remote-machines', {
    id: 'user.remote-machines.browser-mode', group: 'Device control', label: 'Browser mode',
    names: ['Playwright Extension token', 'Save token', 'Clear', 'Browser profile', 'Also let unattended runs like scheduled tasks, triggers, calls and meetings use this browser. Off, they cannot use it at all.'],
    kind: KIND.ITEM, item: 'a machine with browser control',
    help: 'A dedicated profile, or your own browser through the Playwright Extension and its token. Browser profile names the profile that has the extension. With a saved token you can also let unattended runs use it.',
    note: 'Shown once browser control is granted.',
  }),
  user('remote-machines', {
    id: 'user.remote-machines.run-agents', group: 'Run these agents on this machine', label: 'Run these agents on this machine',
    kind: KIND.ITEM, item: 'an agent of yours',
    help: 'Ticks the agents whose chats run on this machine for you. An agent already on another machine says so.',
  }),
]

const AI_ENGINES: readonly CatalogEntry[] = [
  user('ai-engines', {
    id: 'user.ai-engines.connect-account', group: 'AI Engines', label: 'Connect your Claude account',
    names: ['Connect your ChatGPT account', 'Add another account', 'Connect', 'Open', 'Copy', 'Try again', 'Authorization code'], kind: KIND.ACTION,
    help: 'Signs in to your Claude or ChatGPT subscription, so your own chats and agents run on it. The Claude popup uses the browser\'s session at platform.claude.com.',
  }),
  user('ai-engines', {
    id: 'user.ai-engines.use-api-key', group: 'AI Engines', label: 'Use an API key instead',
    names: ['Add', 'API key', 'Label, optional'], kind: KIND.ACTION,
    help: 'Saves an API key of the engine\'s vendor in place of a subscription.',
  }),
  user('ai-engines', {
    id: 'user.ai-engines.subscription', group: 'AI Engines', label: 'Subscription', names: ['Reconnect', 'Remove'],
    kind: KIND.ITEM, item: 'a connected account or API key',
    help: 'One connected account, with its status and usage windows. Reconnect revives an expired login or renews one that ends soon. Remove takes it off.',
  }),
  user('ai-engines', {
    id: 'user.ai-engines.personal-use', group: 'AI Engines', label: 'Personal use',
    kind: KIND.ITEM, item: 'a connected account',
    help: 'Use this account for your own chats.',
  }),
  user('ai-engines', {
    id: 'user.ai-engines.shared-with-agents', group: 'AI Engines', label: 'Shared with agents',
    kind: KIND.ITEM, item: 'a connected account', reads: CATALOG_ROLE.ADMIN, writes: CATALOG_ROLE.ADMIN,
    help: 'Add this account to the shared subscriptions your agents use.',
    note: 'Platform administrators only.',
  }),
]

const AUDIO_MODES: readonly ChoiceOption[] = [
  { value: 'auto', label: 'Auto' }, { value: 'native', label: 'Native' }, { value: 'platform', label: 'Platform' },
]

const AUDIO: readonly CatalogEntry[] = [
  user('audio', {
    id: 'user.audio.voice-playback', group: 'Audio', label: 'Voice playback', kind: KIND.CHOICE, options: AUDIO_MODES,
    help: 'What reads replies aloud: this device, the platform\'s speech service, or Auto.',
    note: 'Shown when the administrator\'s policy is User\'s choice. Otherwise the platform policy sets it.',
    value: { read: { path: AUDIO_PREFS, field: 'tts_mode' }, write: { path: AUDIO_PREFS, field: 'tts_mode' }, default: 'auto' },
  }),
  user('audio', {
    id: 'user.audio.dictation', group: 'Audio', label: 'Dictation', kind: KIND.CHOICE, options: AUDIO_MODES,
    help: 'What listens when you dictate: this device, the platform\'s speech service, or Auto.',
    note: 'Shown when the administrator\'s policy is User\'s choice. Also in Safari, and on an iPhone or iPad, under Native preferred with a speech provider.',
    value: { read: { path: AUDIO_PREFS, field: 'stt_mode' }, write: { path: AUDIO_PREFS, field: 'stt_mode' }, default: 'auto' },
  }),
  user('audio', {
    id: 'user.audio.dictation-language', group: 'Audio', label: 'Dictation language', kind: KIND.CHOICE,
    options: [
      { value: 'auto', label: 'Automatic' }, { value: 'en-US', label: 'English, US' }, { value: 'en-GB', label: 'English, UK' },
      { value: 'el-GR', label: 'Greek' }, { value: 'de-DE', label: 'German' }, { value: 'es-ES', label: 'Spanish' },
      { value: 'fr-FR', label: 'French' }, { value: 'it-IT', label: 'Italian' },
    ],
    help: 'The language the mic recognizes. Set it to dictate in a language other than English. Automatic starts in your browser language. Within a second or two, it moves to the language you speak. Without a speech service, or with Dictation set to Native, this device listens in its own language.',
    note: 'Hidden when chat audio is off. The chat\'s microphone button also picks it.',
    value: { read: { path: AUDIO_PREFS, field: 'stt_language' }, write: { path: AUDIO_PREFS, field: 'stt_language' }, default: 'en-US' },
  }),
  user('audio', {
    id: 'user.audio.device-voice', group: 'Audio', label: 'Voice', names: ['Language'], kind: KIND.CHOICE,
    help: 'Preferred device voice for native playback on this device. Set one language at a time. Kept in this browser.',
    note: 'Shown when this device has voices.',
  }),
]

const USAGE: readonly CatalogEntry[] = [
  user('usage', {
    id: 'user.usage.summary', group: 'Usage', label: 'Usage', kind: KIND.READONLY, writes: null,
    help: 'Your platform API use this month and this week, with your own accounts beside it. A daily chart and a per-agent list follow.',
  }),
  user('usage', {
    id: 'user.usage.subscription-cap', group: 'My subscriptions', label: 'Week %',
    names: ['Day %', 'Week $', 'Day $', 'When reached', 'Save'], kind: KIND.NUMBER,
    help: 'Caps your own connected accounts on each engine. Cap them at a share of their weekly window, or at the matching API cost. The first cap reached stops your new chats, or moves them onto an API key. Empty means no cap.',
  }),
  user('usage', {
    id: 'user.usage.api-key-cap', group: 'My API keys', label: 'Set cap', names: ['Close', 'Monthly ($)', 'Weekly ($)', 'Save'], kind: KIND.ACTION,
    help: 'Caps what your own Anthropic or OpenAI API keys spend, by month and by week.',
  }),
]

const PLATFORM_SETTINGS = '/v1/admin/platform-settings'
const SETUP = 'platform'

type AdminEntry = Omit<CatalogEntry, 'area' | 'section' | 'reads' | 'writes'> & { section?: string; writes?: CatalogRole | null }

function admin(entry: AdminEntry): CatalogEntry {
  return { area: AREA.ADMIN, section: SETUP, reads: CATALOG_ROLE.ADMIN, writes: CATALOG_ROLE.ADMIN, ...entry }
}

/** A platform toggle the card saves after a confirm naming the effect. */
function platformToggle(field: string, confirm: string, fallback: boolean, cloudManaged = false): RestBinding {
  const binding: RestBinding = { read: { path: PLATFORM_SETTINGS, field }, write: { path: PLATFORM_SETTINGS, field }, default: fallback, confirm }
  if (cloudManaged) binding.cloudManaged = true
  return binding
}

/** The admin area: the Setup tabs' rows, then one row per admin section.
 *  The System Settings toggles carry cards. */
const ADMIN: readonly CatalogEntry[] = [
  admin({
    id: 'admin.platform.company-name', tab: 'General', group: 'Identity', label: 'Company Name', kind: KIND.TEXT,
    help: 'Shown as a heading in every agent\'s instructions.',
  }),
  admin({
    id: 'admin.platform.general-instructions', tab: 'General', group: 'Identity', label: 'General Instructions', kind: KIND.TEXT,
    help: 'Rules and context every agent reads with its own instructions.',
  }),
  admin({
    id: 'admin.platform.ai-engines', tab: 'AI Engines', group: 'AI Engines', label: 'Platform subscriptions and API keys',
    names: ['Connect', 'Add API key', 'Add local model server'], kind: KIND.ITEM, item: 'an engine',
    help: 'The subscriptions, API keys, local model servers and model lists the whole platform runs on. Each model has a tier and a price.',
  }),
  admin({
    id: 'admin.platform.otodock', tab: 'OtoDock', group: 'OtoDock', label: 'This install', names: ['License Key', 'Save', 'Check now', 'Re-check now', 'Move license'], kind: KIND.ITEM, item: 'the license, the version and the billing',
    help: 'The running version, whether a newer release is out, and the license and billing. A license key is pasted and saved here.',
  }),
  admin({
    id: 'admin.platform.audio', tab: 'Audio', group: 'Audio', label: 'Chat audio policy', names: ['Add provider'], kind: KIND.ITEM, item: 'a speech provider',
    help: 'The chat audio policy: Native only, Native preferred, Platform only or User\'s choice. Also the speech providers with their keys, voices and defaults.',
  }),
  admin({
    id: 'admin.platform.phone', tab: 'Phone', group: 'Phone', label: 'Phone servers and routes', names: ['Add server', 'Add route'], kind: KIND.ITEM, item: 'a phone server or route',
    help: 'The phone servers and the routes, each with its agent, language, transfer targets and PIN. Also the call log, the caller data and the languages automatic detection may pick.',
  }),
  admin({
    id: 'admin.platform.require-2fa', tab: 'Security', group: 'Security', label: 'Require Two-Factor Authentication', kind: KIND.TOGGLE,
    help: 'Every password account must add a passkey or an authenticator app before it signs in again.',
    note: 'Refused while it would lock the administrator out.',
  }),
  admin({
    id: 'admin.platform.sharing', tab: 'Security', group: 'Sharing', label: 'Sharing', names: ['Sharing to agents', 'Sharing to departments', 'External links', 'Links without a password', 'User directory when sharing', 'Longest share expiry'], kind: KIND.TOGGLE,
    help: 'Whether people may share apps and chats with agents, with departments and by link. Also whether a link may go without a password, and the longest a share lasts.',
  }),
  admin({
    id: 'admin.platform.timezone', tab: 'System Settings', group: 'System Settings', label: 'Platform Timezone', kind: KIND.TEXT,
    help: 'Used for scheduled tasks and for the date and time agents see in messages.',
  }),
  admin({
    id: 'admin.platform.login-session-duration', tab: 'System Settings', group: 'System Settings', label: 'Login Session Duration', kind: KIND.NUMBER,
    help: 'Hours of inactivity before a user must sign in again.',
    note: 'Active users stay signed in, so it only signs out idle sessions.',
  }),
  admin({
    id: 'admin.platform.allow-user-paired-machines', tab: 'System Settings', group: 'System Settings', label: 'Allow users to pair their own remote machines', kind: KIND.TOGGLE,
    help: 'Users can pair their own machines, like laptops or home servers. Their agents can then run there.',
    note: 'Turning it off disconnects every user-paired machine at once and blocks new pairings.',
    value: platformToggle('allow_user_paired_machines', 'Off disconnects every user-paired machine at once, removes the machine choices people made and refuses new pairings. On lets people pair their own machines again.', true),
  }),
  admin({
    id: 'admin.platform.fallback-user-machine', tab: 'System Settings', group: 'System Settings', label: 'Fall back to local when a user\'s machine is offline', kind: KIND.TOGGLE,
    help: 'If a user\'s chosen machine is offline, the session runs on the platform instead.',
    value: platformToggle('remote_fallback_user_override', 'On, a chat whose machine is offline runs on the platform instead. Off, it waits for the machine.', true),
  }),
  admin({
    id: 'admin.platform.fallback-agent-machine', tab: 'System Settings', group: 'System Settings', label: 'Fall back to local when the agent\'s remote target is offline', kind: KIND.TOGGLE,
    help: 'When a machine an admin chose is offline, the session runs on the platform instead.',
    note: 'Off by default, because an admin often picks a machine to keep the data there.',
    value: platformToggle('remote_fallback_agent_default', 'On, an agent whose machine is offline runs on the platform, so its data may leave the machine. Off, it waits for the machine.', false),
  }),
  admin({
    id: 'admin.platform.interactive-terminals', tab: 'System Settings', group: 'System Settings', label: 'Interactive terminal sessions', kind: KIND.TOGGLE,
    help: 'Chats and tasks can run Claude Code or Codex as a live dashboard terminal. Off, every session runs the standard way and the interactive switches are hidden.',
    value: platformToggle('interactive_cli_enabled', 'Off hides the terminal switches everywhere and every new session runs the standard way. On brings the terminals back.', true),
  }),
  admin({
    id: 'admin.platform.subscription-windows', tab: 'System Settings', group: 'System Settings', label: 'Read subscription usage from the provider', kind: KIND.TOGGLE,
    help: 'OtoDock asks Claude and ChatGPT for each connected account\'s session and weekly usage and prefers the account that resets first. Off, the shared subscriptions balance by recorded cost only.',
    value: platformToggle('subscription_windows_enabled', 'Off stops reading the usage windows from Claude and ChatGPT, and the AI Engines cards lose their usage bars. On reads them again.', true),
  }),
  admin({
    id: 'admin.platform.session-cleanup', tab: 'System Settings', group: 'Storage and Retention', label: 'Clean up old chat session files', kind: KIND.TOGGLE,
    help: 'Chats left untouched for the set period lose their session files, here and on the machines they ran on. Their full history stays saved.',
    value: platformToggle('session_retention_enabled', 'On, chats left untouched for the set period lose their session files, here and on the machines they ran on. Their history stays. Off keeps every file.', true, true),
  }),
  admin({
    id: 'admin.platform.archive-cleanup', tab: 'System Settings', group: 'Archive of removed people', label: 'Archive of removed people', names: ['Enabled'], kind: KIND.TOGGLE,
    help: 'A removed person\'s folders are kept in an archive for the set number of days, then deleted.',
    value: platformToggle('offboarded_retention_enabled', 'On, a removed person\'s archived folders are deleted after the set number of days. Off keeps every archive for ever.', true, true),
  }),
  admin({
    id: 'admin.platform.mcp-auto-updates', tab: 'System Settings', group: 'Automatic MCP Updates', label: 'Automatic MCP Updates', names: ['Enabled'], kind: KIND.TOGGLE,
    help: 'Once a week, in a quiet window, OtoDock applies available updates to community MCPs. Only failures notify admins.',
    value: platformToggle('mcp_auto_update_enabled', 'On, community MCPs update once a week on their own. Off, every update waits for an admin.', true),
  }),
  admin({
    id: 'admin.platform.store-review', tab: 'System Settings', group: 'Mobile apps', label: 'Ask for a store rating', kind: KIND.TOGGLE,
    help: 'The Android and iOS apps ask each person to rate OtoDock in Google Play or the App Store. They ask twice at most, after something went well.',
    value: platformToggle('store_review_enabled', 'On, the Android and iOS apps ask each person for a store rating twice at most. Off, they never ask.', true),
  }),
  admin({
    id: 'admin.platform.store-review-days', tab: 'System Settings', group: 'Mobile apps', label: 'Days before the second ask', kind: KIND.NUMBER,
    help: 'The second store rating ask comes at least this many days after the first. From 30 to 730.',
    value: {
      read: { path: PLATFORM_SETTINGS, field: 'store_review_days' },
      write: { path: PLATFORM_SETTINGS, field: 'store_review_days' },
      default: 90,
      confirm: 'The second ask comes at least this many days after the first.',
    },
  }),
  admin({
    id: 'admin.users.add-user', section: 'users', group: 'Users', label: 'Add User', names: ['Invite link', 'Temporary password'], kind: KIND.ACTION,
    help: 'Adds a person with an invite link or a temporary password.',
  }),
  admin({
    id: 'admin.users.user', section: 'users', group: 'Users', label: 'User', names: ['Platform role', 'Agent access', 'Platform API', 'Reset password', 'Delete'], kind: KIND.ITEM, item: 'a person',
    help: 'A person\'s platform role and their role on each agent. Also whether they may use the platform\'s API credentials, a password reset and deletion.',
  }),
  admin({
    id: 'admin.usage.limits', section: 'usage', group: 'Usage and Limits', label: 'Usage and Limits', names: ['Agent Budgets'], kind: KIND.NUMBER,
    help: 'The platform\'s totals and costs, each person\'s usage with a limit, agent budgets and the role defaults.',
  }),
  admin({
    id: 'admin.mcp-servers.server', section: 'mcp-servers', group: 'MCP Servers', label: 'MCP server', names: ['Check Updates', 'Install', 'Browse Community', 'Update', 'Switch', 'Revert to catalog', 'Forget'], kind: KIND.ITEM, item: 'an installed MCP',
    help: 'Every installed tool with its switch, settings, instances and updates. Install takes a ZIP, Browse Community the catalog.',
  }),
  admin({
    id: 'admin.skills.package', section: 'skills', group: 'Skills', label: 'Skill package', names: ['Check Updates', 'Update', 'Delete', 'Browse Community Skills'], kind: KIND.ITEM, item: 'an installed skill package',
    help: 'Every installed skill package with its updates and the catalog to browse.',
  }),
  admin({
    id: 'admin.mcp-requests.request', section: 'mcp-requests', group: 'MCP Requests', label: 'Request', names: ['Approve', 'Needs instance'], kind: KIND.ITEM, item: 'a request',
    help: 'An agent\'s request for a tool or a skill, approved with an instance when the tool takes one.',
  }),
  admin({
    id: 'admin.remote-machines.pair', section: 'remote-machines', group: 'Remote Machines', label: 'Pair New Machine', kind: KIND.ACTION,
    help: 'Pairs a machine for the whole platform, where agents run by default.',
  }),
  admin({
    id: 'admin.remote-machines.machine', section: 'remote-machines', group: 'Remote Machines', label: 'Machine', names: ['Max sessions', 'Auto-update', 'Filesystem access', 'Device control', 'Remove'], kind: KIND.ITEM, item: 'a paired machine',
    help: 'A machine\'s capabilities, versions, capacity, updates and grants. The people\'s own machines are listed too, each removable.',
  }),
  admin({
    id: 'admin.remote-machines.all-roles', section: 'remote-machines', group: 'Who runs here', label: 'Every role of its agents runs here', kind: KIND.ITEM, item: 'a machine an admin paired',
    help: 'Off, the chats of editors, contributors and viewers run on the OtoDock platform, as the machine has no sandbox. On, they run on the machine with its access. An outside caller always runs on the platform.',
    note: 'Only the agents whose default place is this machine. Turning it on asks first.',
  }),
  admin({
    id: 'admin.shares.share', section: 'shares', group: 'Shares', label: 'Share', names: ['Revoke'], kind: KIND.ITEM, item: 'a share or link',
    help: 'Every share to people, agents and departments and every link on the install, each revocable.',
  }),
  admin({
    id: 'admin.reports.report', section: 'reports', group: 'Reports', label: 'Report', names: ['Handled', 'Dismiss', 'Restore', 'Delete'], kind: KIND.ITEM, item: 'a reported reply',
    help: 'The agent replies people flagged, newest first, with who reported and why. Mark one handled or dismiss it, restore it from the closed list, or delete it.',
  }),
]

export const CATALOG: readonly CatalogEntry[] = [
  ...GENERAL,
  ...INTEGRATIONS,
  ...REMOTE_MACHINES,
  ...AI_ENGINES,
  ...AUDIO,
  ...USAGE,
  ...ADMIN,
]

/** The entry with this id, or undefined. */
export function findEntry(id: string): CatalogEntry | undefined {
  return CATALOG.find((e) => e.id === id)
}

/** The entries of one section of one area, in page order. */
export function entriesOf(area: SettingsArea, section: string): CatalogEntry[] {
  return CATALOG.filter((e) => e.area === area && e.section === section)
}

/** The visible names an entry answers to: its label, its other names and
 *  its options' labels. */
export function namesOf(entry: CatalogEntry): string[] {
  return [entry.label, ...(entry.names ?? []), ...(entry.options ?? []).map((o) => o.label)]
}
