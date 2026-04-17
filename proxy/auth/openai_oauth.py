"""OpenAI / Codex OAuth endpoint constants.

The live OpenAI subscription flow is a device-auth flow handled in
``api/auth/openai_oauth.py`` (it shells out to ``codex login --device-auth`` and
accepts the pasted token JSON); token refresh is done inline by
``services.engines.subscription_pool._refresh_openai_oauth_token``. This module only
holds the OAuth token endpoint and the public client id those paths share.
Both come from the Codex CLI source (``codex-rs/login/src/server.rs``); the
client id is public, not a secret.
"""

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
