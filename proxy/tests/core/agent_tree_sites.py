"""The list of ``test_agent_tree_surface.py``: every raw file call of the
server and the satellite, by file and function, with its reason.

A row is ``(file, function): Site(reason, calls, why)``. The file is its
path from ``proxy/`` (or from ``satellite/`` in ``SATELLITE_SITES``). The
function is ``Class.method``, a nested one ``outer.inner``, ``<module>``
outside any. ``calls`` is every raw call of that function, sorted, each as
often as it is made. ``why`` says what the path is and, in the agents tree,
why the call is sound there.

An ``AGENTS`` row's ``why`` starts with the row (``A22``) or the finding
(``F22``) of the 2026-11 file audit it was judged in, ``new`` when the audit
did not name it, or ``to judge`` while the fix of a lane named there is not
merged yet.

``PROXY_ROOTS`` and ``SATELLITE_ROOTS`` are the list of
``test_safe_fs_roots.py``, in the same shape: each root handed to the safe
helpers that the test cannot name, by file and function, with the roots as
written in place of the calls, and why each is a folder no other process
replaces.
"""

from __future__ import annotations

from typing import NamedTuple

#: The server's own data: its sessions folder, the platform data folder
#: outside the agents tree, shipped files, settings, logs, ``/proc``, the
#: installed tools folder, which only the installer writes (except a folder a
#: T1 tool's compose binds: the C13 to C16 rows are judged on their own). On
#: the satellite: its own state, config and update staging.
OWN = "own data"
#: A private copy the server made, in a temporary folder of its own.
COPY = "a private copy"
#: Code that runs inside a session's sandbox, with the session's own rights.
SANDBOX = "inside a sandbox"
#: Code under ``proxy/`` that runs only in the image's build, or only on a
#: Windows computer (never a compute machine). Code a compute machine runs
#: (the satellite's vendored copies, its start) takes the reason of what it
#: reaches there, as the satellite's own rows do.
ELSEWHERE = "runs elsewhere"
#: The argument is no path: a file object, a descriptor, bytes in memory.
NOT_A_PATH = "not a path"
#: Satellite only: a paired computer (a person's own, or one an admin
#: paired), where a session runs unsandboxed as the satellite's own account,
#: so the call reaches nothing the session cannot. A compute machine, where
#: sessions run sandboxed, skips it.
PERSON = "a paired computer"
#: The agents tree, judged: the why says why the call is sound there.
AGENTS = "the agents tree"

REASONS = frozenset({OWN, COPY, SANDBOX, ELSEWHERE, NOT_A_PATH, PERSON, AGENTS})
#: The start of a row whose fix is not merged yet.
TO_JUDGE = "to judge: "
#: A row that is not sound yet and that a lane left on the roadmap, in one
#: form only:
#: ``"<the audit's row or new>: accepted, the roadmap's entry '<its title>'"``.
#: It says the call is not sound yet and where its fix waits. It never says
#: why it would be sound.
ACCEPTED = "accepted, the roadmap's entry "


class Site(NamedTuple):
    reason: str
    calls: str
    why: str = ""


PROXY_SITES: dict[tuple[str, str], Site] = {
    ("../audio/providers/stt/relisten.py", "AudioRing.wav"): Site(
        NOT_A_PATH, "wave.open",
        "a WAV written into a BytesIO in memory, never a file"),
    ("admin_cli.py", "_ask_once"): Site(
        NOT_A_PATH, "Path.open",
        "urllib opener.open of a request to the local server, not a file"),
    ("admin_cli.py", "audit"): Site(
        OWN, "open",
        "the server's proxy.log beside the code and /proc/1/fd/2, appended when present"),
    ("api/agent_data/memory.py", "clear_all_endpoint"): Site(
        AGENTS, "Path.iterdir",
        "new: names only of the agent's users folder, which only the server writes"),
    ("api/agents/agents.py", "create_agent"): Site(
        AGENTS, "Path.mkdir Path.mkdir Path.mkdir Path.write_text shutil.rmtree shutil.rmtree",
        "A26: agent create, the new agent's folders made before any session can reach them"),
    ("api/agents/agents.py", "delete_agent"): Site(
        AGENTS, "shutil.rmtree",
        "A26: agent delete, an admin's rmtree of the agent folder, links inside not followed"),
    ("api/agents/conversations.py", "_export_zip"): Site(
        OWN, "tempfile.TemporaryFile zipfile.ZipFile",
        "an unnamed temp file under SESSIONS_DIR/zip-tmp, the zip writes to its handle"),
    ("api/agents/files.py", "_build_zip_file"): Site(
        OWN, "tempfile.TemporaryFile zipfile.ZipFile",
        "an unnamed temp file under SESSIONS_DIR/zip-tmp, the zip writes to its handle"),
    ("api/agents/files.py", "_init_blank_templates._zip"): Site(
        NOT_A_PATH, "zipfile.ZipFile",
        "a zip written into an in-memory BytesIO"),
    ("api/agents/files.py", "_rmdir_empty_sync"): Site(
        AGENTS, "os.rmdir",
        "A22: the files API delete, rmdir of one name on a held parent handle"),
    ("api/agents/files.py", "_tree_level"): Site(
        NOT_A_PATH, "os.scandir",
        "A10: lists the folder handle open_dir_beneath gave, one level of the files tree"),
    ("api/agents/files.py", "_zip_add_regular_file"): Site(
        NOT_A_PATH, "Path.open",
        "zf.open of an archive member, its source a no-follow descriptor"),
    ("api/agents/files.py", "_zip_tmp_dir"): Site(
        OWN, "os.makedirs",
        "makes SESSIONS_DIR/zip-tmp, the server's own scratch folder"),
    ("api/agents/outside.py", "knowledge_choices"): Site(
        AGENTS, "Path.iterdir",
        "A26: outside.py, names only of the agent's knowledge folders, links skipped"),
    ("api/apps/app_bindings.py", "_read_regular"): Site(
        AGENTS, "os.open os.open os.open",
        "C26: exports/{name}.json beneath the app's data folder, no-follow, regular, capped"),
    ("api/apps/app_proxy.py", "instance_for"): Site(
        NOT_A_PATH, "Path.touch",
        "Instance.touch sets an app process's activity time in memory, no file"),
    ("api/audio/audio.py", "_pcm_to_wav"): Site(NOT_A_PATH, "wave.open", "wave.open on an in-memory BytesIO"),
    ("api/auth/identity.py", "auth_callback_page"): Site(
        OWN, "FileResponse",
        "the dashboard build's index.html under DASHBOARD_DIST"),
    ("api/auth/openai_oauth.py", "_end_login"): Site(
        COPY, "shutil.rmtree",
        "a login's private CODEX_HOME the server made under its own home"),
    ("api/auth/openai_oauth.py", "_make_login_home"): Site(
        COPY, "Path.mkdir Path.mkdir os.chmod",
        "a login's private CODEX_HOME the server made under its own home"),
    ("api/auth/openai_oauth.py", "_read_login_blob"): Site(
        COPY, "Path.read_text",
        "auth.json in a login's private CODEX_HOME the server made"),
    ("api/auth/openai_oauth.py", "_remove_orphan_homes"): Site(
        COPY, "os.scandir shutil.rmtree",
        "the server's own ~/.codex-logins of private login homes, entries not followed"),
    ("api/auth/openai_oauth.py", "_whole_login_file"): Site(
        COPY, "Path.read_text",
        "auth.json in a login's private CODEX_HOME the server made"),
    ("api/auth/openai_oauth.py", "oauth_finish"): Site(
        COPY, "shutil.rmtree",
        "a login's private CODEX_HOME the server made under its own home"),
    ("api/cards/actions_team.py", "_share_run"): Site(
        COPY, "shutil.rmtree",
        "the private mkdtemp folder read_bundle unpacked the app bundle into"),
    ("api/hooks/app_deploy.py", "hook_app_import"): Site(
        COPY, "shutil.rmtree",
        "the private mkdtemp folder read_bundle unpacked the app bundle into"),
    ("api/mcp/local_templates.py", "_check_examples"): Site(
        OWN, "Path.iterdir Path.iterdir Path.read_text",
        "the shipped checks-mcp example folders under MCPS_DIR/custom"),
    ("api/mcp/local_templates.py", "_load_snapshot"): Site(
        COPY, "shutil.rmtree shutil.rmtree",
        "removes the template's private mkdtemp snapshot"),
    ("api/mcp/local_templates.py", "_pull_remote_snapshot"): Site(
        COPY, "shutil.rmtree",
        "removes its own private mkdtemp folder"),
    ("api/mcp/local_templates.py", "_pull_remote_snapshot._worker"): Site(
        COPY, "Path.mkdir",
        "folders inside its private mkdtemp, the names checked canonical first"),
    ("api/mcp/local_templates.py", "_snapshot_template_dir"): Site(
        AGENTS, "Path.mkdir Path.write_bytes os.fwalk os.open shutil.rmtree",
        "C2: os.fwalk enters no link, its top checked as the folder opened strictly, files opened O_NOFOLLOW from each handle into a private mkdtemp"),
    ("api/mcp/local_templates.py", "_validate_apps"): Site(
        COPY, "Path.read_text",
        "app.json inside the template's private mkdtemp snapshot"),
    ("api/mcp/local_templates.py", "install_from_local_template"): Site(
        COPY, "shutil.rmtree",
        "removes the template's private mkdtemp snapshot"),
    ("api/mcp/local_templates.py", "validate_local_package"): Site(
        COPY, "shutil.rmtree",
        "removes the template's private mkdtemp snapshot"),
    ("api/mcp/local_templates.py", "validate_local_template"): Site(
        COPY, "shutil.rmtree",
        "removes the template's private mkdtemp snapshot"),
    ("api/mcp/mcps.py", "_delete_mcp_locked"): Site(
        OWN, "shutil.rmtree",
        "an installed community tool or skill folder under MCPS_DIR, an admin's delete"),
    ("api/mcp/mcps.py", "_extract_upload_zip"): Site(
        COPY, "Archive.extractall Path.write_bytes zipfile.ZipFile",
        "the admin's upload zip, sizes and names checked, extracted inside a private mkdtemp folder"),
    ("api/mcp/mcps.py", "_zip_root_with"): Site(
        COPY, "Path.iterdir",
        "the extracted upload inside a private mkdtemp folder"),
    ("api/mcp/mcps.py", "delete_ssh_key"): Site(
        OWN, "Path.unlink",
        "the ssh-hosts tool's keys under MCPS_DIR, an admin-only folder"),
    ("api/mcp/mcps.py", "install_mcp"): Site(
        COPY, "Path.read_text shutil.rmtree",
        "manifest.json of the upload and its private mkdtemp folder"),
    ("api/mcp/mcps.py", "install_skill_zip"): Site(COPY, "shutil.rmtree", "removes its private mkdtemp upload folder"),
    ("api/mcp/mcps.py", "list_ssh_keys"): Site(
        OWN, "Path.iterdir Path.mkdir",
        "the ssh-hosts tool's keys under MCPS_DIR, an admin-only folder"),
    ("api/mcp/mcps.py", "upload_ssh_key"): Site(
        OWN, "Path.chmod Path.mkdir Path.write_bytes",
        "the ssh-hosts tool's keys under MCPS_DIR, an admin-only folder"),
    ("api/media/document_downloads.py", "_copy_fd"): Site(
        OWN, "open",
        "a prepared download under PLATFORM_DATA_DIR/host-media-cache/converted"),
    ("api/media/document_downloads.py", "_prepare"): Site(
        OWN, "Path.mkdir shutil.rmtree shutil.rmtree",
        "a prepared download under PLATFORM_DATA_DIR/host-media-cache/converted"),
    ("api/media/document_downloads.py", "_prune"): Site(
        OWN, "Path.iterdir shutil.rmtree",
        "a prepared download under PLATFORM_DATA_DIR/host-media-cache/converted"),
    ("api/media/document_downloads.py", "_save_answer"): Site(
        OWN, "open",
        "a prepared download under PLATFORM_DATA_DIR/host-media-cache/converted"),
    ("api/media/document_downloads.py", "forget"): Site(
        OWN, "shutil.rmtree",
        "a prepared download under PLATFORM_DATA_DIR/host-media-cache/converted"),
    ("api/media/media.py", "FdFileResponse"): Site(
        NOT_A_PATH, "FileResponse",
        "a response class that serves an open descriptor, never a name"),
    ("api/media/uploads.py", "_admit_and_stage"): Site(
        OWN, "Path.mkdir os.open",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_count_open_total"): Site(
        OWN, "Path.iterdir Path.iterdir",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_finalize_staged_file"): Site(
        OWN, "Path.unlink",
        "the upload's meta file under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_gone"): Site(OWN, "Path.unlink", "the upload's meta file under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_open_chunk_target"): Site(
        OWN, "os.open",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_read_chunk_meta"): Site(
        OWN, "Path.read_text",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_remove_pair"): Site(
        OWN, "Path.unlink Path.unlink",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "_save_chunk_meta_atomic"): Site(
        OWN, "Path.write_text os.replace",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "sweep_stale_staging"): Site(
        OWN, "Path.iterdir",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/media/uploads.py", "sweep_stale_staging._reap_dir"): Site(
        OWN, "Path.iterdir Path.read_text Path.unlink",
        "the chunked upload's staging under UPLOAD_STAGING_DIR"),
    ("api/public/me.py", "command"): Site(
        OWN, "FileResponse",
        "the repository's shipped satellite/terminal/otodock_api.py"),
    ("api/public/me.py", "description"): Site(OWN, "FileResponse", "the shipped openapi.json beside the module"),
    ("api/remote/remote_machines.py", "_build_bootstrap_script"): Site(
        OWN, "Path.read_bytes Path.read_bytes Path.read_text",
        "the repository's shipped satellite install templates and scripts"),
    ("api/remote/remote_machines.py", "_discover_satellite_package_files"): Site(
        OWN, "Path.rglob",
        "the repository's shipped satellite source tree"),
    ("api/settings/catalog.py", "_load"): Site(OWN, "Path.open", "the shipped settings catalog.json beside the module"),
    ("app.py", "dashboard_legacy_redirect"): Site(
        OWN, "FileResponse FileResponse",
        "the dashboard build under DASHBOARD_DIST, confined to it"),
    ("app.py", "spa_catchall"): Site(OWN, "FileResponse", "the dashboard build under DASHBOARD_DIST, confined to it"),
    ("auth/lan_check.py", "_docker_gateway"): Site(OWN, "open", "/proc/net/route"),
    ("compute_start.py", "_pasta_floor"): Site(
        OWN, "Path.read_text",
        "a compute machine's start, before any session: the shipped VERSIONS.md"),
    ("compute_start.py", "_status"): Site(
        OWN, "Path.mkdir Path.write_text os.replace",
        "a compute machine's start: its own status file in its own HOME"),
    ("compute_start.py", "check"): Site(
        OWN, "Path.mkdir",
        "the install's check of a compute machine: a scratch folder in its own HOME"),
    ("compute_start.py", "community_problem"): Site(
        OWN, "Path.mkdir",
        "a compute machine's start: its own community tools volume, only the installer writes it"),
    ("compute_start.py", "main"): Site(
        AGENTS, "Path.mkdir Path.mkdir",
        "new: a compute machine's start, before any session: makes the agents and tools roots of its own copy, nothing below"),
    ("compute_start.py", "marker_problem"): Site(
        AGENTS, "Path.read_text",
        "new: a compute machine's start, before any session: the agents root's marker, which only the proxy writes, read and compared, never shown"),
    ("compute_start.py", "wait_for_credential"): Site(
        OWN, "Path.read_text",
        "a compute machine's start: the credential in its link folder, which only the proxy writes"),
    ("compute_start.py", "wait_for_new_sign_in"): Site(
        OWN, "Path.read_text Path.unlink",
        "a compute machine's start: its own refused sign-in mark in its HOME"),
    ("compute_start.py", "write_conf"): Site(
        OWN, "Path.mkdir os.open os.replace",
        "a compute machine's start: its satellite.conf in its own HOME"),
    ("config.py", "<module>"): Site(OWN, "Path.mkdir Path.mkdir", "makes SESSIONS_DIR and the shipped hooks folder"),
    ("config.py", "_persist_secret"): Site(OWN, "Path.read_text", "the platform's config.env"),
    ("config.py", "_read_pinned_version"): Site(OWN, "Path.read_text", "the shipped VERSIONS.md"),
    ("config.py", "_set_config_line"): Site(OWN, "Path.read_text", "the platform's config.env"),
    ("config.py", "_tighten_config_mode"): Site(OWN, "os.chmod", "the platform's config.env"),
    ("config.py", "_write_config_env"): Site(OWN, "os.open", "the platform's config.env"),
    ("configuration/prompts.py", "_scan_tree._walk"): Site(
        NOT_A_PATH, "os.scandir",
        "F1: lists a folder handle open_dir_beneath gave beneath the agent's real folder, a link listed by name and never entered"),
    ("configuration/prompts.py", "build_agent_prompt"): Site(
        AGENTS, "Path.mkdir",
        "F1: makes users/{u}/workspace and context, whose parent only the server writes, every read of the prompt goes beneath the agent's handle"),
    ("core/agent_folder.py", "agent_holding"): Site(
        AGENTS, "os.scandir",
        "new: the one owner of an agent's folder: lists the agents folder's top names only, a link entry compared on its text, nothing opened"),
    ("core/concurrency_scan.py", "_stdio_counts"): Site(
        OWN, "open",
        "the session's generated tool config under SESSIONS_DIR/user-mcp-configs (E12)"),
    ("core/credentials/credential_exit.py", "retire_platform_authority"): Site(
        OWN, "Path.rmdir Path.unlink os.scandir",
        "the old authority's folder under the platform data folder: two named files, its leaves' key and certificate files (no link followed) and the emptied folders"),
    ("core/credentials/exit_authority.py", "_holds"): Site(
        OWN, "Path.read_bytes",
        "on a machine, its own trust bundle in its state folder, a session's bundle is written by write_if_changed"),
    ("core/credentials/exit_authority.py", "_load_chain"): Site(
        OWN, "os.scandir os.unlink os.unlink",
        "the exit's private leaves folder under the platform data folder"),
    ("core/credentials/exit_authority.py", "_read_authority"): Site(
        OWN, "Path.read_bytes Path.read_bytes",
        "ca.key and ca.pem in the exit's private folder under the platform data folder"),
    ("core/credentials/exit_authority.py", "_replace"): Site(
        AGENTS, "os.replace os.unlink",
        "new: O_EXCL no-follow temp beside the trust bundle renamed onto the name, never followed"),
    ("core/credentials/exit_authority.py", "_write_new"): Site(
        AGENTS, "os.chmod os.open os.unlink",
        "new: O_EXCL O_NOFOLLOW create of a random temp name, the mode set on the descriptor"),
    ("core/credentials/exit_authority.py", "private_dir"): Site(
        OWN, "Path.chmod Path.mkdir",
        "the exit authority's private folder: a machine's under its OtoDock folder (the satellite's copy), a test's own"),
    ("core/credentials/exit_authority.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/credentials/exit_authority.py", "system_bundle_text"): Site(
        OWN, "Path.read_text",
        "the system CA bundle of the image, or certifi's copy"),
    ("core/credentials/exit_relay.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/credentials/mcp_gateway.py", "descriptor_dir"): Site(
        OWN, "Path.mkdir",
        "SESSIONS_DIR/mcp-gateway, the gateway descriptors"),
    ("core/credentials/mcp_gateway.py", "read_descriptor"): Site(
        OWN, "Path.read_text",
        "a session's descriptor under SESSIONS_DIR/mcp-gateway"),
    ("core/credentials/mcp_gateway.py", "remove_descriptor"): Site(
        OWN, "Path.unlink",
        "a session's descriptor under SESSIONS_DIR/mcp-gateway"),
    ("core/credentials/mcp_gateway.py", "write_descriptor"): Site(
        OWN, "os.open os.replace",
        "a session's descriptor and its temp under SESSIONS_DIR/mcp-gateway"),
    ("core/credentials/ssh_signer.py", "_bind"): Site(
        OWN, "Path.mkdir os.chmod os.chmod os.unlink",
        "SESSIONS_DIR/ssh-agent, a socket an earlier process left there, and the socket the signer binds"),
    ("core/credentials/ssh_signer.py", "_load_key"): Site(
        OWN, "Path.read_bytes",
        "an ssh-hosts key in the installed tool's keys folder, admin-only, never mounted"),
    ("core/credentials/ssh_signer.py", "_unbind"): Site(
        OWN, "os.unlink",
        "the session's agent socket under SESSIONS_DIR/ssh-agent, removed while it is the one the signer bound"),
    ("core/credentials/ssh_signer.py", "sweep_at_boot"): Site(
        OWN, "Path.glob Path.unlink",
        "leftover sockets under SESSIONS_DIR/ssh-agent"),
    ("core/layers/cli/layer.py", "CLIExecutionLayer._legacy_file_has_user_message"): Site(
        OWN, "Path.open",
        "the legacy transcript in the proxy account's own home (~/.claude), which no session writes"),
    ("core/layers/cli/layer.py", "CLIExecutionLayer.iter_session_files"): Site(
        AGENTS, "Path.glob Path.glob",
        "new: names only from .claude/projects, glob may enter a linked dir, removals via safe_fs"),
    ("core/layers/cli/layer.py", "_store_settings"): Site(
        AGENTS, "Path.read_bytes",
        "new: raw only for a store in no agent's folder, which no running server passes, an agent's store is read beneath agent_root, capped"),
    ("core/layers/cli/layer.py", "expect_terminal_tools"): Site(
        OWN, "Path.read_text",
        "the session's generated tool config under SESSIONS_DIR"),
    ("core/layers/cli/remote.py", "ClaudeRemoteAdapter.start_payload"): Site(
        OWN, "Path.read_text",
        "the session's generated tool config under SESSIONS_DIR"),
    ("core/layers/cli/spawn.py", "ClaudeSpawn._build_persistent_cmd"): Site(
        OWN, "Path.mkdir",
        "SESSIONS_DIR/prompt-files, the prompt fallback folder"),
    ("core/layers/codex/app_server_client.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/layers/codex/codex_approvals.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/layers/codex/local_model_catalog.py", "base_instructions"): Site(
        OWN, "Path.read_text",
        "codex_base_instructions.md shipped beside the module"),
    ("core/layers/codex/remote.py", "CodexRemoteAdapter.start_payload"): Site(
        OWN, "Path.read_text",
        "the session's generated tool TOML under SESSIONS_DIR"),
    ("core/layers/codex/resume.py", "_CodexResume.chat_session_files"): Site(
        AGENTS, "Path.rglob",
        "new: names only under .codex/sessions (a link at sessions itself is listed through, none below), opens and removals via safe_fs"),
    ("core/layers/codex/resume.py", "_CodexResume.iter_session_files"): Site(
        AGENTS, "Path.rglob",
        "new: names only under .codex/sessions (a link at sessions itself is listed through, none below), opens and removals via safe_fs"),
    ("core/layers/codex/session_text.py", "_in_home"): Site(
        AGENTS, "os.open os.open os.unlink",
        "new: opens the Codex home O_DIRECTORY O_NOFOLLOW, the other two are names in a support check, every name is reached from that handle"),
    ("core/layers/codex/session_text.py", "drop"): Site(
        AGENTS, "os.unlink",
        "E7: the session's files removed by name from the home's no-follow handle, by path only on Windows, a paired computer"),
    ("core/layers/codex/session_text.py", "read_session"): Site(
        AGENTS, "os.open",
        "new: on a machine (the satellite's copy), one name directly in the Codex home, O_NOFOLLOW, non-blocking, regular checked"),
    ("core/layers/codex/session_text.py", "remove_shared_text"): Site(
        AGENTS, "os.unlink",
        "E7: removed by name from the home's no-follow handle after a no-follow stat, by path only on Windows, a paired computer"),
    ("core/layers/codex/session_text.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/layers/codex/session_text.py", "write_session_files"): Site(
        AGENTS, "Path.mkdir",
        "E7: mkdir of the scope's checked .codex, the files go through write_if_changed"),
    ("core/layers/codex/spawn.py", "_CodexSpawn._prepare"): Site(
        OWN, "Path.read_text",
        "the session's generated tool TOML under SESSIONS_DIR"),
    ("core/layers/direct/file_program.py", "_open_existing"): Site(
        SANDBOX, "os.open",
        "the file program, in a session's sandbox: an existing name opened for writing in a folder's "
        "descriptor, no link followed, never waited on, nothing cut"),
    ("core/layers/direct/file_program.py", "_open_folder"): Site(
        SANDBOX, "os.mkdir os.open",
        "the file program runs in a session's sandbox, never in the server: a folder reached from the root "
        "part by part, no link followed, a missing part of a write's path made in its parent's descriptor"),
    ("core/layers/direct/file_program.py", "_skills"): Site(
        SANDBOX, "os.scandir",
        "the file program, in a session's sandbox: lists the descriptor of the session's skills folder"),
    ("core/layers/direct/file_program.py", "_step"): Site(
        SANDBOX, "os.open",
        "the file program, in a session's sandbox: one name opened in a folder's descriptor, no link "
        "followed, never waited on"),
    ("core/layers/direct/file_program.py", "_walk"): Site(
        SANDBOX, "os.scandir",
        "the file program, in a session's sandbox: lists a folder's descriptor and enters no link"),
    ("core/layers/direct/file_program.py", "_write_whole"): Site(
        SANDBOX, "os.open os.rename os.unlink",
        "the file program, in a session's sandbox: a temp file made exclusively beside the name in the "
        "folder's descriptor, no link followed, moved over the name there or removed"),
    ("core/layers/direct/files.py", "<module>"): Site(
        OWN, "Path.read_text",
        "the file program's own source beside this module, shipped with the server"),
    ("core/layers/direct/layer.py", "DirectLLMExecutionLayer._send_turn"): Site(
        NOT_A_PATH, "Path.touch",
        "DirectSession.touch sets last_activity in memory, no file"),
    ("core/layers/direct/mcp.py", "AgentMCPManager._read_config"): Site(
        OWN, "Path.read_text",
        "the session's generated tool config under SESSIONS_DIR (E12)"),
    ("core/layers/direct/sandbox_program.py", "<module>"): Site(
        OWN, "Path.read_text",
        "the general loop's own source beside this module, shipped with the server"),
    ("core/layers/direct/session.py", "run_direct_stream"): Site(
        NOT_A_PATH, "Path.touch Path.touch",
        "DirectSession.touch sets last_activity in memory, no file"),
    ("core/layout.py", "self_hash"): Site(OWN, "open", "its own module source, hashed for the satellite's drift check"),
    ("core/log_queue.py", "_PrivateRotatingFileHandler"): Site(
        OWN, "logging.handlers.RotatingFileHandler",
        "the server's own log file, a handler class built on the rotating one"),
    ("core/log_queue.py", "_PrivateRotatingFileHandler._open"): Site(
        OWN, "open os.open",
        "the server's own proxy.log, then open() on that descriptor"),
    ("core/log_queue.py", "configure"): Site(OWN, "os.chmod", "the server's own proxy.log"),
    ("core/loop_watchdog.py", "_sample_fds"): Site(OWN, "os.listdir", "/proc/self/fd"),
    ("core/loop_watchdog.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/remote/file_sync.py", "_hash_file"): Site(
        AGENTS, "os.open",
        "F22: hashes a descriptor's path or a file the caller made, non-blocking, regular checked"),
    ("core/remote/file_sync.py", "_manifest_beneath"): Site(
        AGENTS, "os.walk",
        "F22: the walk lists names, each folder it enters is checked beneath the agent's handle, every file opened beneath it, regular and capped"),
    ("core/remote/file_sync.py", "_prune_empty_parents_beneath"): Site(
        AGENTS, "os.rmdir",
        "B10: rmdir by name on a handle opened beneath the agent's folder"),
    ("core/remote/file_sync.py", "_unlink_quiet"): Site(
        AGENTS, "Path.unlink",
        "new: no caller in the server, unlink of one name never follows it"),
    ("core/remote/file_sync.py", "prune_empty_parents"): Site(
        AGENTS, "Path.rmdir",
        "new: no caller in the server, rmdir only of empty folders inside the resolved root"),
    ("core/remote/remote_start_payload.py", "_load_hook_scripts"): Site(
        OWN, "Path.read_text",
        "the shipped hook scripts in proxy/hooks"),
    ("core/remote/remote_workspace_apply.py", "RemoteWorkspaceApplyMixin._sync_apply"): Site(
        COPY, "shutil.rmtree",
        "the merge's recovery folder made by tempfile.mkdtemp"),
    ("core/remote/remote_workspace_apply.py", "RemoteWorkspaceApplyMixin._sync_pull_bytes"): Site(
        COPY, "Path.read_bytes Path.unlink",
        "a pulled file in the merge's mkdtemp recovery folder"),
    ("core/remote/remote_workspace_apply.py", "_keep_mtime"): Site(
        NOT_A_PATH, "os.utime",
        "utime on a descriptor safe_fs opened beneath AGENTS_DIR (B13)"),
    ("core/remote/satellite_file_verbs.py", "SatelliteFileVerbsMixin._open_pull_root"): Site(
        OWN, "Path.mkdir",
        "a pull's parent outside AGENTS_DIR, the host-media cache or a private temp (B12)"),
    ("core/remote/satellite_file_verbs.py", "SatelliteFileVerbsMixin.push_file"): Site(
        AGENTS, "open",
        "F18: opens only an open descriptor's path (safe_fs.fd_path), any other source is refused"),
    ("core/remote/satellite_push_run.py", "_push_read_chunk"): Site(
        AGENTS, "Path.read_bytes",
        "F18: reads only the descriptor's path push_file was given, push_file refuses any other source"),
    ("core/remote/session_files.py", "sweep_staging"): Site(
        AGENTS, "os.listdir",
        "B17: lists agent names in AGENTS_DIR, each removal goes through safe_fs"),
    ("core/sandbox/host_resources.py", "_own_cgroup_dirs"): Site(OWN, "open", "/proc/self/cgroup"),
    ("core/sandbox/host_resources.py", "_proc_meminfo_field"): Site(OWN, "open", "/proc/meminfo"),
    ("core/sandbox/host_resources.py", "_read_int"): Site(
        OWN, "open",
        "the process's cgroup memory files under /sys/fs/cgroup"),
    ("core/sandbox/host_resources.py", "_read_memory_stat"): Site(
        OWN, "open",
        "the process's cgroup memory.stat under /sys/fs/cgroup"),
    ("core/sandbox/host_resources.py", "_v2_value"): Site(
        OWN, "open",
        "the process's cgroup v2 limit files under /sys/fs/cgroup"),
    ("core/sandbox/host_resources.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/sandbox/sandbox.py", "SandboxBuilder._home_parts"): Site(
        AGENTS, "Path.iterdir",
        "D17: lists the names in users/ only, linked entries skipped"),
    ("core/sandbox/sandbox.py", "_mkdirs_held"): Site(
        AGENTS, "Path.mkdir",
        "D17: mkdir of the agent's resolved folder, the chain below via safe_fs"),
    ("core/sandbox/sandbox.py", "empty_mount_dir"): Site(OWN, "Path.mkdir", "SESSIONS_DIR/.empty, the mask source"),
    ("core/sandbox/sandbox.py", "mcp_dir_binds_from_config"): Site(
        OWN, "Path.read_text",
        "the pre-sandbox tool config under SESSIONS_DIR"),
    ("core/sandbox/sandbox_render.py", "_reads"): Site(
        OWN, "open",
        "the kernel's user namespace switches under /proc/sys"),
    ("core/sandbox/sandbox_render.py", "_sign_in_mount_point"): Site(
        AGENTS, "os.open",
        "new: on a machine, an O_EXCL O_NOFOLLOW create of the store's sign-in mount point, anything else at the name refused"),
    ("core/sandbox/sandbox_render.py", "build_data_dirs"): Site(
        OWN, "open",
        "on a machine, a tool's manifest in its tools folder, which only the installer writes"),
    ("core/sandbox/sandbox_render.py", "home_args"): Site(
        AGENTS, "os.mkdir",
        "new: on a machine, own/ made beneath a scope's .claude checked to be no link, a link at the name refused"),
    ("core/sandbox/sandbox_render.py", "host_resolv_has_loopback_ns"): Site(OWN, "Path.read_text", "/etc/resolv.conf"),
    ("core/sandbox/sandbox_render.py", "mem_total_mb"): Site(OWN, "open", "/proc/meminfo"),
    ("core/sandbox/sandbox_render.py", "prepare_build_step"): Site(
        OWN, "open os.makedirs os.makedirs",
        "on a machine, a tool build's outputs in the tool's folder and its private scratch"),
    ("core/sandbox/sandbox_render.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/sandbox/session_config_dir.py", "_a_home_may_mask._names"): Site(
        NOT_A_PATH, "os.scandir",
        "lists a folder handle open_dir_beneath gave beneath agent_root, names only"),
    ("core/sandbox/session_config_dir.py", "_clear_wrapper_dir"): Site(
        NOT_A_PATH, "os.scandir",
        "new: lists the wrappers' folder handle beneath agent_root, removals through safe_fs on that handle"),
    ("core/sandbox/session_config_dir.py", "_copy_hook_lf"): Site(
        OWN, "Path.read_bytes",
        "the shipped hook scripts and interceptor source in the proxy's code folder"),
    ("core/sandbox/session_config_dir.py", "_holds"): Site(
        AGENTS, "os.open",
        "new: O_NOFOLLOW O_NONBLOCK open of a config dir name, judged on the descriptor"),
    ("core/sandbox/session_config_dir.py", "_open_folder"): Site(
        AGENTS, "os.open",
        "new: raw only for a folder in no agent's folder, which no running server passes, O_NOFOLLOW at its name, in an agent's folder open_dir_beneath"),
    ("core/sandbox/session_config_dir.py", "_session_copy_path"): Site(
        NOT_A_PATH, "os.scandir",
        "lists the config folder's handle open_dir_beneath gave beneath agent_root, removals through unlink_beneath"),
    ("core/sandbox/session_config_dir.py", "_sweep_stale_temps"): Site(
        AGENTS, "os.scandir os.unlink",
        "new: unlinks only stale write temps by name in the config dir, never followed"),
    ("core/sandbox/session_config_dir.py", "_verified_session_dir"): Site(
        AGENTS, "Path.mkdir",
        "new: mkdir of the agent's resolved folder, the chain below via safe_fs"),
    ("core/sandbox/session_config_dir.py", "_write_if_changed"): Site(
        AGENTS, "os.open os.replace os.unlink os.unlink",
        "new: an O_EXCL O_NOFOLLOW temp and its rename, each a name in the held folder's handle (dir_fd), never a path"),
    ("core/sandbox/session_config_dir.py", "make_session_home"): Site(
        NOT_A_PATH, "os.utime",
        "the home's own handle from _open_folder, its time set on the descriptor"),
    ("core/sandbox/session_config_dir.py", "prepare_mcp_config_for_sandbox"): Site(
        OWN, "Path.read_bytes Path.read_text",
        "reads the session's generated tool config in SESSIONS_DIR, the copy is written beneath agent_root"),
    ("core/sandbox/session_config_dir.py", "sweep_private_ssh_keys"): Site(
        AGENTS, "os.scandir os.scandir",
        "F21: lists the agents folder's names, then each key folder from a handle beneath agent_root, removals through safe_fs"),
    ("core/sandbox/session_config_dir.py", "sweep_private_ssh_keys._folders"): Site(
        NOT_A_PATH, "os.scandir",
        "F21: lists a folder handle open_dir_beneath gave beneath agent_root, folders only"),
    ("core/sandbox/session_config_dir.py", "unlink_in"): Site(
        AGENTS, "os.unlink",
        "new: one name removed from the folder handle _open_folder gave (dir_fd), a link at the name goes, never its target"),
    ("core/sandbox/session_config_dir.py", "write_command_wrappers"): Site(
        AGENTS, "Path.mkdir Path.unlink os.scandir os.unlink shutil.rmtree",
        "new: raw only for a folder in no agent's folder, which no running server passes, in an agent's folder _clear_wrapper_dir beneath agent_root"),
    ("core/sandbox/session_config_dir.py", "write_no_follow"): Site(
        AGENTS, "Path.unlink os.open",
        "new: raw only for a path in no agent's folder, which no running server passes, O_NOFOLLOW at the name, in an agent's folder written in place beneath agent_root"),
    ("core/sandbox/session_home.py", "_FdOps._clear_files"): Site(
        AGENTS, "os.listdir os.unlink",
        "D16: listdir and unlink by name on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._drop_in"): Site(
        AGENTS, "os.rmdir os.unlink os.unlink",
        "D16: removal by name on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._empty"): Site(
        AGENTS, "os.rmdir",
        "D16: removal by name on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._link"): Site(
        AGENTS, "os.rename os.symlink os.unlink",
        "new: symlink, rename and unlink by name on held no-follow folder descriptors"),
    ("core/sandbox/session_home.py", "_FdOps._merge"): Site(
        AGENTS, "os.listdir os.rename os.rmdir",
        "new: listdir, rename and rmdir by name on held no-follow folder descriptors"),
    ("core/sandbox/session_home.py", "_FdOps._mkdir_open"): Site(
        AGENTS, "os.mkdir",
        "new: mkdir by name on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._move_into"): Site(
        AGENTS, "os.rmdir",
        "new: rmdir by name on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._open_dir"): Site(
        AGENTS, "os.open",
        "D16: O_DIRECTORY O_NOFOLLOW open, a name beneath a held descriptor"),
    ("core/sandbox/session_home.py", "_FdOps._seed"): Site(
        AGENTS, "os.open os.open os.rename",
        "D11: the seed opened O_NOFOLLOW O_NONBLOCK from the store's handle, regular and capped, written by an O_EXCL temp and a rename in the home's handle"),
    ("core/sandbox/session_home.py", "_FdOps.sweep"): Site(
        AGENTS, "os.listdir",
        "D16: listdir of own/ on a held no-follow folder descriptor"),
    ("core/sandbox/session_home.py", "_PathOps._drop_at"): Site(
        ELSEWHERE, "os.rmdir",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps._empty"): Site(
        ELSEWHERE, "os.listdir os.rmdir os.unlink",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps._make_link"): Site(
        ELSEWHERE, "os.symlink",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps._merge"): Site(
        ELSEWHERE, "os.listdir os.rename os.rmdir",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps._real_dir"): Site(
        ELSEWHERE, "os.chmod os.mkdir",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps._remove_link"): Site(
        ELSEWHERE, "os.rmdir os.unlink",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps.prepare"): Site(
        ELSEWHERE, "Path.write_bytes open os.rmdir os.unlink",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "_PathOps.sweep"): Site(
        ELSEWHERE, "os.listdir",
        "Windows-only path walk, runs only in a Windows machine's vendored copy"),
    ("core/sandbox/session_home.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/sandbox/skills_materializer.py", "_lock_path"): Site(
        OWN, "os.makedirs",
        "the skills lock folder under SESSIONS_DIR"),
    ("core/sandbox/skills_materializer.py", "_materialize_locked"): Site(
        OWN, "os.open",
        "a config dir's lock file under SESSIONS_DIR"),
    ("core/session/codex_rollout_tailer.py", "resolve_rollout_path"): Site(
        AGENTS, "glob.glob",
        "D13: accepted, the roadmap's entry 'Codex's local rollout tailer is unreachable'"),
    ("core/session/codex_rollout_tailer.py", "seek_past_existing"): Site(
        AGENTS, "open",
        "D13: accepted, the roadmap's entry 'Codex's local rollout tailer is unreachable'"),
    ("core/session/codex_rollout_tailer.py", "tail_rollout"): Site(
        AGENTS, "open",
        "D13: accepted, the roadmap's entry 'Codex's local rollout tailer is unreachable'"),
    ("core/session/external_identity.py", "touch_home"): Site(
        NOT_A_PATH, "os.utime",
        "utime on a descriptor safe_fs opened beneath the agent's real folder"),
    ("core/session/session_state.py", "_load_sessions"): Site(OWN, "Path.read_text", "SESSIONS_DIR/index.json"),
    ("core/session/session_state.py", "_save_pending_result"): Site(
        OWN, "Path.write_text",
        "a pending result file under SESSIONS_DIR"),
    ("core/session/session_state.py", "get_pending_result"): Site(
        OWN, "Path.read_text Path.unlink",
        "a pending result file under SESSIONS_DIR"),
    ("core/session/session_state.py", "load_session_security"): Site(
        OWN, "Path.read_text",
        "SESSIONS_DIR/security_index.json"),
    ("core/session/start_context.py", "_read_file"): Site(
        OWN, "Path.read_text",
        "the held start-context file under SESSIONS_DIR"),
    ("core/session/transcript_tailer.py", "_on_tool_use"): Site(
        NOT_A_PATH, "Path.open",
        "ToolEventBuffer.open records a pending tool block, no file"),
    ("core/stdio_path_interceptor.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("core/terminal_queries.py", "self_hash"): Site(
        OWN, "open",
        "its own module source, hashed for the satellite's drift check"),
    ("hooks/credential_helper.py", "_own_login"): Site(
        SANDBOX, "open",
        "git's credential helper in the session, gh's hosts file in the session's home"),
    ("hooks/permission_gate.py", "_log_failure"): Site(
        SANDBOX, "open os.makedirs",
        "the hook's crash log in the session's own home"),
    ("hooks/session_text.py", "_open"): Site(
        SANDBOX, "os.open",
        "Codex's SessionStart hook reading its own CODEX_HOME, no-follow"),
    ("hooks/session_text.py", "_write"): Site(
        SANDBOX, "os.open os.replace",
        "Codex's SessionStart hook writing its record in its own CODEX_HOME"),
    ("hooks/start_context.py", "main"): Site(
        NOT_A_PATH, "Path.open",
        "urllib opener.open of a request to PROXY_URL, not a file"),
    ("hooks/usage_report.py", "main"): Site(
        SANDBOX, "open os.open",
        "the usage hook's last-report note in the session's temp folder"),
    ("services/agents/offboarding_transfer.py", "_agent_dirs"): Site(
        AGENTS, "os.scandir",
        "D15: names of AGENTS_DIR only, entries not followed, the archive moves are safe"),
    ("services/agents/repository_instructions.py", "_folders"): Site(
        AGENTS, "os.scandir",
        "C26: lists a held directory descriptor, links left out"),
    ("services/apps/app_blueprints.py", "read_bundle"): Site(
        COPY, "shutil.rmtree shutil.rmtree shutil.rmtree shutil.rmtree shutil.rmtree",
        "C11: removes its own mkdtemp folder at each refusal, the bundle is unpacked into it through safe_fs, capped"),
    ("services/apps/app_db.py", "_copy_once"): Site(
        AGENTS, "sqlite3.connect sqlite3.connect",
        "F20: SQLite opens names in a private folder this call made exclusively beneath agent_root, filled by hard links from held handles"),
    ("services/apps/app_db.py", "_link_live"): Site(
        AGENTS, "os.link",
        "F20: links each live name from the data folder's handle into the private folder's handle, no link followed"),
    ("services/apps/app_follow.py", "_put"): Site(
        COPY, "shutil.rmtree",
        "the bundle's own private mkdtemp extract, removed after its import"),
    ("services/apps/app_lint_brand.py", "kit_values"): Site(
        OWN, "Path.read_text",
        "the ui-kit stylesheet in the dashboard build the image ships"),
    ("services/apps/app_supervisor.py", "ensure_up"): Site(
        NOT_A_PATH, "Path.touch",
        "Instance.touch, an in-memory activity time, no file"),
    ("services/apps/app_supervisor.py", "start"): Site(
        NOT_A_PATH, "Path.touch Path.touch",
        "Instance.touch, an app instance's last activity time in memory, no file"),
    ("services/apps/app_supervisor.py", "touch"): Site(
        NOT_A_PATH, "Path.touch",
        "Instance.touch, an in-memory activity time, no file"),
    ("services/apps/app_supervisor.py", "ws_closed"): Site(
        NOT_A_PATH, "Path.touch",
        "Instance.touch, an in-memory activity time, no file"),
    ("services/apps/app_supervisor.py", "ws_opened"): Site(
        NOT_A_PATH, "Path.touch",
        "Instance.touch, an in-memory activity time, no file"),
    ("services/apps/catalog.py", "_list_app_folder"): Site(
        NOT_A_PATH, "os.scandir",
        "C18: lists the authorized folder's handle open_dir_beneath gave beneath agent_root, capped"),
    ("services/apps/viewer_data.py", "remove"): Site(
        AGENTS, "shutil.rmtree",
        "C26: the viewers folder beside app-data, only the server writes it"),
    ("services/apps/viewer_data.py", "write"): Site(
        AGENTS, "Path.iterdir",
        "C26: counts files in the viewers folder, names only, only the server writes it"),
    ("services/community/community_agent_installer.py", "_create_agent_folder"): Site(
        AGENTS, "Path.mkdir Path.mkdir Path.mkdir Path.write_text",
        "new: the new agent's folder, made before its record exists, no session reaches it yet"),
    ("services/community/community_agent_installer.py", "install_from_catalog"): Site(
        COPY, "shutil.rmtree",
        "removes the catalog template's private mkdtemp extract"),
    ("services/community/community_agent_installer.py", "install_from_extracted_template"): Site(
        AGENTS, "shutil.rmtree shutil.rmtree",
        "new: removes the folder this install made before its record exists, rmtree follows no link"),
    ("services/community/community_agent_template_store.py", "_discover_apps"): Site(
        COPY, "Path.iterdir",
        "apps/ of a template in a private mkdtemp copy (catalog extract or local snapshot)"),
    ("services/community/community_agent_template_store.py", "_discover_checks"): Site(
        COPY, "Path.iterdir Path.read_bytes",
        "checks/ of a template in a private mkdtemp copy (catalog extract or local snapshot)"),
    ("services/community/community_agent_template_store.py", "_parse_dashboards_json"): Site(
        COPY, "Path.read_text",
        "dashboards/ of a template in a private mkdtemp copy (catalog extract or local snapshot)"),
    ("services/community/community_agent_template_store.py", "load_template_from_dir"): Site(
        COPY, "Path.read_text Path.read_text Path.read_text Path.read_text Path.read_text Path.rglob",
        "C26: a template in a private mkdtemp copy (catalog extract or local snapshot)"),
    ("services/community/community_agent_updater.py", "_fetch_new"): Site(
        COPY, "shutil.rmtree",
        "removes the catalog template's private mkdtemp extract"),
    ("services/community/community_agent_updater.py", "_layer_apps"): Site(
        COPY, "Path.read_text",
        "an app manifest in the new version's private mkdtemp extract"),
    ("services/community/community_agent_updater.py", "_layer_dashboards"): Site(
        AGENTS, "Path.iterdir",
        "F16: lists the person folders' names in users/, which only the server writes, each page checked and written through safe_fs"),
    ("services/community/community_agent_updater.py", "apply._job"): Site(
        COPY, "shutil.rmtree",
        "removes the catalog template's private mkdtemp extract"),
    ("services/community/community_agent_updater.py", "consent_lines"): Site(
        COPY, "Path.read_text",
        "an app manifest in the new version's private mkdtemp extract"),
    ("services/community/community_agent_updater.py", "plan"): Site(
        COPY, "shutil.rmtree",
        "removes the catalog template's private mkdtemp extract"),
    ("services/community/community_agents_catalog.py", "_extract_template_subfolder"): Site(
        COPY, "Path.mkdir Path.mkdir Path.write_bytes shutil.rmtree",
        "unpacks the catalog tarball into a private mkdtemp folder (caps are row C12)"),
    ("services/community/community_catalog.py", "_collect_installed_manifest_hashes"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/community_installer.py", "_apply_extracted_files"): Site(
        OWN, "Path.iterdir Path.iterdir Path.rename Path.unlink remove_tree remove_tree shutil.copy2 shutil.copytree shutil.copytree shutil.copytree shutil.rmtree shutil.rmtree shutil.rmtree",
        "C13: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/community_installer.py", "_check_incoming_tree"): Site(
        COPY, "Path.read_text Path.rglob Path.unlink",
        "C26: the incoming tool folder, a private mkdtemp extract"),
    ("services/community/community_installer.py", "_drop_backup"): Site(
        OWN, "Path.rename remove_tree",
        "the install's backup beside the tool under MCPS_DIR"),
    ("services/community/community_installer.py", "_drop_package_manager_files"): Site(
        COPY, "Path.unlink",
        "lock files at the root of the private mkdtemp extract"),
    ("services/community/community_installer.py", "_extract_mcp_subfolder"): Site(
        COPY, "Path.mkdir Path.mkdir Path.open",
        "unpacks the catalog tarball into a private mkdtemp folder (caps are row C12)"),
    ("services/community/community_installer.py", "_patch_set"): Site(
        OWN, "Path.glob Path.read_bytes",
        "C13: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/community_installer.py", "_refuse_shipped_trees"): Site(
        COPY, "Path.rglob",
        "walks the incoming tool folder, a private mkdtemp extract"),
    ("services/community/community_installer.py", "_remnant_owner"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/community_installer.py", "_rollback_extracted_files"): Site(
        OWN, "Path.rename Path.rename remove_tree remove_tree shutil.move",
        "C13: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/community_installer.py", "install_from_catalog"): Site(
        COPY, "shutil.rmtree",
        "removes its private mkdtemp extract"),
    ("services/community/community_installer.py", "install_from_extracted_folder"): Site(
        OWN, "Path.mkdir",
        "makes MCPS_DIR/community"),
    ("services/community/community_installer.py", "sweep_failed_asides"): Site(
        OWN, "Path.glob remove_tree",
        "failed install folders under MCPS_DIR, swept at boot"),
    ("services/community/mcp_source_swap.py", "_read_manifest"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/community/mcp_source_swap.py", "_settle_backups"): Site(
        OWN, "Path.glob remove_tree",
        "tool backups under MCPS_DIR/community, settled at boot"),
    ("services/community/package_check.py", "_bundled_skill_warnings"): Site(
        COPY, "Path.read_text",
        "skill files in a private mkdtemp snapshot of the author's package"),
    ("services/community/package_check.py", "_check_package_files"): Site(
        COPY, "Path.rglob",
        "walks a private mkdtemp snapshot of the author's package"),
    ("services/community/package_check.py", "_icon_problem"): Site(
        COPY, "Path.open",
        "icon.png in a private mkdtemp snapshot of the author's package"),
    ("services/community/package_check.py", "check_package"): Site(
        COPY, "Path.read_text Path.rglob",
        "a private mkdtemp snapshot of the author's package"),
    ("services/community/skills_installer.py", "_scrub_package_skills"): Site(
        COPY, "Path.read_text Path.write_text",
        "skill files in the private mkdtemp extract"),
    ("services/community/skills_installer.py", "_validate_skill_package"): Site(
        COPY, "Path.rglob",
        "walks the private mkdtemp extract"),
    ("services/community/skills_installer.py", "install_bare_skill_folder"): Site(
        COPY, "Path.mkdir Path.read_text Path.write_text shutil.copytree shutil.rmtree",
        "the admin's zip extract and its own mkdtemp staging folder"),
    ("services/community/skills_installer.py", "install_skill_package_from_catalog"): Site(
        COPY, "shutil.rmtree",
        "removes its private mkdtemp extract"),
    ("services/community/skills_installer.py", "install_skill_package_from_extracted"): Site(
        OWN, "Path.mkdir Path.read_text shutil.rmtree",
        "MCPS_DIR/skills and its backup, the manifest read from a private extract"),
    ("services/community/skills_installer.py", "package_has_scripts"): Site(
        OWN, "Path.iterdir Path.rglob",
        "an installed skill package under MCPS_DIR/skills"),
    ("services/containers/compose_files.py", "_compose_mounts_subpath"): Site(
        OWN, "Path.read_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/containers/door.py", "load_held"): Site(
        OWN, "Path.read_text",
        "container-sessions.json under SESSIONS_DIR"),
    ("services/containers/idle.py", "_direct_dials"): Site(
        OWN, "Path.glob Path.glob Path.read_text",
        "gateway descriptors and session tool configs under SESSIONS_DIR"),
    ("services/delegation/file_transfer.py", "perform_send_files"): Site(
        AGENTS, "Path.rglob",
        "B18: lists the sender's tree, folder links not entered, files opened strictly later"),
    ("services/engines/token_fanout.py", "write_credential_file"): Site(
        AGENTS, "Path.mkdir",
        "new: makes the scope's .claude or .codex, the name and its parents sit in folders no session writes"),
    ("services/infra/agent_dirs.py", "agent_dirs"): Site(
        AGENTS, "Path.iterdir",
        "new: lists AGENTS_DIR's top names, only the server writes that folder"),
    ("services/infra/archive_read.py", "open_tar_gz"): Site(
        NOT_A_PATH, "gzip.GzipFile",
        "C11, C12: a gzip stream read from bytes in memory, capped while it unpacks"),
    ("services/infra/git_writer.py", "_environment"): Site(
        OWN, "Path.mkdir",
        "the git home under SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "_fresh"): Site(
        OWN, "shutil.rmtree",
        "a new store under SESSIONS_DIR/history/incoming"),
    ("services/infra/git_writer.py", "_root"): Site(
        OWN, "Path.chmod Path.mkdir",
        "SESSIONS_DIR/history, the history store root"),
    ("services/infra/git_writer.py", "_write_commit"): Site(
        OWN, "Path.glob Path.unlink",
        "fast-import crash reports inside a store under SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "archive_person"): Site(
        OWN, "Path.mkdir os.rename shutil.rmtree",
        "a person's histories moved within SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "forget_agent"): Site(
        OWN, "shutil.rmtree",
        "an agent's histories under SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "forget_archived"): Site(
        OWN, "shutil.rmtree",
        "an archived person's histories under SESSIONS_DIR/history/offboarded"),
    ("services/infra/git_writer.py", "init_if_missing"): Site(
        AGENTS, "Path.mkdir",
        "new: makes a tracked folder, parents only the server writes, a link at it not followed"),
    ("services/infra/git_writer.py", "new_store"): Site(
        OWN, "Path.mkdir shutil.rmtree",
        "a new bare store under SESSIONS_DIR/history/incoming"),
    ("services/infra/git_writer.py", "publish"): Site(
        OWN, "Path.mkdir os.rename shutil.rmtree",
        "moves a built store to its place under SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "remove_orphans"): Site(
        AGENTS, "Path.iterdir Path.iterdir Path.iterdir os.listdir shutil.rmtree",
        "new: names of AGENTS_DIR only, every removal is in SESSIONS_DIR/history"),
    ("services/infra/git_writer.py", "set_aside"): Site(
        OWN, "Path.mkdir os.rename os.utime",
        "moves a store to SESSIONS_DIR/history/set-aside"),
    ("services/infra/git_writer.py", "set_aside_agent"): Site(
        OWN, "shutil.rmtree",
        "an earlier agent's histories under SESSIONS_DIR/history"),
    ("services/infra/history_move.py", "_copy_objects"): Site(
        OWN, "Path.mkdir open",
        "writes object files into its raw store under SESSIONS_DIR/history, reads go via safe_fs"),
    ("services/infra/history_move.py", "_repacked"): Site(
        OWN, "Path.glob Path.unlink shutil.rmtree shutil.rmtree",
        "pack files and stores under SESSIONS_DIR/history"),
    ("services/infra/history_move.py", "_sweep_incoming"): Site(
        OWN, "Path.iterdir shutil.rmtree",
        "stale stores under SESSIONS_DIR/history/incoming"),
    ("services/infra/history_move.py", "finish"): Site(
        OWN, "Path.read_text Path.unlink",
        "the pending flag inside a store under SESSIONS_DIR/history"),
    ("services/infra/history_move.py", "run_pass"): Site(
        OWN, "Path.write_text os.replace",
        "the marker file under SESSIONS_DIR/history"),
    ("services/infra/history_move.py", "take"): Site(
        OWN, "Path.write_text shutil.rmtree shutil.rmtree",
        "the pending flag and a built store under SESSIONS_DIR/history"),
    ("services/infra/retention.py", "_codex_session_leftovers"): Site(
        AGENTS, "Path.glob",
        "new: lists one .codex folder by name, each removal goes through safe_fs beneath AGENTS_DIR"),
    ("services/infra/retention.py", "_open_writable"): Site(
        AGENTS, "os.chmod os.open",
        "D15: chmod through an O_NOFOLLOW O_PATH handle beneath a held directory descriptor"),
    ("services/infra/retention.py", "_pass_codex_junk"): Site(
        AGENTS, "Path.glob Path.glob",
        "F17: lists names in a scope's .codex, every removal goes through _unlink and rmdir_beneath, which refuse a link on the way"),
    ("services/infra/retention.py", "_pass_orphan_partials"): Site(
        AGENTS, "Path.rglob",
        "D14: walks AGENTS_DIR names, folder links not entered, removals through safe_fs"),
    ("services/infra/retention.py", "_pass_session_homes"): Site(
        AGENTS, "os.listdir",
        "D16: lists own/ names after a link check, each home dropped beneath a held handle"),
    ("services/infra/retention.py", "_remove_beneath"): Site(
        OWN, "Path.unlink shutil.rmtree",
        "raw when the path is not under the agents folder by its text: the legacy transcript in the proxy's own home (D14)"),
    ("services/infra/retention.py", "compute_storage_usage"): Site(
        OWN, "Path.glob",
        "the server's own proxy.log files in BASE_DIR, sizes only"),
    ("services/infra/retention.py", "iter_local_homes"): Site(
        AGENTS, "Path.iterdir Path.iterdir Path.iterdir Path.iterdir",
        "new: names in users/ and externals/, folders only the server writes"),
    ("services/infra/storage_quota.py", "_mount_info"): Site(OWN, "open", "/proc/mounts"),
    ("services/knowledge/library_projector.py", "_mirror_mode"): Site(OWN, "open", "/proc/self/status for the umask"),
    ("services/knowledge/library_projector.py", "_touch"): Site(
        NOT_A_PATH, "os.utime",
        "F14: a mirror file's times set on a descriptor open_beneath gave, no link followed"),
    ("services/knowledge/library_rules.py", "validate_subdir_on_disk"): Site(
        AGENTS, "os.walk",
        "A26: a check walk under knowledge/, links not followed, names and targets only"),
    ("services/mcp/compose_rewrite.py", "_plan_pull_compose"): Site(
        OWN, "Path.read_text",
        "a T2 tool's compose under MCPS_DIR, the installer only writes it"),
    ("services/mcp/compose_rewrite.py", "_read_recorded_subnet"): Site(
        OWN, "Path.read_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/compose_rewrite.py", "ensure_pull_compose"): Site(
        OWN, "Path.write_text",
        "rewrites a T2 tool's compose under MCPS_DIR"),
    ("services/mcp/compose_rewrite.py", "ensure_t1_override"): Site(
        OWN, "Path.read_text Path.read_text Path.write_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_build_state"): Site(
        OWN, "open",
        "C16: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_compose_config_hash"): Site(
        OWN, "Path.read_bytes",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_hash_tree"): Site(
        OWN, "open os.walk",
        "C16: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_inject_mcp_env"): Site(
        OWN, "Path.read_text Path.read_text Path.unlink Path.write_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_read_hash_file"): Site(
        OWN, "Path.read_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_target_service"): Site(
        OWN, "Path.read_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/docker_manager.py", "_write_hash_file"): Site(
        OWN, "Path.write_text",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/manifest_floor.py", "load_schema"): Site(
        OWN, "Path.read_text",
        "the shipped manifest schema in MCPS_DIR"),
    ("services/mcp/mcp_egress.py", "_trust_dir"): Site(
        OWN, "Path.iterdir Path.read_bytes",
        "the certificate folder the server's own install environment names"),
    ("services/mcp/mcp_egress.py", "_trust_file"): Site(
        OWN, "Path.read_bytes",
        "a trust file the server's own install environment names"),
    ("services/mcp/mcp_installer.py", "_create_venv"): Site(
        OWN, "shutil.rmtree shutil.rmtree shutil.rmtree",
        "a tool's build in the tools folder, which only the installer writes: the --build command, the image's build, a machine"),
    ("services/mcp/mcp_installer.py", "_managed_of"): Site(
        OWN, "Path.glob",
        "a tool's build in the tools folder, which only the installer writes: the --build command, the image's build, a machine"),
    ("services/mcp/mcp_installer.py", "_prune_one"): Site(
        OWN, "Path.iterdir Path.unlink shutil.rmtree",
        "uv interpreters and venv configs under MCPS_DIR, pruned at boot"),
    ("services/mcp/mcp_installer.py", "_python_installed_version"): Site(
        OWN, "Path.glob",
        "a tool's build in the tools folder, which only the installer writes: the --build command, the image's build, a machine"),
    ("services/mcp/mcp_installer.py", "_pyvenv_cfgs"): Site(
        OWN, "os.walk",
        "walks MCPS_DIR for venv configs, regular files only"),
    ("services/mcp/mcp_installer.py", "_walk_source_files"): Site(
        OWN, "os.walk",
        "C16: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_installer.py", "compute_version_hash"): Site(
        OWN, "Path.glob Path.read_bytes Path.read_bytes Path.read_bytes",
        "C16: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_installer.py", "install_mcp"): Site(
        OWN, "Path.glob Path.unlink Path.write_bytes Path.write_bytes shutil.rmtree shutil.rmtree shutil.rmtree",
        "a tool's build in the tools folder, which only the installer writes: the --build command, the image's build, a machine"),
    ("services/mcp/mcp_installer.py", "read_built"): Site(
        OWN, "os.open",
        "C25: a build's output under MCPS_DIR, no-follow, regular, capped"),
    ("services/mcp/mcp_installer.py", "remove_tree"): Site(
        OWN, "os.chmod os.scandir shutil.rmtree shutil.rmtree",
        "C17: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_installer.py", "seed_interpreter"): Site(
        OWN, "Path.mkdir os.replace shutil.copytree shutil.rmtree",
        "a tool's build in the tools folder, which only the installer writes: the --build command, the image's build, a machine"),
    ("services/mcp/mcp_installer.py", "self_hash"): Site(
        OWN, "Path.read_bytes",
        "on a machine, the satellite's copy of this module, hashed for its drift check"),
    ("services/mcp/mcp_installer.py", "sweep_folders"): Site(
        OWN, "Path.iterdir remove_tree",
        "on a machine, the satellite's own builds folder"),
    ("services/mcp/mcp_manifest_parse.py", "_parse_manifest"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_manifest_store.py", "scan_manifests"): Site(
        OWN, "Path.iterdir",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_permissions.py", "_legacy_file_labels"): Site(
        OWN, "Path.read_text",
        "the 1.9 tool hints file in the platform data folder"),
    ("services/mcp/mcp_pin.py", "pin_manifest"): Site(
        OWN, "Path.read_text Path.write_bytes",
        "an installed tool's manifest, the installer's own folder"),
    ("services/mcp/mcp_pin.py", "settle_built"): Site(
        OWN, "Path.write_bytes",
        "an installed tool's package.json, the installer's own folder"),
    ("services/mcp/mcp_server_resolve.py", "_generate_instance_config_file"): Site(
        OWN, "Path.write_text",
        "an instance config file under SESSIONS_DIR/user-mcp-configs"),
    ("services/mcp/mcp_session_config.py", "_SessionMcpBuild._instance_delivery"): Site(
        OWN, "Path.mkdir",
        "makes SESSIONS_DIR/user-mcp-configs"),
    ("services/mcp/mcp_session_config.py", "_sweep_session_builds"): Site(
        OWN, "Path.iterdir shutil.rmtree",
        "day-old session tool configs under SESSIONS_DIR"),
    ("services/mcp/mcp_session_config.py", "_write_session_mcp_config"): Site(
        OWN, "Path.mkdir os.utime",
        "a session's tool config under SESSIONS_DIR"),
    ("services/mcp/mcp_session_config.py", "_write_whole"): Site(
        OWN, "os.replace os.unlink tempfile.mkstemp",
        "a temp file and rename beside a tool config under SESSIONS_DIR"),
    ("services/mcp/mcp_session_config.py", "inject_credential_env_into_toml"): Site(
        OWN, "Path.read_text Path.unlink Path.write_text",
        "a session's Codex tool config and its sidecar under SESSIONS_DIR"),
    ("services/mcp/mcp_system_deps.py", "_detect_os_keys"): Site(OWN, "Path.read_text", "/etc/os-release"),
    ("services/mcp/mcp_tarball.py", "_build_bytes"): Site(
        OWN, "Path.rglob",
        "C24: packs an installed tool folder under MCPS_DIR, links skipped"),
    ("services/mcp/mcp_tarball.py", "_cache_dir"): Site(
        OWN, "Path.mkdir",
        "the tool archive cache under the server's sessions folder"),
    ("services/mcp/mcp_tarball.py", "build_tarball"): Site(
        OWN, "Path.read_bytes Path.touch Path.unlink Path.write_bytes os.replace",
        "the tool archive cache under the server's sessions folder"),
    ("services/mcp/mcp_tarball.py", "gc"): Site(
        OWN, "Path.iterdir Path.unlink Path.unlink",
        "the tool archive cache under the server's sessions folder"),
    ("services/mcp/mcp_tarball.py", "invalidate"): Site(
        OWN, "Path.iterdir Path.unlink",
        "the tool archive cache under the server's sessions folder"),
    ("services/mcp/mcp_updater.py", "_package_axis._installed_hashes"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_updater.py", "_pinned_axis._docker_hashes"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_updater.py", "_skills_axis._skill_hashes"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_updater.py", "_update_node_python_mcp"): Site(
        OWN, "Path.read_text",
        "C15: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/mcp/mcp_venv_bootstrap.py", "_build_python"): Site(
        OWN, "shutil.rmtree",
        "the bundled tools' build in the install's tools folder: the --build command and the image's build"),
    ("services/mcp/mcp_venv_bootstrap.py", "_forget_success"): Site(
        OWN, "Path.write_text",
        "the bundled tools' build in the install's tools folder: the --build command and the image's build"),
    ("services/mcp/mcp_venv_bootstrap.py", "_mark_interpreter"): Site(
        OWN, "Path.write_text",
        "the bundled tools' build in the install's tools folder: the --build command and the image's build"),
    ("services/mcp/mcp_venv_bootstrap.py", "_read_marker"): Site(
        OWN, "Path.read_text",
        "a bundled tool's runtime marker under MCPS_DIR"),
    ("services/mcp/mcp_venv_bootstrap.py", "_requirements_hash"): Site(
        OWN, "Path.read_bytes",
        "a bundled tool's requirements.txt under MCPS_DIR/custom"),
    ("services/mcp/mcp_venv_bootstrap.py", "_venv_has_packages"): Site(
        OWN, "Path.glob Path.glob",
        "a bundled tool's venv under MCPS_DIR/custom"),
    ("services/mcp/mcp_venv_bootstrap.py", "_write_marker"): Site(
        OWN, "Path.write_text",
        "a bundled tool's runtime marker under MCPS_DIR/custom"),
    ("services/mcp/mcp_venv_bootstrap.py", "collect_for_image"): Site(
        ELSEWHERE, "Path.iterdir Path.iterdir Path.mkdir shutil.move shutil.move shutil.move",
        "the image's build only (--build-image)"),
    ("services/mcp/mcp_venv_bootstrap.py", "scan_bundled_venvs"): Site(
        OWN, "Path.iterdir Path.read_text",
        "bundled tools' manifests under MCPS_DIR, the boot scan"),
    ("services/mcp/tool_listing.py", "container_fingerprint"): Site(
        OWN, "Path.read_bytes",
        "C14: accepted, the roadmap's entry 'The installed tool folders' remainder'"),
    ("services/media/media_pipeline.py", "_faststart_remux_sync"): Site(
        OWN, "Path.mkdir Path.unlink Path.unlink",
        "the media cache under the platform data folder or the admin's OTO_MEDIA_CACHE_DIR"),
    ("services/media/media_pipeline.py", "_transcode_sync"): Site(
        OWN, "Path.mkdir Path.unlink Path.unlink",
        "the media cache under the platform data folder or the admin's OTO_MEDIA_CACHE_DIR"),
    ("services/media/media_pipeline.py", "host_cache_dir"): Site(
        OWN, "Path.mkdir",
        "host-media-cache under the platform data folder"),
    ("services/media/media_pipeline.py", "sweep_host_media_cache"): Site(
        OWN, "Path.iterdir Path.iterdir Path.iterdir Path.unlink shutil.rmtree",
        "host-media-cache and its converted folders under the platform data folder"),
    ("services/media/preview_snapshots.py", "_sha256_file"): Site(OWN, "open", "a copy under preview-snapshots"),
    ("services/media/preview_snapshots.py", "create_snapshot"): Site(OWN, "os.makedirs", "preview-snapshots"),
    ("services/media/preview_snapshots.py", "delete_chat_dir"): Site(
        OWN, "shutil.rmtree",
        "a chat's folder under preview-snapshots"),
    ("services/media/preview_snapshots.py", "delete_snapshot"): Site(
        OWN, "Path.unlink",
        "a copy under preview-snapshots"),
    ("services/media/preview_snapshots.py", "gc_chat"): Site(
        OWN, "Path.iterdir Path.unlink",
        "a chat's folder under preview-snapshots"),
    ("services/media/preview_snapshots.py", "refresh_newest"): Site(
        NOT_A_PATH, "os.utime",
        "os.utime on the open writer's descriptor"),
    ("services/media/preview_snapshots.py", "sweep_orphans"): Site(
        OWN, "Path.iterdir shutil.rmtree",
        "chat folders under preview-snapshots"),
    ("services/media/upload_staging.py", "_open_metas"): Site(
        OWN, "Path.iterdir Path.read_text",
        "a person's folder under upload-staging"),
    ("services/media/upload_staging.py", "staged_bytes_by_user"): Site(OWN, "Path.iterdir", "upload-staging"),
    ("services/media/wake_keywords.py", "_load_encoder"): Site(
        OWN, "open",
        "the shipped keyword encoder model under BASE_DIR/assets"),
    ("services/memory/memory_file.py", "regenerate_index"): Site(
        NOT_A_PATH, "os.utime",
        "A14: the index's times set on a descriptor open_beneath gave beneath the scope"),
    ("services/memory/memory_publish.py", "commit"): Site(
        AGENTS, "Path.mkdir",
        "new: makes knowledge or users/{u}/context, parents only the server writes"),
    ("services/notifications/push_sender.py", "<module>"): Site(
        OWN, "open",
        "the admin's FCM service account key path from the settings"),
    ("services/oauth/credential_resolver.py", "purge_agent_tree_credentials"): Site(
        AGENTS, "Path.unlink shutil.rmtree",
        "A26: the boot sweep of .credentials, a link removed not followed, linked folders skipped"),
    ("services/oauth/credential_resolver.py", "purge_agent_tree_credentials._plain_dirs"): Site(
        AGENTS, "Path.iterdir",
        "A26: lists AGENTS_DIR and users/, linked folders skipped"),
    ("services/oauth/github_app_lifecycle.py", "_detach"): Site(
        OWN, "Path.unlink",
        "an installation's token files under SESSIONS_DIR/{provider}-tokens/_app"),
    ("services/oauth/github_apps.py", "installation_files"): Site(
        OWN, "Path.glob",
        "the token store under SESSIONS_DIR/{provider}-tokens/_app"),
    ("services/oauth/github_apps.py", "mark_installation_tokens_for_remint"): Site(
        OWN, "Path.glob",
        "the token store under SESSIONS_DIR/{provider}-tokens/_app"),
    ("services/oauth/github_apps.py", "reap_if_orphan"): Site(
        OWN, "Path.unlink",
        "an installation's token file under SESSIONS_DIR/{provider}-tokens/_app"),
    ("services/oauth/github_apps.py", "remint_file"): Site(
        OWN, "Path.unlink",
        "an installation's token file under SESSIONS_DIR/{provider}-tokens/_app"),
    ("services/oauth/oauth_account_store.py", "_read_oauth_token"): Site(
        OWN, "Path.read_text",
        "a token file under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "_write_generic_oauth_v1_token"): Site(
        OWN, "Path.chmod Path.replace Path.write_text",
        "a token file under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "account_label_for"): Site(
        OWN, "Path.glob",
        "a person's token folder under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "delete_account_token"): Site(
        OWN, "Path.unlink",
        "a token file under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "get_token_base_dir"): Site(
        OWN, "Path.mkdir",
        "SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "get_token_dir"): Site(
        OWN, "Path.mkdir",
        "a person's folder under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "other_account_files"): Site(
        OWN, "Path.glob Path.iterdir",
        "the token store under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_account_store.py", "write_token_payload"): Site(
        OWN, "Path.chmod Path.replace Path.write_text",
        "a token file under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_refresh_worker.py", "_read_with_mtime"): Site(
        OWN, "open",
        "a token file under SESSIONS_DIR/{provider}-tokens"),
    ("services/oauth/oauth_refresh_worker.py", "_scan_token_files"): Site(
        OWN, "Path.glob Path.glob Path.iterdir",
        "the token store under SESSIONS_DIR"),
    ("services/oauth/oauth_refresh_worker.py", "_write_back"): Site(
        OWN, "os.chmod os.replace os.unlink tempfile.mkstemp",
        "a token file and its temporary beside it under SESSIONS_DIR/{provider}-tokens"),
    ("services/phone/people.py", "_memory_of"): Site(
        AGENTS, "Path.glob Path.read_text",
        "B21: the caller's memory topics, read-only to the caller, links skipped"),
    ("services/phone/people.py", "person_files"): Site(
        AGENTS, "Path.rglob",
        "B21: lists the caller's tree by name, links skipped, the export reads safely"),
    ("services/remote/compute_bootstrap.py", "_held_token"): Site(
        AGENTS, "os.open",
        "B22: the agents folder's marker, opened no-follow and non-blocking, regular checked"),
    ("services/remote/compute_bootstrap.py", "_read"): Site(
        OWN, "Path.read_text",
        "the credential file in the compute link folder"),
    ("services/remote/compute_bootstrap.py", "_replace_marker"): Site(
        AGENTS, "Path.unlink os.open shutil.rmtree",
        "B22: the agents folder's marker, a link removed not followed, created O_EXCL no-follow"),
    ("services/remote/compute_bootstrap.py", "_write"): Site(
        OWN, "os.open os.replace",
        "the credential file in the compute link folder"),
    ("services/remote/compute_bootstrap.py", "ensure"): Site(OWN, "Path.mkdir", "the compute link folder"),
    ("services/remote/compute_bootstrap.py", "ensure_marker"): Site(
        AGENTS, "Path.mkdir",
        "new: makes AGENTS_DIR itself, its parent is the platform data folder"),
    ("services/remote/compute_pairing.py", "bootstrap_script"): Site(
        OWN, "Path.read_bytes Path.read_bytes Path.read_text",
        "the shipped install scripts under PLATFORM_ROOT/scripts"),
    ("services/webhooks/relay_events.py", "hosted_account_providers"): Site(
        OWN, "Path.glob",
        "the token store folders under SESSIONS_DIR"),
    ("startup_sweeps.py", "_purge_mcp_build_outputs"): Site(
        OWN, "shutil.rmtree",
        "SESSIONS_DIR's user-mcp-configs and sse-mcp-configs"),
    ("static_assets.py", "dashboard_build_id"): Site(OWN, "open", "the dashboard build's index.html"),
    ("static_assets.py", "negotiated_file_response"): Site(
        OWN, "FileResponse FileResponse",
        "the dashboard build and the wake-word assets, confined by the caller"),
    ("storage/agents/agent_store.py", "create_agent"): Site(
        AGENTS, "Path.mkdir Path.mkdir Path.mkdir",
        "A26: agent create, the new agent's folders made before any session can reach them"),
    ("storage/catalog_schema.py", "_entries"): Site(
        COPY, "os.scandir",
        "a template's private copy, its snapshot or the extracted catalog archive"),
    ("storage/catalog_schema.py", "_load"): Site(
        OWN, "Path.read_text",
        "the shipped catalog schemas in the repository's mcps folder"),
    ("storage/catalog_schema.py", "read_plain"): Site(
        OWN, "Path.open",
        "the catalog check's default reader, used by the release gate on the repository's catalog. The server passes template_sig.read_file"),
    ("storage/files/recover_bin_store.py", "capture"): Site(
        OWN, "Path.mkdir Path.unlink Path.write_bytes",
        "the recover bin under RECOVER_BIN_DIR"),
    ("storage/files/recover_bin_store.py", "delete"): Site(OWN, "Path.unlink", "the recover bin under RECOVER_BIN_DIR"),
    ("storage/files/recover_bin_store.py", "delete_expired"): Site(
        OWN, "Path.rmdir Path.unlink",
        "the recover bin under RECOVER_BIN_DIR"),
    ("storage/files/recover_bin_store.py", "prune_orphan_blobs"): Site(
        OWN, "Path.iterdir Path.rmdir Path.unlink os.listdir",
        "the recover bin under RECOVER_BIN_DIR"),
    ("storage/files/recover_bin_store.py", "read_bytes"): Site(
        OWN, "Path.read_bytes",
        "the recover bin under RECOVER_BIN_DIR"),
    ("storage/files/recover_bin_store.py", "remove_agent_files"): Site(
        OWN, "shutil.rmtree",
        "the recover bin under RECOVER_BIN_DIR"),
    ("storage/identity/db_users.py", "usernames_on_disk"): Site(
        AGENTS, "os.scandir os.scandir",
        "new: names only, of AGENTS_DIR and each agent's users folder, no file opened"),
    ("storage/pg.py", "LoopPool._recover"): Site(NOT_A_PATH, "Path.open", "a database connection pool's open, no file"),
    ("storage/pg.py", "LoopPool.open"): Site(NOT_A_PATH, "Path.open", "a database connection pool's open, no file"),
    ("storage/pg.py", "_new_thread_pool"): Site(NOT_A_PATH, "Path.open", "a database connection pool's open, no file"),
    ("storage/pg.py", "arm_loop_pool"): Site(NOT_A_PATH, "Path.open", "a database connection pool's open, no file"),
    ("ws/audio.py", "_LanguageSwitch._event"): Site(NOT_A_PATH, "Path.open", "a speech stream's open, no file"),
    ("ws/satellite.py", "_read_satellite_version_from_source"): Site(
        OWN, "Path.read_text",
        "the repository's shipped satellite/config.py"),
}

SATELLITE_SITES: dict[tuple[str, str], Site] = {
    ("__main__.py", "_check_post_update_state"): Site(
        OWN, "Path.read_text Path.read_text Path.unlink Path.unlink Path.unlink Path.write_text Path.write_text atomic_replace atomic_replace force_rmtree force_rmtree force_rmtree",
        "its update marker, satellite.previous and satellite.new under otodock_dir"),
    ("__main__.py", "_ensure_otodock_on_path"): Site(
        OWN, "Path.mkdir Path.symlink_to Path.unlink os.chmod",
        "a link in its account's ~/.local/bin to the otodock wrapper its install ships"),
    ("__main__.py", "_ensure_otodock_on_path_windows"): Site(
        OWN, "Path.unlink",
        "a stale otodock.cmd copy in otodock_dir/bin"),
    ("__main__.py", "_install_crash_diagnostics"): Site(
        OWN, "Path.read_text Path.replace open",
        "its satellite-fault.log under otodock_dir"),
    ("__main__.py", "_main"): Site(
        AGENTS, "Path.mkdir Path.mkdir Path.mkdir Path.mkdir",
        "new: makes the configured agents and tools roots themselves, sessions write only below"),
    ("__main__.py", "_setup_logging"): Site(
        OWN, "logging.handlers.RotatingFileHandler",
        "the satellite's own log file in its state folder"),
    ("__main__.py", "_verify_installer_drift"): Site(
        OWN, "Path.read_bytes Path.read_bytes",
        "its own vendored files, hashed against the baked values"),
    ("config.py", "_holds"): Site(
        PERSON, "os.open",
        "write_if_changed's no-follow check, only paired starts call it, a _compute start skips"),
    ("config.py", "_sweep_stale_temps"): Site(
        PERSON, "os.scandir os.unlink",
        "B27: write_if_changed's temps in .claude or .codex, paired starts only"),
    ("config.py", "_write_in_place"): Site(
        PERSON, "Path.chmod open",
        "write_if_changed's Windows fallback, a Windows computer is never a compute machine"),
    ("config.py", "atomic_replace"): Site(
        OWN, "os.replace",
        "a helper: its install, its tools folder, safe_fs's Windows chain on a paired PC"),
    ("config.py", "ensure_windows_install_artifacts"): Site(
        OWN, "Path.read_text Path.write_text",
        "start-tray.vbs under otodock_dir on a Windows install"),
    ("config.py", "force_rmtree"): Site(
        AGENTS, "shutil.rmtree",
        "new: the shared removal itself, each caller is a listed site with its own judgement"),
    ("config.py", "write_command_wrappers"): Site(
        PERSON, "Path.mkdir Path.unlink os.scandir os.unlink shutil.rmtree",
        "B27: command wrappers in .claude or .codex, a _compute start returns before them"),
    ("config.py", "write_if_changed"): Site(
        PERSON, "os.open os.replace os.unlink os.unlink os.unlink shutil.rmtree",
        "B27: hook and settings files of paired starts, a _compute start writes none"),
    ("host/auth_paths.py", "unpack_tarball"): Site(
        OWN, "Archive.extractall Archive.extractall",
        "the update's staging folder and a tool's .new folder, each made empty by its caller under "
        "otodock_dir or mcps_dir; every member checked before anything is written"),
    ("host/host_probe.py", "_detect_user_dirs"): Site(
        OWN, "Path.read_text",
        "user-dirs.dirs in its own account's home, read for its capabilities"),
    ("host/macos_app.py", "build"): Site(
        OWN, "Path.mkdir Path.mkdir Path.read_bytes Path.read_text Path.write_bytes Path.write_text Path.write_text Path.write_text shutil.move shutil.rmtree",
        "its Mac app bundle and stamp in the install folder, built in a TemporaryDirectory"),
    ("host/service_unit.py", "ensure_oom_policy"): Site(
        OWN, "Path.mkdir Path.write_text",
        "the drop-in of its own systemd user unit"),
    ("host/session_retention.py", "_files"): Site(
        AGENTS, "os.scandir",
        "new: lists names only, each removal it feeds is safe_fs beneath the root or row B9"),
    ("host/session_retention.py", "_subdirs"): Site(
        AGENTS, "os.scandir",
        "new: lists names only, each removal it feeds is safe_fs beneath the root, row B9, or sweep_staging (its own row)"),
    ("host/tray.py", "_load_base_image"): Site(OWN, "Image.open", "the icon its install ships"),
    ("sessions/cli_session.py", "CLISession.start"): Site(
        PERSON, "Path.iterdir Path.mkdir Path.mkdir Path.unlink Path.write_text Path.write_text shutil.rmtree",
        "B27: .claude writes of a paired start, a compute start returns to _start_compute"),
    ("sessions/cli_session.py", "remove_prompt_file"): Site(
        PERSON, "Path.unlink",
        "B27: a paired session's prompt file, a compute close returns before it"),
    ("sessions/cli_session.py", "write_prompt_file"): Site(
        PERSON, "Path.glob Path.mkdir Path.unlink Path.unlink Path.write_text",
        "B27: a paired start's prompt file, a compute start returns before it"),
    ("sessions/codex_files.py", "chmod_private"): Site(
        PERSON, "Path.chmod",
        "B27: .codex config of a paired Codex start, a compute start returns before it"),
    ("sessions/codex_files.py", "write_or_drop_auth_json"): Site(
        PERSON, "Path.unlink Path.write_text",
        "B27: auth.json of a paired Codex start, a compute start returns before it"),
    ("sessions/codex_files.py", "write_or_drop_model_catalog"): Site(
        PERSON, "Path.unlink Path.write_text",
        "B27: models.json of a paired Codex start, a compute start returns before it"),
    ("sessions/codex_session.py", "CodexSession.start"): Site(
        PERSON, "Path.mkdir Path.mkdir Path.write_text Path.write_text",
        "the .codex writes of a paired start, a compute start returns to _start_compute"),
    ("sessions/mcp_install_support.py", "_fix_shebangs_after_swap"): Site(
        OWN, "Path.iterdir",
        "venv/bin of a swapped tool in its tools folder, links refused, read via read_built"),
    ("sessions/mcp_install_support.py", "_fix_windows_exe_paths_after_swap"): Site(
        OWN, "Path.iterdir",
        "venv/Scripts of a swapped tool in its tools folder, Windows only"),
    ("sessions/mcp_install_support.py", "_force_remove_or_quarantine"): Site(
        OWN, "Path.rename force_rmtree",
        "a tool folder or {tool}.new in its tools folder, removed or renamed aside"),
    ("sessions/mcp_install_support.py", "_read_runtime_marker"): Site(
        OWN, "Path.read_text",
        "a tool's .oto-runtime.json in its installed tools folder"),
    ("sessions/mcp_install_support.py", "_rewrite_through_fresh_name"): Site(
        OWN, "atomic_replace os.open os.unlink",
        "a built entry point in its tools folder, rewritten via an exclusive no-follow temp"),
    ("sessions/mcp_install_support.py", "_scan_installed_mcps"): Site(
        OWN, "Path.iterdir Path.iterdir Path.read_text",
        "the manifests in its installed tools folder"),
    ("sessions/mcp_install_support.py", "_settle_venv_asides"): Site(
        OWN, "Path.glob force_rmtree force_rmtree os.replace",
        "the venvs a stopped rebuild set aside in its tools folder"),
    ("sessions/mcp_install_support.py", "_warm_one_mcp"): Site(
        PERSON, "Path.read_text",
        "B27: a tool's manifest, sync_mcps skips the warm start on a compute machine"),
    ("sessions/mcp_install_support.py", "_write_runtime_marker"): Site(
        OWN, "Path.write_text",
        "a tool's .oto-runtime.json in its installed tools folder"),
    ("sessions/mcp_install_support.py", "reconcile_mcp_runtimes"): Site(
        OWN, "Path.iterdir Path.iterdir Path.read_text force_rmtree force_rmtree force_rmtree os.replace os.replace",
        "venvs, asides and manifests in its installed tools folder, at its start"),
    ("sessions/mcp_install_support.py", "rescue_preserved"): Site(
        OWN, "Path.mkdir os.replace",
        "keys, config and screenshots moved back from {tool}.new in its tools folder"),
    ("sessions/mcp_install_support.py", "sweep_staged"): Site(
        OWN, "Path.glob Path.glob Path.glob force_rmtree",
        "quarantine, .new and .copying folders in its installed tools folder"),
    ("sessions/mcp_install_support.py", "verify_installed"): Site(
        OWN, "Path.iterdir Path.iterdir Path.read_text",
        "the manifests and install markers in its installed tools folder"),
    ("sessions/memory_guard.py", "Guard._children"): Site(
        OWN, "open os.listdir",
        "/proc/{pid}/task and its children lists"),
    ("sessions/memory_guard.py", "Guard._folders"): Site(OWN, "open", "/proc/{pid}/mountinfo"),
    ("sessions/memory_guard.py", "Guard._name"): Site(OWN, "open", "/proc/{pid}/comm"),
    ("sessions/memory_guard.py", "Guard._size"): Site(OWN, "open", "/proc/{pid}/status"),
    ("sessions/memory_guard.py", "Guard._table"): Site(OWN, "os.scandir", "the /proc listing"),
    ("sessions/memory_guard.py", "Guard.run_pass"): Site(
        NOT_A_PATH, "Path.walk",
        "the memory guard's own walk of its cgroups, a method of the same name"),
    ("sessions/memory_guard.py", "_stat"): Site(OWN, "open", "/proc/{pid}/stat"),
    ("sessions/sandbox_host.py", "_resolv_conf"): Site(
        OWN, "Path.mkdir Path.read_text Path.write_text",
        "its generated resolver file in its compute state folder under otodock_dir"),
    ("sessions/sandbox_host.py", "_write_private"): Site(
        OWN, "os.open",
        "a build's trust file in its compute state folder, opened no-follow"),
    ("sessions/sandbox_host.py", "build_command"): Site(
        OWN, "Path.mkdir",
        "a tool's interpreter folder in its installed tools folder"),
    ("sessions/sandbox_host.py", "build_host"): Site(
        OWN, "Path.mkdir",
        "the empty mask folder in its compute state folder"),
    ("sessions/sandbox_host.py", "build_run.run"): Site(
        OWN, "Path.mkdir tempfile.mkdtemp",
        "a build step's scratch, a mkdtemp in its compute state folder"),
    ("sessions/sandbox_host.py", "clear_egress"): Site(
        OWN, "shutil.rmtree",
        "the sync frame's trust files in its compute state folder"),
    ("sessions/sandbox_host.py", "note_egress"): Site(
        OWN, "Path.mkdir Path.mkdir",
        "the sync frame's trust folder in its compute state folder"),
    ("sessions/sandbox_host.py", "note_hosted"): Site(
        OWN, "Path.mkdir Path.unlink Path.write_text",
        "the hosted mark in its compute state folder"),
    ("sessions/sandbox_host.py", "render_host"): Site(
        OWN, "Path.mkdir",
        "the empty mask folder in its compute state folder"),
    ("sessions/session_files.py", "materialize_agent_files"): Site(
        AGENTS, "Path.mkdir",
        "B6: makes the agent folder of this copy itself, every folder and file below through the safe_fs twin"),
    ("sessions/session_files.py", "purge_agent_tree_credentials"): Site(
        AGENTS, "Path.glob shutil.rmtree",
        "B6: at the start, before any session, only a folder reached with no link from the agents folder is removed, rmtree follows no link inside"),
    ("sessions/session_files.py", "purge_agent_tree_mcp_configs"): Site(
        AGENTS, "Path.glob",
        "B6: the start's sweep lists names by a glob, every removal through unlink_beneath, which refuses a link on the way"),
    ("sessions/session_files.py", "wipe"): Site(
        OWN, "shutil.rmtree",
        "a session's folder in its session-secrets under otodock_dir"),
    ("sessions/session_files.py", "wipe_all"): Site(
        OWN, "shutil.rmtree",
        "its session-secrets folder under otodock_dir"),
    ("sessions/session_homes.py", "_stores"): Site(
        AGENTS, "Path.iterdir Path.iterdir",
        "new: lists agent and users folder names, folders no session writes, links skipped"),
    ("sessions/session_homes.py", "write_sign_in_beneath"): Site(
        AGENTS, "Path.mkdir",
        "B25: makes the agents root only, the sign-in goes through the safe_fs twin"),
    ("sessions/session_manager.py", "SessionManager._agent_dir_for"): Site(
        AGENTS, "Path.mkdir",
        "new: makes agents/{slug} by a checked slug on its own copy, no session writes that root"),
    ("sessions/session_manager.py", "SessionManager._file_pull"): Site(
        PERSON, "open",
        "B24 and B27: agent paths open via safe_fs, the raw open is a host path, never on compute"),
    ("sessions/session_manager.py", "SessionManager.cleanup_orphans"): Site(
        AGENTS, "Path.glob Path.iterdir Path.read_text Path.unlink",
        "new: old pid files in agents/.satellite-pids, no session writes there, and its tools"),
    ("sessions/session_manager.py", "SessionManager.compute_agent_fingerprints"): Site(
        PERSON, "Path.iterdir",
        "lists the agent folders, the heartbeat skips it when _compute"),
    ("sessions/session_manager.py", "SessionManager.file_push"): Site(
        AGENTS, "Path.mkdir",
        "B24: the push goes through the safe_fs twin, this makes agents/{slug} by a checked slug"),
    ("sessions/session_manager.py", "SessionManager.sync_mcps"): Site(
        OWN, "Path.mkdir Path.mkdir Path.unlink Path.unlink Path.unlink Path.write_text atomic_replace force_rmtree force_rmtree force_rmtree shutil.move shutil.move shutil.rmtree shutil.rmtree",
        "its installed tools folder: extract through auth_paths.unpack_tarball, swap, removal by a checked name"),
    ("sessions/session_manager.py", "_apply_satellite_host_push"): Site(
        PERSON, "Path.mkdir Path.mkdir Path.unlink Path.unlink Path.write_bytes open open os.replace os.replace",
        "B27: host paths, the platform sends them to paired machines only, never to compute"),
    ("sessions/session_move.py", "discard"): Site(
        AGENTS, "Path.iterdir",
        "B9: asks only whether the staging parent is empty after _real_dir, the removals go through rmtree_beneath and rmdir_beneath"),
    ("sessions/ssh_agent.py", "_chmod_quiet"): Site(OWN, "os.chmod", "its socket and key folders under otodock_dir"),
    ("sessions/ssh_agent.py", "_open"): Site(
        OWN, "Path.mkdir Path.mkdir Path.unlink os.chmod os.chmod os.chmod",
        "the session's agent socket in its run folder under otodock_dir"),
    ("sessions/ssh_agent.py", "_post"): Site(
        NOT_A_PATH, "Path.open",
        "an HTTP request through urllib's opener, no file"),
    ("sessions/ssh_agent.py", "_unlink_own"): Site(
        OWN, "os.unlink",
        "the agent socket it bound in its run folder under otodock_dir, removed only while it is still that file"),
    ("sessions/ssh_agent.py", "_write_public_keys"): Site(
        OWN, "Path.mkdir Path.write_text",
        "public key files in its session-secrets folder under otodock_dir"),
    ("sessions/ssh_agent.py", "stop"): Site(
        OWN, "shutil.rmtree",
        "a session's key folder in its session-secrets under otodock_dir"),
    ("sessions/step_runner.py", "run_step"): Site(
        OWN, "Path.mkdir Path.mkdir Path.read_bytes Path.write_bytes Path.write_text force_rmtree force_rmtree os.chmod os.chmod",
        "its private steps folder under otodock_dir, a compute step goes to run_compute_step"),
    ("sessions/step_runner.py", "wipe_steps_root"): Site(
        OWN, "force_rmtree",
        "its private steps folder under otodock_dir"),
    ("terminal/codex_pty_session.py", "CodexPtySession.start"): Site(
        PERSON, "Path.mkdir Path.write_text Path.write_text",
        "the .codex writes of a paired terminal, a compute start returns to _start_compute"),
    ("terminal/local_socket.py", "LocalControlServer.start"): Site(
        OWN, "Path.unlink os.chmod",
        "the daemon's control socket in its run folder under otodock_dir"),
    ("terminal/local_socket.py", "LocalControlServer.stop"): Site(
        OWN, "Path.unlink",
        "the daemon's control socket in its run folder under otodock_dir"),
    ("terminal/local_socket.py", "socket_path"): Site(
        OWN, "Path.mkdir os.chmod",
        "its run folder under otodock_dir, made by the daemon and by the otodock command"),
    ("terminal/otodock_api.py", "_call"): Site(
        NOT_A_PATH, "Path.open",
        "the otodock command's HTTP request through urllib's opener, no file"),
    ("terminal/otodock_api.py", "_load_login"): Site(
        SANDBOX, "Path.read_text",
        "the otodock command, runs as its caller: reads the caller's own login file"),
    ("terminal/otodock_api.py", "_save_login"): Site(
        SANDBOX, "Path.mkdir os.chmod os.open",
        "the otodock command, runs as its caller: writes the caller's own login file"),
    ("terminal/otodock_api.py", "cmd_files_get"): Site(
        SANDBOX, "Path.write_bytes",
        "the otodock command, runs as its caller: writes the file the caller names"),
    ("terminal/otodock_api.py", "cmd_files_put"): Site(
        SANDBOX, "Path.read_text",
        "the otodock command, runs as its caller: reads the file the caller names"),
    ("terminal/otodock_api.py", "cmd_logout"): Site(
        SANDBOX, "Path.unlink",
        "the otodock command, runs as its caller: removes the caller's own login file"),
    ("terminal/pty_session.py", "PtySession.start"): Site(
        PERSON, "Path.iterdir Path.mkdir Path.unlink Path.write_text Path.write_text shutil.rmtree",
        "B27: .claude writes of a paired terminal, a compute start returns to _start_compute"),
    ("terminal/pty_session.py", "_seed_interactive_cli_config"): Site(
        PERSON, "Path.mkdir Path.read_text Path.write_text",
        ".claude.json of a paired terminal, only PtySession.start calls it, after _compute"),
    ("terminal/pty_session_base.py", "resolve_work_cwd"): Site(
        PERSON, "Path.mkdir",
        "the cwd of a paired start or step, compute starts and steps never call it"),
    ("transport/compute_link.py", "note_refused_sign_in"): Site(
        OWN, "Path.mkdir Path.write_text os.replace",
        "its compute-refused mark under otodock_dir"),
    ("transport/file_sync.py", "_hash_file"): Site(
        AGENTS, "os.open",
        "G1: a hash only, of a file this satellite just wrote or a pull's partial, non-blocking and regular checked, the scan reads through _open_listed"),
    ("transport/file_sync.py", "_open_listed"): Site(
        AGENTS, "os.open",
        "G1: on POSIX open_regular_for_read beneath the walk's hold, os.open by name only on Windows, a paired computer"),
    ("transport/file_sync.py", "_walk_sync_tree"): Site(
        AGENTS, "os.walk",
        "G1: lists names, each folder it visits checked beneath the walk's hold, every read goes through _open_listed"),
    ("transport/file_sync.py", "prune_empty_parents"): Site(
        AGENTS, "Path.rmdir",
        "new: nothing in the satellite calls it, and an rmdir removes only an empty folder"),
    ("transport/lifecycle_update.py", "_finalize_post_update_state"): Site(
        OWN, "Path.read_text Path.unlink Path.unlink Path.unlink Path.write_text Path.write_text shutil.rmtree",
        "its update marker, boot stamp and satellite.previous under otodock_dir"),
    ("transport/lifecycle_update.py", "_self_uninstall_and_exit"): Site(
        OWN, "Path.unlink shutil.rmtree shutil.rmtree shutil.rmtree",
        "its whole state folder and its own systemd unit, a compute machine refuses it"),
    ("transport/lifecycle_update.py", "_self_update_and_restart"): Site(
        OWN, "Path.mkdir Path.read_text Path.read_text Path.unlink Path.unlink Path.unlink Path.write_text Path.write_text os.replace os.replace os.replace shutil.copytree shutil.move shutil.rmtree shutil.rmtree shutil.rmtree shutil.rmtree shutil.rmtree shutil.rmtree",
        "B26: its update staging and install under otodock_dir, hash checked, unpacked by auth_paths.unpack_tarball"),
    ("transport/ws_client.py", "_sweep_satellite_screenshots"): Site(
        PERSON, "Path.glob Path.glob Path.iterdir Path.unlink",
        "B27: workspace .screenshots trimmed, the heartbeat skips it when _compute"),
}

#: The roots of ``test_safe_fs_roots.py`` that the test cannot name, by file
#: and function, each root as written and as often as it is handed.
PROXY_ROOTS: dict[tuple[str, str], Site] = {
    ("api/agents/files.py", "_build_tree"): Site(
        AGENTS, "base",
        "new: the agent's folder as the config names it, opened by its real place as agent_root gives it"),
    ("api/media/media.py", "open_media_row"): Site(
        AGENTS, "root",
        "new: the agents folder or a media cache of the server's, the root split_under picks for the file"),
    ("api/media/uploads.py", "_finalize_staged_file"): Site(
        OWN, "staging_root staging_root",
        "the platform data folder, the parent of the upload staging folder, the staged file reached by rel"),
    ("api/media/uploads.py", "_rename_to_free_name"): Site(
        AGENTS, "src_root",
        "new: the agents folder from two callers, the platform data folder from the third"),
    ("core/file_commit.py", "atomic_replace"): Site(
        OWN, "path.parent",
        "the sessions folder: each state file it saves sits right in it"),
    ("core/remote/file_sync.py", "apply_incoming_file"): Site(
        AGENTS, "agent_dir.parent",
        "new: the agents folder, the parent of an agent's folder from the config"),
    ("core/remote/file_sync.py", "compute_manifest"): Site(
        AGENTS, "agent_dir",
        "new: an agent's folder from the config, opened by its real place as agent_root gives it"),
    ("core/remote/file_sync.py", "prepare_outgoing_files"): Site(
        AGENTS, "agent_dir.parent",
        "new: only tests call it, with an agent's folder from the config, whose parent is the agents folder"),
    ("core/remote/remote_file_cache.py", "_cached_regular"): Site(
        AGENTS, "_host_cache_root().parent",
        "new: the agents folder, the parent of its host cache folder"),
    ("core/remote/remote_file_cache.py", "_make_cache_folder"): Site(
        AGENTS, "_host_cache_root().parent",
        "new: the agents folder, the parent of its host cache folder"),
    ("core/remote/remote_file_cache.py", "_read_old_host_meta"): Site(
        AGENTS, "_host_cache_root().parent",
        "new: the agents folder, the parent of its host cache folder"),
    ("core/remote/remote_file_flow.py", "_remove_host_cache"): Site(
        AGENTS, "root",
        "new: the agents folder for the host cache, the sessions folder for its records"),
    ("core/remote/remote_workspace_apply.py", "_platform_bytes"): Site(
        AGENTS, "agent.parent",
        "new: the agents folder, the parent of an agent's folder from the config"),
    ("core/remote/satellite_file_transfer.py", "SatelliteFileTransferMixin._capture_pre_overwrite._read"): Site(
        AGENTS, "agent.parent",
        "new: the agents folder, the parent of an agent's folder from the config"),
    ("core/remote/satellite_file_transfer.py", "SatelliteFileTransferMixin._workspace_path_checked._check"): Site(
        AGENTS, "agent.parent",
        "new: the agents folder, the parent of an agent's folder from the config"),
    ("core/remote/satellite_file_verbs.py", "SatelliteFileVerbsMixin._open_pull_root"): Site(
        AGENTS, "dest.parent",
        "new: the agents folder for a path in it, else a temporary folder of its own or the host media cache"),
    ("core/sandbox/sandbox.py", "SandboxBuilder._credentials_mask"): Site(
        AGENTS, "agent_root_real agent_root_real",
        "new: the agent's real folder, as _agent_root_real gives it (the realpath of its folder, the root of every bind source)"),
    ("core/sandbox/sandbox.py", "SandboxBuilder._ready_mask_point"): Site(
        AGENTS, "root root root root",
        "new: the agent's real folder, as _agent_root_real gives it, handed in by _shared_text_masks"),
    ("core/sandbox/sandbox.py", "_mkdirs_held"): Site(
        AGENTS, "root_real",
        "new: the agent's real folder, as agent_root gives it"),
    ("core/sandbox/session_config_dir.py", "_a_home_may_mask._names"): Site(
        AGENTS, "root",
        "new: the agent's one root, from beneath_agent in write_no_follow"),
    ("core/sandbox/session_config_dir.py", "_copy_instance_file"): Site(
        OWN, "folder",
        "the folder of people's tool settings in the sessions folder, which only the server writes"),
    ("core/sandbox/session_config_dir.py", "_write_in_place"): Site(
        AGENTS, "root root root root root",
        "new: the agent's one root, from beneath_agent in write_no_follow"),
    ("services/apps/app_blueprints.py", "import_folder._write"): Site(
        AGENTS, "src_root",
        "new: the agent's one root, or a temporary folder of its own with an unpacked bundle or template"),
    ("services/apps/app_deploy.py", "require_entry_page"): Site(
        AGENTS, "root",
        "new: the agent's one root, or a temporary folder of its own with an unpacked bundle or template"),
    ("services/apps/releases.py", "_kind_in_tree"): Site(
        AGENTS, "root",
        "new: as tree_root picks, the agent's one root or a temporary folder of its own with a bundle or template"),
    ("services/apps/releases.py", "_walk_sized"): Site(
        AGENTS, "root",
        "new: as tree_root picks, the agent's one root or a temporary folder of its own with a bundle or template"),
    ("services/apps/releases.py", "copy_tree"): Site(
        AGENTS, "src_root",
        "new: as tree_root picks, the agent's one root or a temporary folder of its own with a bundle or template"),
    ("services/apps/releases.py", "read_manifest"): Site(
        AGENTS, "root",
        "new: as tree_root picks, the agent's one root or a temporary folder of its own with a bundle or template"),
    ("services/apps/releases.py", "read_tree_file"): Site(
        AGENTS, "root",
        "new: as tree_root picks, the agent's one root or a temporary folder of its own with a bundle or template"),
    ("services/community/community_agent_updater.py", "_Run.kept"): Site(
        COPY, "*releases.tree_root(new_tree)",
        "a template app's folder in the server's own temporary unpack of the catalog"),
    ("services/community/community_icons.py", "installed_icon"): Site(
        OWN, "manifest.mcp_dir",
        "an installed tool's own folder in the tool folders, made by the installer"),
    ("services/community/template_app_seeder.py", "write_seed_source"): Site(
        COPY, "src_root",
        "a template app's folder in the server's own temporary unpack or copy of the template"),
    ("services/delegation/result_files.py", "attach"): Site(
        AGENTS, "src_fd",
        "new: a held handle on the worker agent's folder, opened beneath the agents folder"),
    ("services/infra/file_bookkeeping.py", "_delete_file_sync"): Site(
        AGENTS, "root",
        "new: the agents folder, the parent of the agent's folder each caller joins: opened once to the "
        "name's folder, which is then held for the look, the capture and the removal"),
    ("services/infra/file_bookkeeping.py", "_look"): Site(
        AGENTS, "root",
        "new: the agents folder, the parent of the agent's folder each caller joins"),
    ("services/infra/file_bookkeeping.py", "_only_case_differs"): Site(
        AGENTS, "root",
        "new: the agents folder, the parent of the agent's folder each caller joins"),
    ("services/infra/file_bookkeeping.py", "retire_aged_file_sync"): Site(
        AGENTS, "root root root",
        "new: the agents folder, the parent of the agent's folder each caller joins"),
    ("services/infra/file_bookkeeping.py", "walk_files_beneath"): Site(
        AGENTS, "root root",
        "new: the agents folder, the parent of the agent's folder each caller joins"),
    ("services/infra/history_move.py", "_copy_aside"): Site(
        OWN, "git_writer.history_root()",
        "the history folder in the sessions folder, made for the server alone and never bound into a session"),
    ("services/infra/retention.py", "_remove_beneath"): Site(
        AGENTS, "root root",
        "new: the agent's one root from beneath_agent, or the agents folder"),
    ("services/infra/storage_quota.py", "dir_usage"): Site(
        AGENTS, "root",
        "new: an agent's root, the agents, recover or sessions folder, or an old one in the server's home, only sized"),
    ("services/mcp/mcp_manifest_parse.py", "read_skill_file"): Site(
        OWN, "mcp_dir",
        "an installed tool's own folder in the tool folders, made by the installer"),
    ("services/mcp/mcp_output_relocation.py", "_relocate_local"): Site(
        OWN, "mcp_dir mcp_dir mcp_dir",
        "an installed tool's own folder in the tool folders, made by the installer"),
    ("services/media/media_pipeline.py", "_media_input"): Site(
        AGENTS, "root",
        "new: the agents folder or a media cache of the server's, the root split_under picks for the file"),
    ("services/media/media_pipeline.py", "remove_cache_copies"): Site(
        AGENTS, "root",
        "new: the agents folder or a media cache of the server's, the root split_under picks for the file"),
    ("services/sharing/chat_snapshot.py", "_Copier.copy_token"): Site(
        AGENTS, "root",
        "new: the agents folder or a media cache of the server's, the root split_under picks for the file"),
}

SATELLITE_ROOTS: dict[tuple[str, str], Site] = {
    ("sessions/resume_probe.py", "_open_probe"): Site(
        AGENTS, "agent_dir",
        "new: an agent's folder, the config's agents folder joined with a checked name"),
    ("sessions/session_files.py", "wipe_agent_files"): Site(
        AGENTS, "agent_dir",
        "new: the agent's folder kept at the write, the config's agents folder and a checked name"),
    ("sessions/session_manager.py", "SessionManager._file_pull.opener"): Site(
        AGENTS, "_root",
        "new: the config's agents folder, bound as the opener's default"),
    ("sessions/session_move.py", "commit"): Site(
        AGENTS, "agents_dir agents_dir agents_dir agents_dir",
        "new: the config's agents folder, handed in by the table of move verbs"),
    ("sessions/session_move.py", "delete"): Site(
        AGENTS, "agents_dir agents_dir",
        "new: the config's agents folder, handed in by the table of move verbs"),
    ("sessions/session_move.py", "discard"): Site(
        AGENTS, "agents_dir agents_dir",
        "new: the config's agents folder, from the table of move verbs or from commit"),
    ("sessions/session_move.py", "list_files"): Site(
        AGENTS, "agents_dir",
        "new: the config's agents folder, handed in by the table of move verbs"),
    ("sessions/session_move.py", "stat_files"): Site(
        AGENTS, "agents_dir",
        "new: the config's agents folder, handed in by the table of move verbs"),
    ("terminal/codex_pty_session.py", "CodexPtySession._fresh_rollout"): Site(
        AGENTS, "where",
        "new: a held handle, on the agent's folder or a folder the walk opened beneath it"),
    ("terminal/codex_pty_session.py", "_read_session_meta"): Site(
        AGENTS, "root",
        "new: the agent's folder from config, or a held handle opened beneath it"),
    ("terminal/codex_pty_session.py", "_walk_rollouts"): Site(
        AGENTS, "folder",
        "new: a handle opened beneath a held handle on the agent's folder"),
    ("transport/file_sync.py", "_folder_refused"): Site(
        AGENTS, "root",
        "new: the walk's held handle on the agent's folder"),
    ("transport/file_sync.py", "_held"): Site(
        AGENTS, "agent_dir",
        "new: an agent's folder in the config's agents folder, by a session's config or a listing of it"),
    ("transport/file_sync.py", "_open_listed"): Site(
        AGENTS, "root",
        "new: the walk's held handle on the agent's folder"),
    ("transport/file_sync.py", "apply_file_push"): Site(
        AGENTS, "Path(agent_dir).parent",
        "new: the config's agents folder, the parent of an agent's folder named by a checked name"),
}
