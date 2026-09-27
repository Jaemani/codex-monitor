"""Read saved resident permissions without resuming a conversation."""
from .owner_reconnect import OwnerReconnect, _option


def configured_permissions(root, connections):
    try:
        jobs = OwnerReconnect(root)._jobs()
    except Exception:
        jobs = []
    for conversation in connections:
        for binding in conversation.get("bindings", []):
            matches = [j for j in jobs if "resident" in j.args and
                       _option(j.args, "--endpoint") == binding.get("endpoint") and
                       any(v == "--thread" and j.args[i + 1] == conversation.get("thread")
                           for i, v in enumerate(j.args[:-1]))]
            modes = set()
            for job in matches:
                mode = _option(job.args, "--sandbox")
                label = {"danger-full-access": "Full Access", "read-only": "Read-only",
                         "workspace-write": "Workspace"}.get(mode, "Unknown")
                if mode == "workspace-write" and "--network-access" in job.args:
                    label = "Project Access"
                modes.add(label)
            label = next(iter(modes)) if len(modes) == 1 else "Mixed" if modes else "Unknown"
            binding["permission"] = {"label": label, "source": "saved-service-config", "effective_verified": False}
        bindings = conversation.get("bindings", [])
        active = [binding for binding in bindings if binding.get("enabled")]
        labels = {binding["permission"]["label"] for binding in (active or bindings)}
        conversation["permission_label"] = next(iter(labels)) if len(labels) == 1 else "Mixed" if labels else "Unknown"
