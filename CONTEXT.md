# Domain vocabulary

| Term | Meaning | Do not confuse with |
|---|---|---|
| Conversation | Existing Codex context receiving human messages and external input | A parent monitor |
| Monitor | Named observation definition scoped to one conversation | A model or child agent |
| Source | Producer identity permitted to submit events and retrieve scoped replies | A conversation |
| Producer | Program that observes external state or receives events | A continuously running model |
| Binding | Named route from permitted sources to exactly one conversation | Automatic broadcast to every conversation |
| Receiver | Shared service that authenticates, stores and delivers events | A parent agent or team coordinator |
| Receipt | Event identity and evidence of delivery state | Proof that the requested work completed |
| Relay conversation | Optional conversation explicitly chosen to route work elsewhere | A required monitoring component |
| Live view | Dashboard refresh activity | Proof that a producer or model is running |

Several monitors and bindings can target the same conversation. Several conversations can use one
receiver. These are routing and ownership relationships, not agent parent/child relationships.
A source allowed on several bindings still submits to a selected binding; this does not create an
automatic fan-out graph. Native agent orchestration remains owned by Codex.
