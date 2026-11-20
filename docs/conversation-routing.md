# Conversation routing proposal

The prototype implements configuration-only defaults and native Runs steering. The UI and voice selectors below are future work; see the README for live acceptance gates.

Each Alexa invocation is a separate request. A conversation is a durable Hermes history, not an Alexa session or a speaker. For the first setup, all configured Echos default to House.

## Choosing a conversation

Give each configured Echo a setting named **Default conversation** with two choices: **House (shared)** and **This Echo only**. Show the current setting beside its display name. Changing the default applies to future requests; it neither moves nor deletes history and does not reroute already accepted work. The display name is not a routing identifier. Use a stable configured device mapping. A per-Echo history is not speaker authentication or inherently private.

Optional voice shortcuts select a conversation for one request, without changing any setting: “Alexa, ask my assistant to use the house conversation to compare those options” and “Alexa, ask my assistant to use this Echo’s conversation to help plan dinner.” These phrases are examples pending Alexa invocation testing. Use explicit routing syntax, not model inference about casual words like “kitchen,” and ask the user to restate an unsupported or unclear selector rather than silently switch. In a later settings UI, expose named controls and current values to screen readers; do not rely on voice-only setup.

## In-flight behavior

Resolve the conversation key before checking for active work. On an idle history, create a turn. While a run ID is being obtained, an unattempted request can queue as an ordinary follow-up. If there is no usable create receipt or the run cannot be observed to completion, block that history in this process: drop queued requests without speech, report busy for later requests, and leave other histories available. Before restarting to clear the block, verify that no run was started or that it has ended.

Once the run ID is known, send an update to that run’s steer endpoint. A receipt means Hermes queued it for a tool boundary, not that the model consumed it. Never turn a refusal into a separate turn: the next poll may be terminal even if a pause or stop caused the refusal. Replay only exact, unambiguously matched terminal `pending_steer` evidence from status or the matching terminal SSE event, and not if polling observed a pause, stop, or canonical `pending_controls`. Acknowledgement is not a promise of a reply. If a correction gets no reply, wait until the conversation is idle before explicitly asking again if needed; an unconfirmed steer may already have been applied.

Never steer House from an unrelated Echo-only conversation. Speak the final answer only on the Echo that started the run. An undelivered steer with sufficient evidence to become a new turn uses the original update’s Echo.

## Acceptance

1. On one Echo, start a request, hear its response, then submit a separate invocation that requires the prior answer.
2. Continue that same House conversation from another Echo and hear its answer only there.
3. While a House run is active, send a correction from a configured Echo. Verify the correction reaches the existing run and changes the result; separately verify that a refused steer followed by terminal status does not create a follow-up and that an unambiguously pending steer can replay when no control was observed.
4. Set one Echo to Echo-only in the operator config. Verify repeated requests there retain their own context and do not steer House. Verify explicit House selection only if a future voice selector is implemented.
5. Confirm actual Alexa interpretation and audible responses on both devices before describing these flows as live-tested.
