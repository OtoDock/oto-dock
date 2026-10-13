# phone/: the OtoDock phone service

An optional daemon that lets an agent take and place phone calls. It answers
a call, runs the conversation through speech-to-text, the agent and
text-to-speech, and speaks the reply on the line. It holds no configuration
of its own beyond how to reach the platform: routes, numbers, providers and
credentials are set in the dashboard (Setup, Phone) and pushed to it.

## Two ways in

- **A PBX (Asterisk or FreePBX).** The PBX streams each call's audio to the
  daemon's AudioSocket server (TCP, port 9092 by default,
  `telephony/audio_socket.py`). Outbound calls go through the PBX's manager
  interface (AMI, `telephony/ami_client.py`). A transfer is an AMI `Redirect`
  on the daemon's persistent event connection (`telephony/transfer.py`,
  `telephony/ami_events.py`).
- **Twilio.** A number's voice webhook points at the platform's public
  address: the proxy relays the `/v1/twilio/*` requests and the media
  stream to the daemon byte for byte (`proxy/api/phone/twilio_relay.py`), so
  the daemon's own port stays private. The daemon answers with TwiML that
  opens a Twilio Media Stream (`calls/twilio_http.py`,
  `telephony/twilio_media.py`) and checks Twilio's signature on every
  request.

Both reach one per-call pipeline (`pipeline/core.py`): voice activity and
turn detection, speech-to-text, the agent's reply, text-to-speech, with
barge-in. The dashboard's live voice conversations reuse the same pipeline
over a websocket instead of a phone line (`duplex/`). Once the proxy and the
daemon both run 1.8.0 the agent has three call tools on every call
(`mcps/custom/call-control-mcp`): end the call,
transfer it to a number the route allows, and, on a call another agent
placed, ask that agent a question mid-call. The daemon carries the action
out once the reply has finished playing (`pipeline/call_control.py`).

## How it reaches the platform

The daemon connects **out** to the proxy, which needs no route to it for
configuration:
- `/ws/phone-management` (`proxy/management_ws.py`): the configuration push
  at start and on every change in the dashboard.
- `/ws/phone` (`proxy/client.py`): one websocket per call, on which the
  agent's turns run as a phone session on the platform.
- `/ws/duplex-engine/{id}`: the daemon dials back for each live voice
  conversation a dashboard opens.
- A few HTTP calls back (the call log, the PIN gate, usage).

The proxy calls the daemon's HTTP API (port 9093, `calls/http_api.py`) to
place outbound calls and to relay Twilio. That API requires the shared
`PHONE_API_SECRET` (Twilio's paths authenticate by signature instead). The
speech providers come from the `audio/` package with its `localmodels` extra
(the on-box voice activity and turn models).

## Run it

- **Docker:** `scripts/install.sh` stacks `docker-compose.phone.yml` on the
  base compose file (`COMPOSE_FILE=docker-compose.yml:docker-compose.phone.yml`
  in `.env`), so the default install runs the daemon.
- **Bare metal:** `scripts/dev-setup.sh --phone` builds `phone/venv` (and,
  with `--service`, the `otodock-phone` unit). `phone/.env` holds
  `PROXY_URL` and the shared secrets when they are not in `config.env`.

Until a PBX or a Twilio account is set up in the dashboard, the daemon takes
no calls.

## Tests

```bash
cd phone && ../proxy/venv/bin/python -m pytest
```

The suite borrows the proxy's environment and needs no database or phone
line. Setup and the other suites are in
[`CONTRIBUTING.md`](../CONTRIBUTING.md), and the user documentation (setting
up a PBX, Twilio, routes and caller data) is at
[docs.otodock.io](https://docs.otodock.io).
