# Set up Home Assistant for Echo replies

Home Assistant speaks Henry's completed reply on one Echo. The Alexa development skill is a separate step that sends the original question to Hermes. You can finish and test this page before creating the skill, setting up nginx, or running the bridge.

## 1. Add Alexa Devices

1. Open your existing Home Assistant dashboard and sign in as a Home Assistant administrator. Use the actual dashboard address for your installation. If you run Home Assistant OS in a VM and aren't sure of the address, check the VM console for its IP and use `ha core info` to check the web port and whether SSL is enabled. `8123` is the default, not a requirement. The Observer on `4357` is not the dashboard.
2. In the Alexa app, under **Devices**, find the physical Echo you want to test and check its name. Use the Amazon account the Echo is registered to.
3. In that Amazon account, open **Login & Security → 2-step verification → Backup methods → Add new app** if you do not already have an authenticator app set up. Follow Amazon's enrollment prompts and set the authenticator app as the preferred verification method. Home Assistant asks for the current code from that app, not a text-message code.
4. In Home Assistant, go to **Settings → Devices & services → Add integration**. Search for and select **Alexa Devices**, the built-in integration introduced in Home Assistant Core 2025.6. Enter your Amazon email, password, and the current code from your authenticator app in the Home Assistant setup form. If the integration is missing, check **Settings → About** for the Core version. If sign-in fails, check that a fresh sign-in to Amazon prompts for an app code and retry with a fresh code.

## 2. Find and test one Speak entity

1. Under **Settings → Devices & services → Integrations**, open **Alexa Devices**, open its device list, and select the Echo you identified in the Alexa app. Open its **Speak** entity and copy its **Entity ID** from the entity settings. It starts with `notify.`. If the entity is disabled, enable it in its settings. Use **Speak**, not **Announce**, a speaker group, or the `media_player` entity. If you can't find the Echo, check its account and connection in the Alexa app; if you see the Echo but no Speak entity, check disabled entities and whether that model exposes notifications.
2. In Home Assistant, open **Developer tools → Actions**. Select **Send a notification message** (`notify.send_message`). Under **Target**, select the one **Speak entity**, not an area or device group. Enter `This is a Home Assistant voice test.` in **Message** and select **Perform action**. Alternatively, use YAML mode and replace the example entity ID:

   ```yaml
   action: notify.send_message
   target:
     entity_id: notify.example_echo_speak
   data:
     message: This is a Home Assistant voice test.
   ```

3. Confirm you actually heard the sentence on that physical Echo. If the action succeeds but you hear nothing, verify the target, device is online, volume and Do Not Disturb, and that speech works in the Alexa app. If it spoke on a different Echo, correct the entity and repeat. Don't copy an entity into the bridge until you have confirmed the room.

## 3. Give the bridge private API access

1. Sign in as the Home Assistant user whose permissions the bridge should use. Open that user's **Profile → Security → Long-lived access tokens → Create token**, name it `Hermes Alexa Bridge`, and copy it when shown. The token inherits the user's permissions; it is not limited to the one Speak entity. Use the least-privileged account that can perform the action.
2. On the bridge host, supply that token privately as `HERMES_ALEXA_HA_TOKEN` using the [start commands](install.md#5-check-start-and-test-one-echo). Keep it out of chat, command arguments, configuration JSON, Git, and logs. The bridge host must be able to reach `ha_origin`, which is a scheme, host and optional port only. On a separate host or VM, use a hostname-valid private HTTPS origin, for example `https://ha.example.internal`; the bridge accepts plain HTTP only at literal loopback for a co-located instance. A LAN dashboard URL that works over HTTP in your browser is not by itself a working `ha_origin`. If it is HTTP-only, arrange a private HTTPS listener or proxy with a certificate the bridge host trusts before continuing. Do not forward Home Assistant to the public internet.
3. In the bridge's private routing file, use the tested `notify.…` value as `entity_id`. The Alexa `user_id` and `device_id` are different identifiers; obtain those from the [signed capture step](install.md#4-map-one-echo-at-a-time-without-publishing-ids), not from the Home Assistant device page. Repeat the test and capture separately for each Echo.

## Hand this to your setup agent

Give your agent the repo and this guide. Tell it the Home Assistant dashboard URL, where the bridge will run, and which Echo to test first. Ask it to verify dashboard reachability and guide you through each Home Assistant step, then stop for you to complete account sign-in and hear the test sentence. Provide the tested Speak entity for local configuration and confirm what room played the test. Have the agent use the private credential-entry method on your host for the token, not chat. From there it can follow [the installation guide](install.md) to configure the private upstreams and the Alexa skill. If the Home Assistant action works but the bridge doesn't, check the bridge's private HTTPS origin and API credential separately from Alexa's public skill endpoint.

Official references: [Alexa Devices integration](https://www.home-assistant.io/integrations/alexa_devices/), [Home Assistant authentication](https://www.home-assistant.io/docs/authentication/), [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/).
