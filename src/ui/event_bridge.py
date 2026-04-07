"""Translate LangGraph astream events into SimEvents for the browser.

Simplified architecture: EventBridge sends only data events.
The frontend UIDirector handles all visual decisions (movement, lighting).

Unified team-based execution — no deep_research vs hierarchical distinction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.ui.characters import CharacterManager
from src.ui.floor_plan import FloorPlan
from src.utils.progress import NODE_LABELS


@dataclass
class SimEvent:
    """A simulation event to be sent to the browser via WebSocket."""

    type: str       # scene_change | char_spawn | progress | message |
                    # interrupt | hierarchy | complete | error
    timestamp: float
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "ts": self.timestamp, "data": self.data}


class EventBridge:
    """Stateful translator: graph events -> SimEvents.

    Only emits data events. The frontend UIDirector decides
    character movement and room lighting autonomously.
    """

    def __init__(self):
        self.characters = CharacterManager()
        self.floor = FloorPlan()
        self._current_node: str | None = None
        self._step = 0
        self._spawned: set[str] = set()

    def translate(self, node_name: str, update: dict) -> list[SimEvent]:
        """Translate one graph event into SimEvents (data only)."""
        events: list[SimEvent] = []
        now = time.time()

        # ── Node transition detection ──
        if node_name != self._current_node:
            self._step += 1
            self._current_node = node_name

            events.append(SimEvent(
                type="scene_change",
                timestamp=now,
                data={
                    "node": node_name,
                    "label": NODE_LABELS.get(node_name, node_name),
                    "step": self._step,
                },
            ))

        # ── workers change → hierarchy + char spawns ──
        if "workers" in update:
            workers_list = update["workers"]

            # Spawn workers that haven't been spawned yet
            for worker in workers_list:
                w_domain = worker.get("worker_domain", "unknown")
                if w_domain not in self._spawned:
                    char = self.characters.activate_worker(w_domain, w_domain)
                    events.append(SimEvent(type="char_spawn", timestamp=now, data={
                        "character": char,
                        "spawn_at": "corridor_entrance",
                    }))
                    self._spawned.add(w_domain)

            # Hierarchy event (flat workers, no leaders)
            hierarchy = self._build_hierarchy_from_workers(workers_list)
            events.append(SimEvent(
                type="hierarchy",
                timestamp=now,
                data=hierarchy,
            ))

            # Auto-advance to worker_execution when task decomposition emits workers
            if node_name == "ceo_task_decomposition" and self._current_node != "worker_execution":
                self._step += 1
                self._current_node = "worker_execution"
                events.append(SimEvent(
                    type="scene_change",
                    timestamp=now,
                    data={
                        "node": "worker_execution",
                        "label": NODE_LABELS.get("worker_execution", "Worker Execution"),
                        "step": self._step,
                    },
                ))

        # ── Messages (intake 에코 제외) ──
        if node_name != "intake":
            for msg in update.get("messages", []):
                content = msg.content if hasattr(msg, "content") else str(msg)
                speaker, style = self._classify_message(content, node_name)
                events.append(SimEvent(
                    type="message",
                    timestamp=now,
                    data={"speaker": speaker, "content": content, "style": style},
                ))

        # ── Error state ──
        if update.get("phase") == "error":
            events.append(SimEvent(
                type="error",
                timestamp=now,
                data={"message": update.get("error_message", "Unknown error")},
            ))

        return events

    def _build_hierarchy_from_workers(self, workers: list[dict]) -> dict:
        """Build hierarchy from flat workers list (2-tier architecture).

        Assigns meeting rooms to workers and includes room_assignments
        so the frontend UIDirector can place characters correctly.
        """
        _MEETING_ROOMS = ["mr_a", "mr_b", "mr_c", "mr_d", "mr_e"]

        domain_counts: dict[str, int] = {}
        for w in workers:
            d = w.get("worker_domain", "unknown")
            domain_counts[d] = domain_counts.get(d, 0) + 1
        domain_seen: dict[str, int] = {}

        worker_entries = []
        seen_domains: list[str] = []
        for w in workers:
            wd = w.get("worker_domain", "unknown")
            wid = w.get("worker_id", "")
            if not wid:
                idx = domain_seen.get(wd, 0)
                domain_seen[wd] = idx + 1
                wid = f"{wd}_{idx}" if domain_counts[wd] > 1 else wd
            worker_entries.append({
                "domain": wd,
                "worker_id": wid,
                "dependencies": w.get("dependencies", []),
                "role_type": w.get("role_type", "executor"),
                "worker_name": w.get("worker_name", ""),
                "task_title": w.get("task_title", ""),
            })
            if wd not in seen_domains:
                seen_domains.append(wd)

        room_assignments: dict[str, str] = {}
        for i, domain in enumerate(seen_domains):
            room_assignments[domain] = _MEETING_ROOMS[i % len(_MEETING_ROOMS)]

        hierarchy: dict = {"leaders": [], "room_assignments": room_assignments}
        domain_workers: dict[str, list] = {}
        for we in worker_entries:
            domain_workers.setdefault(we["domain"], []).append(we)
        for domain, dw in domain_workers.items():
            hierarchy["leaders"].append({"domain": domain, "workers": dw})

        return hierarchy

    def _classify_message(self, content: str, node_name: str) -> tuple[str, str]:
        """Classify a message into (speaker, style)."""
        if content.startswith("[CEO"):
            return ("ceo", "announcement")
        if content.startswith("[System]") or content.startswith("[system"):
            return ("system", "system")
        if content.startswith("[Generated Files]"):
            return ("system", "report")
        if content.startswith("[Report"):
            return ("system", "report")
        if content.startswith("[") and "]" in content:
            domain = content[1:content.index("]")]
            return (domain, "chat")
        return ("system", "system")
