# Hermes Alexa Bridge

Talk to Hermes from a stock Echo using an Alexa Custom skill. House is the shared default; an operator can give each Echo its own conversation. Alexa acknowledges receipt without waiting for Hermes or Home Assistant. While a conversation is active, another invocation in that conversation can steer its existing run. The final answer goes **only to the Echo that started that run**.

## Install and configure

Follow the [installation guide](docs/install.md) to configure one public HTTPS skill route on nginx, connect to private Hermes and Home Assistant, and map each Echo without publishing its IDs. The repo includes the runnable Python app, dependency manifest, nginx example, Alexa interaction model and synthetic config examples. You supply the host, TLS and DNS, a development skill, a private Hermes Runs API and an HA Speak entity per Echo.

From the checkout, with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# After creating private config/routes and setting both tokens as documented:
.venv/bin/python server.py check --config "$HOME/.config/hermes-alexa/config.json"
.venv/bin/python server.py serve --config "$HOME/.config/hermes-alexa/config.json"
```

The app listens only on `127.0.0.1:8765` by default. nginx forwards only `POST /alexa`. There is no unsigned mode, public enrollment endpoint or upstream API proxy. `check` loads the real SDK and validates local configuration without binding or making network calls. Live Alexa signatures, Hermes continuity and physical Echo playback still require on-site acceptance.

## Try the synthetic demo

From the project root, with Python 3.10+ (tested on Windows with 3.14):

```sh
python -B -m unittest discover -v
python -B demo.py
```

No pip packages, logins, tokens or VM are required for the demo. It exercises three synthetic Alexa requests through the real bridge and loopback test services. The demo verifier is a fixture, **not** Alexa authentication. With `requirements.txt` installed, the test suite additionally starts the real CLI, checks that unsigned HTTP fails, and verifies a locally generated test signing chain with the real ASK verifier. Neither exercise uses live Alexa, Hermes or HA.

## How it works

- `server.py` provides `check`, `serve`, and one-shot private `capture`. The HTTP adapter limits connections, workers, headers and body. It rejects duplicate `Signature-256` and `SignatureCertChainUrl` headers and passes the unmodified bytes to `ingress.py`, which uses Amazon’s ASK SDK to validate the signing chain and signature and rejects stale signed timestamps.
- `bridge.py`: exact skill ID and `(user ID, device ID)` allowlist, signed timestamp freshness ±150 seconds, bounded in-memory scheduling, and request-ID deduplication for at least 305 seconds. Resolve the configured conversation before looking for an active run. No broadcast or Alexa-session-based routing.
- `clients.py`: create a run with `POST /v1/runs`, `X-Hermes-Session-Key`, and a request-scoped `Idempotency-Key`; poll `GET /v1/runs/{run_id}`. A follow-up targets that run with `POST /v1/runs/{run_id}/steer` and `{"input":"…"}`. An accepted steer is queued for the next tool boundary, not an interruption or proof that the model read it. The bridge reads terminal `pending_steer` from status, or checks the matching terminal SSE event when canonical status omits the field. Only complete runs speak, using `POST /api/services/notify/send_message` to one HA Speak entity, with plain text capped at 600 characters. HA's success means service accepted, not that sound was heard.
- A request received while a run ID is still being obtained can queue as an ordinary follow-up. A refused steer is never promoted to a new turn: a later terminal status does not prove that the refusal was a harmless completion race. The bridge replays an accepted or uncertain steer only if trustworthy terminal `pending_steer` maps unambiguously to the original request and no pause or stop was observed (including canonical `pending_controls`). Each replay uses the original request’s Echo and idempotency key. Alexa acknowledges receipt, not upstream acceptance or playback. If a correction gets no reply, wait until the conversation is idle and ask again explicitly if it is still needed; an unconfirmed steer may already have been applied.
- Defaults: four outstanding requests (including steers awaiting disposition), 256 dedup records, 120-second queue age, 250 ms poll interval, 10-minute run observation budget, 32 KiB Alexa body and 1 MiB Hermes read limits. One background worker serializes HTTP calls across conversations; a slow upstream can delay controls, not Alexa acknowledgments. Socket timeouts (Hermes 75 s; terminal event lookup at most 5 s per socket operation; HA 10 s) are **not** absolute deadlines. If a create fails without a usable run ID, or run observation times out, the bridge blocks that history for the life of the process. Already queued requests for it are dropped without speech and later requests report busy; other histories can continue. Do not restart to clear the block until you have verified that the earlier create did not start a run or that the run has ended. The bridge does not resolve Hermes `unknown` runs, answer approvals, or retry HA speech. Failure and ambiguity produce counters rather than audible diagnostics. Queue, run ownership, and dedup state are volatile; use one bridge instance per voice profile and do not assume crash-proof delivery.

### Default conversation configuration

In the private file shaped like [`examples/routes.example.json`](examples/routes.example.json), set `default_conversation` to `house` (or omit it) or `echo`. If several allowed users map to the same Echo, all of those rows must agree. House retains `alexa:household:voice:v1`; Echo-only keys use a separate namespace and a stable hash of the configured skill and device IDs, not the HA entity, display name, speaker, or Alexa session ID. A default change selects a different history on future requests; it does not move or delete anything. Drain the bridge before applying a new policy. There is no dashboard, hot reload, voice override, or speaker authentication.

### References

- [Hermes API Server: Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server#runs-api-streaming-friendly-alternative), [Programmatic integration: HTTP steering](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration), [inspected Runs routes](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e/gateway/platforms/api_server_runs.py).
- Amazon: [HTTPS signature and timestamp verification](https://developer.amazon.com/en-US/docs/alexa/custom-skills/host-a-custom-skill-as-a-web-service.html), [ASK Python SDK](https://github.com/alexa/alexa-skills-kit-sdk-for-python), [Lambda skill ID verification](https://developer.amazon.com/en-US/docs/alexa/custom-skills/host-a-custom-skill-as-an-aws-lambda-function.html), [SearchQuery constraints](https://developer.amazon.com/en-US/docs/alexa/custom-skills/slot-type-reference.html#searchquery), [eight-second response budget](https://developer.amazon.com/en-US/docs/alexa/custom-skills/send-the-user-a-progressive-response.html).
- Home Assistant: [Alexa Devices](https://www.home-assistant.io/integrations/alexa_devices/), [REST API](https://developers.home-assistant.io/docs/api/rest/).

## License

[MIT](LICENSE). The examples use synthetic identifiers. Keep operator configuration, databases, logs, receipts, and credentials outside the repository.
