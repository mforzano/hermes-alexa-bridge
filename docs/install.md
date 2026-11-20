# Install the Alexa HTTPS skill bridge

Run one bridge process on an always-on host. Alexa calls **only** `https://alexa.example.org/alexa` (replace this hostname). nginx ends TLS and forwards the unmodified skill POST to the same host's `127.0.0.1:8765`. The bridge calls private Hermes and Home Assistant (HA) origins. The only other application egress is HTTPS to Amazon S3 for ASK signing certificates. Do not publish either upstream or forward the bridge port. Use a trusted host without untrusted local users or processes; the proxy-to-bridge hop is local HTTP.

## 1. Install the app

From the root of a checkout that includes `server.py`, on a Linux host with Python 3.11+ and nginx (Windows Python smoke instructions below):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -B -m unittest discover -v
```

Use a supported Python and system CA store (`ca-certificates` on Debian/Ubuntu). The requirements pin Amazon ASK `1.3.3`, Waitress, and an upstream `oscrypto` revision for OpenSSL 3 compatibility. Installation needs PyPI and GitHub package access. Use the real SDK tests as a gate on the intended host. Windows: substitute `py -3 -m venv .venv` and `.venv/Scripts/python.exe` for the Python commands (use `python` if the `py` launcher is unavailable); protect the private configuration directory with NTFS ACLs. The nginx setup below is for Linux.

## 2. Get private upstreams ready

**Hermes:** Use a dedicated voice-only profile and verify effective tool, connector and memory isolation before putting it on a shared-room speaker. In that profile's protected `.env` set `API_SERVER_ENABLED=true`, `API_SERVER_HOST=127.0.0.1`, `API_SERVER_PORT=8642`, and `API_SERVER_KEY` to a strong, unique bearer key (e.g. generated from 32 random bytes). Start or restart that profile's `hermes gateway` using the site's usual supervisor. Point the bridge at `http://127.0.0.1:8642` if co-located; otherwise use an authenticated, hostname-valid HTTPS origin reached over the private network. Verify authenticated `GET /v1/capabilities` privately: require create, status, steer, event replay, and `X-Hermes-Session-Key`. Check the installed Runs implementation actually resumes keyed history and exposes trustworthy terminal `pending_steer` when applicable. A Responses-only backend won't work. See the [Hermes API Server docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) and [routing details](conversation-routing.md).

**Home Assistant:** Follow the [Home Assistant setup guide](home-assistant.md) to connect your Amazon account, test one Echo's Speak entity from the dashboard, and create the bridge credential. Before continuing, have the exact tested `notify.…` entity, an HA origin reachable from the bridge host, and the token in private storage. You do not need the Alexa development skill to test HA speech.

## 3. Set up one public HTTPS route and a development skill

1. Use a dedicated DNS hostname pointed at the trusted nginx host. Make TCP 443 reachable by Alexa. Obtain a publicly trusted certificate for that exact hostname with your existing ACME/certificate workflow, including the full chain and renewal. No Alexa account linking, public HA/Hermes ports or Lambda are required. Allow the bridge's HTTPS egress to `s3.amazonaws.com`; keep host time synchronized. The public virtual host must not serve the checkout, private config files, or any upstream API.
2. For a Debian-style nginx layout, copy the example, replace both occurrences of `alexa.example.org` outside the certificate paths, set both certificate paths, and test before reloading (leave the default site and other vhosts alone):

   ```bash
   sudo install -m 0644 examples/nginx.alexa.conf /etc/nginx/sites-available/hermes-alexa
   sudoedit /etc/nginx/sites-available/hermes-alexa
   sudo ln -s /etc/nginx/sites-available/hermes-alexa /etc/nginx/sites-enabled/hermes-alexa
   sudo nginx -t && sudo systemctl reload nginx
   ```

   On other layouts, include the file once inside nginx's `http` context. Don't change the request body, consume either signature header, enable request mirroring, or enable header/body logging anywhere in front of the bridge. Keep port `8765` loopback-only. The example limits concurrent service requests and request rate, rejects other paths and methods, and caps the body at 32 KiB. Timeouts are not a hard end-to-end deadline for all network operations; measure prompt acknowledgments on cold and warm invocations before regular use.
3. In the **Alexa Developer Console**, select **Create Skill**, choose the Echo’s locale, **Custom** model, **Provision your own** hosting, and **Start from Scratch**. In **Build → Interaction Model → JSON Editor**, paste [`examples/interaction-model.en-US.json`](../examples/interaction-model.en-US.json) for en-US, save and build. For other locales, adapt the invocation and utterances and confirm that locale supports `AMAZON.SearchQuery`.
4. Under **Build → Endpoint**, select **HTTPS** and put `https://alexa.example.org/alexa` in **Default Region**. Select the option for a certificate from a trusted certificate authority, then save. Copy the skill ID from the developer console for local use only. Under **Test**, enable **Development**. Use a physical Echo registered with the authorized developer/test account, not the web simulator when collecting the device mapping. Leave the skill in development; do not submit it to the store.

## 4. Map one Echo at a time without publishing IDs

From the checkout on the bridge host, as the user who will run it:

```bash
umask 077
private="$HOME/.config/hermes-alexa"
mkdir -p "$private"
chmod 700 "$private"
cp examples/bridge.example.json "$private/config.json"
cp examples/routes.example.json "$private/routes.private.json"
chmod 600 "$private/config.json" "$private/routes.private.json"
read -r -p 'Alexa skill ID (local only): ' SKILL_ID
.venv/bin/python server.py capture --skill-id "$SKILL_ID" --output "$private/first-echo.private.json"
```

Do this with `serve` stopped and only your controlled development testers enabled. In the first **120 seconds**, say **“Alexa, open Hermes Helper”** on the specific physical Echo. The signed launch for this exact skill is verified and timestamp-checked; only the first one in the window writes a new private file containing the two IDs. Capture has no upstream credentials or dispatch, says “Echo pairing recorded” only after saving the capture, and does not auto-enroll a device or expose an ID lookup. Stop capture with Ctrl+C, then open the file in a private local editor (or a trusted private terminal without session recording), not in a web page or shared chat. If the file was not created, verify endpoint reachability, test-account ownership and that the phrase was a launch. Confirm only your intended Echo invoked during that window; if unsure, discard that capture and redo it. Restart the capture command with a new output filename for each Echo and account pair. It will not overwrite an existing capture.

Edit `routes.private.json` privately. Set `skill_id` to the console ID, remove **both** synthetic rows and create one row per captured pair with `user_id`, `device_id`, and the verified `entity_id` (`notify.…`). The default conversation is `house`; optionally set `default_conversation` to `echo` for every row on the same device. Never map several physical devices to one notify entity. Remove the temporary captures after copying, or keep the intended Echo’s capture private until optional Voice ID registration is finished; never commit or paste real IDs or tokens in issues, logs, or chat. Edit `config.json` with both private origins, scheme and optional port only, no path, token, or query. `routes_file` is relative to the config directory unless given as an absolute path.

For what pairing grants and how to opt into automatic owner/guest routing, see [Onboarding: pairing and access](onboarding.md). Keep the private pair capture until the one-time Voice ID registration is finished if you plan to enroll an owner.

Optional: set the top-level JSON boolean `"omit_google_maps_urls": true` in this installation’s `config.json` to omit Google Maps links from the first spoken page and all continuations, in both guest and owner lanes. Omit the key or set it to `false` to keep them. The bridge strips typical Markdown link destinations (keeping the labels) and bare links such as `google.com/maps`, regional `maps.google.*`, `maps.app.goo.gl`, and `goo.gl/maps`. It does not fetch URLs, follow redirects, or classify generic shorteners. Names, addresses, other sites, and the original Hermes output and chat are unchanged by the filter.

## 5. Check, start, and test one Echo

Use a private Bash terminal, with shell tracing off. Enter both bearer keys when prompted (input hidden); don't put keys into command arguments or Git:

```bash
set +x
read -rs -p 'Hermes API key: ' HERMES_ALEXA_HERMES_TOKEN; printf '\n'
read -rs -p 'HA REST token: ' HERMES_ALEXA_HA_TOKEN; printf '\n'
export HERMES_ALEXA_HERMES_TOKEN HERMES_ALEXA_HA_TOKEN
.venv/bin/python server.py check --config "$private/config.json"
.venv/bin/python server.py serve --config "$private/config.json"
unset HERMES_ALEXA_HERMES_TOKEN HERMES_ALEXA_HA_TOKEN
```

`check` validates local configuration and dependency imports; it doesn't test credentials or upstream reachability. `serve` runs in the foreground; Ctrl+C stops it. For unattended use, put the same `serve` command with absolute interpreter, script and config paths under your trusted service supervisor as an unprivileged user; inject the two variables from a protected secret file/store. Allow at least 20 seconds for shutdown and stop while idle; work and dedup state aren't durable. Do not blindly restart an ambiguous in-flight create or run.

From another terminal, first verify rejection (never send live IDs or tokens in a probe):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  --data-binary '{}' http://127.0.0.1:8765/alexa
# 400 with the app listening
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  --data-binary '{}' https://alexa.example.org/alexa
# 400 with valid TLS and the proxy reaching the app (do not use -k)
curl -sS -o /dev/null -w '%{http_code}\n' https://alexa.example.org/v1/runs
# 404; there is no public upstream/API proxy
```

Then say **“Alexa, open Hermes Helper.”** After **“What can I help you with?”**, say **“What is two plus two?”** without a prefix. Also try the one-shot **“Alexa, ask Hermes Helper to tell me what two plus two is.”** The `find out` and `ask a question` one-shot phrases still work. Expect **“Working on it.”**, then a separate spoken answer **on that Echo only**. Opening the skill alone starts no run. Say **“cancel”** or **“stop”** while Alexa is listening to close the skill dialog; this does not cancel any Hermes run already submitted. Use your own invocation name if you changed it.

For an existing development skill, back up its current model, then update and successfully build the model in **Build → Interaction Model → JSON Editor** for the tested locale. Preserve your existing invocation name and unrelated customizations. Add the example’s `ReadMoreIntent` with the utterance `read more`, and keep `AskHermesIntent`’s slot dialog so “read more” also works after opening the skill. Test both forms after a long reply on the same Echo with Alexa Voice ID and **Personalize skills** enabled, and verify another Echo cannot continue that reply. The dialog response needs the new model as well as the new bridge code; arrange the two updates together and don't test the new launch flow against the old model. The example uses [Alexa's slot elicitation directive](https://developer.amazon.com/en-US/docs/alexa/custom-skills/dialog-interface-reference.html#elicitslot-directive) and keeps the unprefixed utterance at [the search slot level](https://developer.amazon.com/en-US/docs/alexa/custom-skills/slot-type-reference.html#amazonsearchquery), not at intent level.

Test with a second mapped Echo and an unmapped one. Before depending on it, verify shared recall across Alexa sessions and after an idle restart, Echo-only isolation, steering without moving the original reply, and behavior on HA failure. A successful HA REST call is not proof of audible playback. If unsigned probes pass but the real skill fails, check TLS chain, the exact endpoint and auth headers, host time, certificate egress, allowlist and ASK latency; don't add payload logging or disable checks. For teardown, disable development testing, remove this proxy site, stop the bridge and revoke its credentials. Existing transcripts are not deleted.
