# Changelog

All notable changes to OtoDock are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
OtoDock uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each
release's entry is also published as its [GitHub Release](https://github.com/OtoDock/oto-dock/releases).

Upgrading is `docker compose pull && docker compose up -d`. Anything that
changes the behaviour of a running install — a config key, a schema migration, a
changed default — is called out explicitly under its version.

## [Unreleased]

## [1.1.1] — 2026-07-16

### Changed

- Chat dictation can now run up to 3 minutes per take (was 60 seconds, which
  cut long dictations off mid-sentence). Admins can tune this with the
  `audio_chat_stt_max_seconds` setting.

### Fixed

- Live voice mode no longer sends truncated turns. The auto-send fired the
  moment the silence timer expired, racing the speech provider's own
  end-of-utterance commit — long turns went out with the tail missing and
  anything said after was lost. The mic now stops first, the provider flushes
  everything it heard, and the full transcript is what gets sent. Stopping
  dictation likewise waits briefly for that flush, so the last sentence is
  never dropped.
- Dictation no longer doubles your sentences. Two ways a hidden second
  recording session could be left running — a slow microphone connect timing
  out, and stopping the mic while it was still connecting — meant a retry had
  multiple sessions transcribing the same microphone, so every sentence landed
  in the input twice (or more, stacking with each retry).
- The sidebar's "Active now" strip shows the real titles of live chats (it
  showed "New chat" for chats that started after the page loaded — most
  visible in the task-history view).

## [1.1.0] — 2026-07-15

### Added

- Images opened full-screen from a gallery can now be zoomed: pinch on touch,
  mouse wheel or double-click on desktop, with panning while zoomed. Long
  captions are shown in full.

### Fixed

- Video fullscreen on mobile no longer pins the screen to landscape: wide
  videos still rotate as a starting cue, but turning the phone upright rotates
  the video back to portrait fullscreen. Vertical videos are no longer rotated
  to landscape when fullscreen starts before the video dimensions are known.
- ElevenLabs speech-to-text no longer invents trailing text after you stop the
  chat mic. Stopping after a pause committed a silence-only buffer, which the
  model would "transcribe" into words never spoken (most noticeable in
  non-English dictation).
- A speech-to-text provider that fails (bad API key, quota, connection) now
  shows an error on the chat mic instead of a mic that hears nothing.
- Audio settings: the provider API-key row (Save/Remove buttons) no longer
  overflows the screen on mobile.

## [1.0.2] — 2026-07-14

### Added

- This changelog. Every release from here on ships its notes with it, so you can
  see what changed before you pull.

### Fixed

- The proxy reported version `1.0.0` in its API documentation regardless of which
  release was actually running. It now reports the running version.

## [1.0.1] — 2026-07-13

### Fixed

- Signing in could land you on a dead page. The redirect from `/` resolved
  against the wrong list of agents, so anyone who had agents but had not picked a
  favourite hit a broken "Back to Chat". It now falls back to the first agent you
  can actually see, for every role.

## [1.0.0] — 2026-07-13

Initial public release.

OtoDock is a self-hosted platform for running a team of AI agents on
infrastructure you control. It runs the real Claude Code and Codex as its engine,
so your agents inherit everything those CLIs can do, and wraps them in a live
dashboard, a security model built for shared servers, and the plumbing that turns
a coding tool into a team of coworkers.

### Added

- **Agents and chat.** Every step streams live — reasoning, tool calls, file
  edits as diffs, plans ticking off. Approve sensitive actions inline, or let
  trusted agents run unattended.
- **Multi-agent meetings.** Put specialists in one room for a moderated
  discussion where agents address each other, answer in parallel, and converge.
- **Delegation.** Agents hand work to parallel agent sessions you can watch,
  steer, and continue.
- **Bring your own engine.** Connect the Claude or ChatGPT plan you already pay
  for, an API key, or a local model.
- **Sandboxed by default.** Each agent runs locked down and isolated from your
  network; you grant one folder or one service at a time.
- **Schedules and triggers.** Recurring and one-off background tasks, plus
  webhooks that let outside systems start work.
- **Persistent memory** that survives across sessions.
- **Documents and images.** Read and author Word, Excel, PowerPoint and PDF, with
  a live in-chat preview; edit and generate images.
- **Voice.** Speak to your agents and have them answer out loud.
- **Interactive artifacts and pinned mini-apps** — dashboards and small tools
  your agents build and you keep.
- **Community catalogs** for installable MCP capabilities and ready-made agent
  templates.
- **Self-hosted install** via Docker Compose, with your chats, files, memory and
  credentials staying on hardware you run.

[Unreleased]: https://github.com/OtoDock/oto-dock/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/OtoDock/oto-dock/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/OtoDock/oto-dock/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/OtoDock/oto-dock/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/OtoDock/oto-dock/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/OtoDock/oto-dock/releases/tag/v1.0.0
