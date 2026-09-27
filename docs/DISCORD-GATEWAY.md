# Native Discord Gateway

`codex-monitor discord-gateway` runs a Rust Discord producer independently of the
receiver, Codex owner and resident. It replaces the deployed Python Gateway loops;
it does not rewrite or own another project's reply CLI, model tools or web servers.
The core receiver remains usable without Discord. Existing one-shot reply adapters
can continue using their existing `message_routes` and outbox databases.

## Configuration and preflight

Use the adapter's existing JSON configuration with `project_name`, `guild_id`,
`channel_id`, `application_id`, private `token_file`, absolute `state_file`,
`allowed_user_ids`, `roles`, `discord_threads` and optional `monitor_bindings`.
Roles map to exact Codex conversation IDs; Discord thread IDs map to roles.
Bindings must stay within the selected source/role namespace. Preserve
`threads_only`, `allow_all_thread_users` and `receipt_emoji` as configured.

```bash
codex-monitor --state /absolute/monitor-state discord-gateway \
  --config /absolute/adapter/config.json --source discord-project --check
codex-monitor --state /absolute/monitor-state discord-gateway \
  --config /absolute/adapter/config.json --source discord-project --verify
```

`--check` validates local configuration and credential-file permissions without
network I/O or state changes. `--verify` performs read-only Discord REST checks for
bot identity, guild, parent channel and every registered thread. Neither starts a
Gateway session, posts a message or invokes a model. The monitor source credential
is read from `source-SOURCE.token`; the receiver port comes from monitor config.
Credentials never appear in command arguments or routine error logs.

The optional legacy `request_lifecycle` observer is not implemented by this
adapter. Configurations enabling it are rejected before starting; retain the old
adapter for those deployments. Neither migrated deployment enabled this feature.
Do not equate Gateway readiness with work completion or a delivered Discord reply.

## Controlled adoption

1. Build and validate the native release while the existing Gateway remains live.
2. Back up the adapter config, LaunchAgent and SQLite database with SQLite's backup
   API (including committed WAL content). Keep these private and outside Git.
3. Record a Discord snowflake at or just before the cutover. Stop only the verified
   producer service. Leave receiver, Codex owner, residents and active work running.
4. Replace that producer's command with the installed native executable:

```bash
codex-monitor --state /absolute/monitor-state discord-gateway \
  --config /absolute/adapter/config.json --source discord-project \
  --since-id CUTOVER_SNOWFLAKE
```

5. Verify the new service PID, executable and fresh `gateway-health.json` with
   `runtime: rust` and `gateway_ready: true`. Cut over a second Gateway only after
   the first is ready; honor Discord's shared bot session-start limits.

The first start requires `--since-id`; it is saved and later ignored in favor of
persisted progress. The seed is a lower bound for message-history recovery, not a
request to replay old work. The adapter adds private `gateway_native_*` tables and
preserves existing `gateway_pending`, routes, receipts and outbox records. Raw
messages commit before the Gateway sequence checkpoint. Message IDs deduplicate
both incoming Gateway events and history recovery. Recovery runs after connection
so messages arriving during REST scans are also captured by the live stream.
At most 100 pages per registered channel are recovered; exceeding this bound
prevents a ready claim and requires operator review.

Each adapter database admits only one native Gateway process. A legacy producer
does not participate in this lock; the cutover must stop it before starting the
native producer. If startup fails, unload only the new producer and restore the
previous service definition. Preserve the updated database and inspect new raw
records before rollback: do not blindly restore an old database or discard work.

## Runtime behavior and limits

- Guild, channel, role, human-author and allowlist checks precede intake. Bot,
  webhook, wrong-role and unregistered-channel messages cannot wake Codex.
- Up to four PNG/JPEG attachments, each at most 8 MiB, are downloaded from approved
  Discord CDN hosts without following redirects. Paths are generated locally;
  credentials are not forwarded to attachments. Files expire after 24 hours.
- Receiver retries retain the original message ID and payload. Five attempts is
  the default; `delivery_max_attempts: 0` preserves an adapter's unlimited retry
  policy. Exhausted records remain stored for review, never silently deleted.
- Receipt reactions use native consumption evidence. Unknown consumption does not
  imply model completion. Reaction failures back off and stop after five attempts.
  The former substring scan of rollout files is not used as consumption evidence.
- Gateway heartbeats, ACK checks, resume checkpoints, invalid-session recovery and
  fatal close-code handling use [Discord's Gateway protocol](https://docs.discord.com/developers/events/gateway).
- Health is a current connection observation. A ready Gateway can still have held
  deliveries or a failed downstream conversation. Inspect receipts separately.

The service does not send a synthetic Discord test message, create channels,
change account/provider configuration or grant Full Access during adoption.
