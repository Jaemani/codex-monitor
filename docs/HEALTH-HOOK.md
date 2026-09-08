# OS-timed health checks with event-only recovery requests

Use an OS timer to run a small probe, rather than scheduling an assistant to ask whether a server is
alive. The [example hook](../examples/health-hook.py) performs one HTTP check per invocation. No model
is called for a healthy check. Three consecutive failed invocations produce an external event for an
existing operations conversation. The example never executes recovery commands itself.

```text
OS timer -> HTTP probe -> healthy: exit quietly
                      -> confirmed failure: stored event ID -> codex-monitor -> operations conversation
                                                                              -> authorized recovery
```

## Configure the receiving conversation

Use an existing conversation ID and the same receiver state throughout. Set up the source and
binding before starting a new receiver; an existing receiver needs a controlled restart to load a new
source token. Do not reinitialize existing state.

```bash
MONITOR="$HOME/.local/share/codex-monitor/bin/codex-monitor"
"$MONITOR" source health-probe
"$MONITOR" attach service-ops --thread "$THREAD_ID" --source health-probe
```

Give that conversation a concrete recovery policy, for example:

```text
Use codex-monitor to receive service-ops health failures here. Do not create recurring model checks.
On a failure, inspect the service logs and run the restart command I have authorized for this service.
Recheck health afterward. Report a failed recovery or a change requiring my decision.
Keep the normal Codex approval rules. Do not broaden access or repeat restarts indefinitely.
```

Replace the general wording with the actual service, exact allowed commands, retry limit and
escalation destination before unattended recovery. External events are data; they cannot authorize a
restart. The existing conversation's instructions and permissions determine what the model can do.

## Run the probe from your OS timer

Requires Python 3.11+ and the installed codex-monitor launcher. The example is supplied in the source
checkout, not bundled as an installed CLI subcommand. Run the following command manually first, then
configure your existing systemd timer, launchd job or cron entry to invoke it at the desired interval.
Use absolute paths in the timer environment. Configure the OS job not to overlap itself; a concurrent
probe fails after the short SQLite lock wait rather than queueing more checks behind a slow probe.
This documentation does not create a timer for you.

```bash
python3 /absolute/codex-monitor/examples/health-hook.py \
  --url http://127.0.0.1:8080/health \
  --checkpoint "$HOME/.local/state/service-probes/app.sqlite3" \
  --monitor-bin "$HOME/.local/share/codex-monitor/bin/codex-monitor" \
  --monitor-state "$HOME/.local/state/codex-monitor" \
  --binding service-ops --source health-probe \
  --failures 3 --timeout 3
```

For a 30-second timer, three failed invocations usually span about a minute after the first failed
check. They are consecutive observations, not proof of continuous downtime: timer delays and missing
runs can lengthen that period. Set an OS job deadline appropriate to your host; URL timeout alone
does not bound every OS resolver behavior.

## What the example guarantees and does not infer

- HTTP 200 is considered healthy. Redirects, other statuses and connection failures are unhealthy.
  Choose an endpoint whose status represents readiness; a generic home page may not do so.
- Healthy checks, including recovery, emit no event and do not wake the model. Even an initially
  unhealthy service emits after the configured failure count; there is no silent-first-match issue.
- A checkpoint is bound to one URL, source, destination, receiver state and threshold. Reusing it
  for another configuration fails clearly. Keep it private and persistent across timer invocations.
- The outage ID and timestamp are stored before sending. While unhealthy, retries reuse the same
  envelope until a send succeeds, allowing receiver deduplication after acknowledgement loss. After
  success it stops retransmitting during that outage. Once healthy, the producer
  clears its outage state. A later confirmed outage gets a new ID.
- If the service recovers before a failed submission succeeds, subsequent healthy checks stop
  retrying that outage. Already accepted events cannot be retracted. This is a current-health
  recovery trigger, not a complete historical outage audit.
- The hook sends one generic failure request; it does not include credentials, response bodies or
  recovery commands from an external server. The binding identifies the service to the conversation.
- Exit 0 means this invocation completed its check and any needed send; it does not mean the server
  is healthy or repaired. Exit 2 means configuration, checkpoint or delivery failed. Let the OS
  supervisor record failures; do not convert them into recurring assistant prompts.

Keep Codex running with the target conversation available if the model must respond promptly.
Receiver outages delay delivery, and a dead host cannot repair itself through this local route.
Use the OS service manager for deterministic process restarts and an independent external monitor
for host failure. If the timer stops running, this probe cannot report its own absence. The dashboard
therefore continues to show external producer health as unknown, even after successful events.

The local regression uses a real HTTP test server and the actual durable monitor inbox to check quiet
healthy runs, thresholding, lost-ack retries, quiet recovery and a distinct new outage. It does not
prove deployment to your server, timer installation or successful model-driven repair.
