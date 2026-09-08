# Monitoring examples

For OS-timed HTTP checks that stay quiet while healthy and request recovery only after confirmed
failure, use the [health-hook recipe](HEALTH-HOOK.md). The probe example includes persistent outage
IDs and a consecutive-failure threshold; actual recovery needs a service-specific policy.


These are example requests for the installed skill, not claims that every external integration is
bundled. Use a real file path or an already configured producer. Each monitor belongs to an explicitly
chosen conversation; several monitors can feed one conversation, and different conversations can
have independent monitors.

### Keep working while a build finishes

```text
$codex-monitor Watch /absolute/project/build-status.json in this conversation.
Notify me when its contents change, and keep unchanged observations quiet.
```

The managed collector sends change metadata, not file contents. Ask the conversation to read the
file when needed. Its first observation establishes a silent baseline.

### React only when a condition changes

```text
$codex-monitor Watch /absolute/project/build-status.json. Notify this conversation
when /build/status becomes "failed" for five seconds, and when it recovers.
```

JSON predicates and debounce run outside the model. The initial valid observation stays silent even
if already matched; request a separate initial status check if needed. Invalid samples are errors,
not successful matches. See [condition policies](CONVERSATION-MONITORS.md).

### Give a PM conversation updates from several workers

```text
$codex-monitor Receive events from my configured worker sources in this PM conversation.
Act on blocked, failed and completed work. Preserve the worker identity and request ID.
Keep ordinary progress chatter out of the event stream.
```

Workers need an adapter that emits authenticated events with stable IDs and filters routine updates.
Attaching a source does not launch a worker or discover every Codex agent. Use the
[request lifecycle](REQUEST-LIFECYCLE.md) for explicit progress and terminal states; message
delivery alone does not complete the request.

### Use an optional relay conversation

```text
$codex-monitor Use this conversation as the receiver for my configured messaging source.
Forward relevant requests, their original content and metadata to the PM conversation I specify.
Let the PM provide the substantive response; send it back only within my authorized reply workflow.
```

Choose the relay's model in Codex if you want routing and PM reasoning to use different models.
A relay is optional: the PM can receive events directly. Discord requires a separate Gateway
producer and reply adapter; these are not bundled Discord features. Producer authentication, source
scoping and explicit replies still apply. See [adapter contracts](ADAPTERS.md).

### Check or pause one conversation's monitoring

```text
$codex-monitor Show the monitors in this conversation, receiver state, collector errors
and last delivery evidence. Distinguish unknown source health from a confirmed failure.
```

```text
$codex-monitor Pause the build monitor in this conversation. Leave other monitors running.
```

Status commands do not create model turns; asking an assistant to interpret them uses an ordinary
conversation turn. Pause affects future delivery and cannot retract input already accepted by Codex.
