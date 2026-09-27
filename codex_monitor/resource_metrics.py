"""Bounded, cached observations of the monitor's own resource footprint."""

import os
from pathlib import Path
import subprocess
import time


def process_tree(output, receiver_pid):
    rows = {}
    for line in output.splitlines():
        try:
            pid, parent, state, rss, cpu = line.split()
            rows[int(pid)] = (int(parent), state, int(rss) * 1024, float(cpu))
        except ValueError:
            continue
    if receiver_pid not in rows:
        raise ValueError("receiver absent from process snapshot")
    owned = {receiver_pid}
    children = {}
    for pid, row in rows.items():
        children.setdefault(row[0], []).append(pid)
    todo = [receiver_pid]
    while todo:
        for pid in children.get(todo.pop(), []):
            if pid not in owned:
                owned.add(pid)
                todo.append(pid)
    return {
        "processes": len(owned), "children": len(owned) - 1,
        "zombies": sum("Z" in rows[pid][1] for pid in owned),
        "rss_bytes": sum(rows[pid][2] for pid in owned),
        "cpu_percent": round(sum(rows[pid][3] for pid in owned), 1),
        "scope": "receiver and currently attached descendants; excludes detached owners and dashboards",
    }


def project_resources(output, runtime, connections):
    """Attribute only explicitly registered sampler PIDs, never shared owners."""
    projects = {}
    threads = {}
    for connection in connections:
        project = connection.get("project") or "Ungrouped"
        threads[connection.get("thread")] = project
        projects.setdefault(project, {"available": False, "scope": "monitor samplers only; agent resources and shared storage unattributed"})
    sampler = runtime.get("sampler") or {}
    if not sampler.get("available") or "owned_workers" not in sampler:
        return projects
    rows = {}
    for line in output.splitlines():
        try:
            pid, parent, state, rss, cpu = line.split()
            rows[int(pid)] = (int(parent), state, int(rss) * 1024, float(cpu))
        except ValueError:
            continue
    for value in projects.values():
        value.update(available=True, workers=0, rss_bytes=0, cpu_percent=0.0, zombies=0)
    for worker in sampler['owned_workers']:
        project = threads.get(worker.get('thread'))
        if project not in projects:
            continue
        value = projects[project]
        value['workers'] += 1
        row = rows.get(worker.get('pid'))
        if row is None or row[0] != runtime.get('pid'):
            value['available'] = False
            continue
        value['rss_bytes'] += row[2]
        value['cpu_percent'] = round(value['cpu_percent'] + row[3], 1)
        value['zombies'] += 'Z' in row[1]
    return projects


def storage(root, *, budget=0.1, limit=10000):
    """Count logical bytes without opening contents or following symlinks."""
    deadline = time.monotonic() + budget
    todo, count = [Path(root)], 0
    sizes = {"database_bytes": 0, "log_bytes": 0, "other_bytes": 0}
    try:
        while todo:
            with os.scandir(todo.pop()) as entries:
                for entry in entries:
                    count += 1
                    if count > limit or time.monotonic() > deadline:
                        return {"available": False, "reason": "storage scan budget exceeded"}
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        todo.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        name = entry.name.lower()
                        key = ("database_bytes" if any(token in name for token in (".db", ".sqlite"))
                               else "log_bytes" if ".log" in name else "other_bytes")
                        sizes[key] += entry.stat(follow_symlinks=False).st_size
        return {"available": True, **sizes, "total_bytes": sum(sizes.values()),
                "scope": "state directory logical bytes; excludes runtime and watched files"}
    except OSError:
        return {"available": False, "reason": "storage scan unavailable"}


class ResourceSampler:
    def __init__(self, root, *, clock=time.monotonic, runner=subprocess.run):
        self.root, self.clock, self.runner = root, clock, runner
        self.next_read = 0
        self.cached = None
        self.previous_storage = None
        self.zombie_samples = 0
        self.previous_pid = None

    def sample(self, runtime, connections=()):
        now = self.clock()
        if self.cached is not None and now < self.next_read:
            return dict(self.cached, age_seconds=max(0, now - self.cached["sampled_monotonic"]))
        self.next_read = now + 30
        pid = runtime.get("pid") if isinstance(runtime, dict) else None
        process = {"available": False, "reason": "receiver identity unavailable"}
        output = ""
        if type(pid) is int and pid > 0:
            try:
                result = self.runner(["/bin/ps", "-axo", "pid=,ppid=,stat=,rss=,%cpu="],
                                     capture_output=True, text=True, timeout=1, check=True,
                                     env={**os.environ, "LC_ALL": "C"})
                output = result.stdout
                process = {"available": True, **process_tree(output, pid)}
            except (OSError, subprocess.SubprocessError, ValueError):
                process = {"available": False, "reason": "process sample unavailable"}
        if pid != self.previous_pid:
            self.zombie_samples = 0
        self.previous_pid = pid
        self.zombie_samples = self.zombie_samples + 1 if process.get("available") and process.get("zombies", 0) else 0
        process["consecutive_zombie_samples"] = self.zombie_samples
        disk = storage(self.root)
        if disk.get("available"):
            if self.previous_storage:
                previous_time, previous_size = self.previous_storage
                elapsed = now - previous_time
                if elapsed > 0:
                    disk["change_bytes_per_hour"] = (disk["total_bytes"] - previous_size) * 3600 / elapsed
                    disk["change_window_seconds"] = elapsed
            self.previous_storage = now, disk["total_bytes"]
        self.cached = {"projects": project_resources(output, runtime or {}, connections) if process.get("available") else {}, "process": process, "storage": disk, "sampled_monotonic": now,
                       "sampled_at": time.time(), "age_seconds": 0, "interval_seconds": 30}
        return dict(self.cached)
