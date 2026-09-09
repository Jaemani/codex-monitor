# Monitoring feedback review

Reviewed 2026-09-10 against this repository and first-party Claude Monitor, Channels and OpenTelemetry
documentation. This is an assessment of claims, not an independent performance benchmark.

| Feedback | Decision | Result / boundary |
|---|---|---|
| Qualify the official queue API as experimental in the introduction | Accept | README introduction and execution diagram now state it. Compatibility remains a version-specific contract risk (#7). |
| Separate external conditions, event delivery and execution telemetry | Accept | README adds an explicit scope table; dashboard terminology is aligned with observed delivery and reported work. No telemetry is invented. |
| Distinguish retained input from unloaded Desktop execution | Accept, already implemented in behavior/docs | Opening the app is not a residency lease. #4 tracks the blocked owner integration; #5 tracks the CLI alternative. |
| Highlight durable inbox, duplicate suppression and reconciliation | Accept with limits | These are implemented primitives, not exactly-once business execution or universal loss prevention. See session.py, monitor.py and delivery tests. |
| Distinguish native consumption from actual work completion | Accept | Request transitions remain explicit. A completed report is not independent verification of its truth. |
| Add arbitrary stdout, WebSocket sources and plugin startup to comparison | Accept | Claude Monitor supports these directly. This project still needs custom producers; its managed file sampler is not a generic command supervisor. |
| Model/tool/API/token/cost observability is missing from this project | Accept | It is explicitly outside the current dashboard scope. Native Codex capabilities are not removed or judged by this absence. |
| Assign a 65–80% or 40–50% parity score | Do not adopt | No agreed weighting, test set or matched benchmark establishes these percentages. |
| Claim stronger reliability than Claude overall | Do not adopt | A bare Channels transport lacks processing acknowledgement; adapters can add storage and explicit confirmation. A matched benchmark is #13. |
| Treat closed/unloaded execution as universally absent | Qualify | Desktop cold wakeup remains unavailable, but a running CLI owner/resident can process events after its TUI closes. The host and owner processes must remain available. |
| Generalize into a multi-vendor infrastructure layer now | Defer | Current task remains Codex monitoring; broader backends would require separate design and validation. |

## Priorities retained

1. Compatibility and truthful readiness (#7), plus reliable CLI owner lifecycle (#5/#6).
2. Fresh-client setup and concrete user controls (#3); Desktop owner attachment remains separately blocked (#4).
3. Broader producer integration and observed health (#2). A generic command producer would require explicit
   argv/environment, output bounds/filtering, process cleanup, durable handoff and restart tests before
   it can be described as managed support.
4. Native telemetry remains a separate possible integration, not an inferred dashboard state.

## Source verification

- [Monitor tool](https://code.claude.com/docs/en/tools-reference#monitor-tool): background command output
  and WebSocket messages, session/subagent lifetime, plugin startup and provider/settings restrictions.
- [Channels reference](https://code.claude.com/docs/en/channels-reference): notification transport writes
  are not processing acknowledgements; absent/blocked channels may silently drop events; servers can
  implement explicit delivery confirmation. Busy notifications queue for a later turn.
- [OpenTelemetry](https://code.claude.com/docs/en/monitoring-usage): configurable metrics, tool/API events,
  token/cost export and optional traces. Export needs configuration and is not the Monitor tool.
- [Compatibility](COMPATIBILITY.md), [test scope](TESTING.md), [source assessment](PRODUCT-COMPARISON.md).

The Claude pages were read in the browser after direct Markdown retrieval returned HTTP 403. No Claude
session was started and no matched runtime benchmark is claimed. Postscript questions in the supplied
feedback were excluded from this review.
