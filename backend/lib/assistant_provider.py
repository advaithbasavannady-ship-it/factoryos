"""AI assistant provider seam.

The prototype ships with `NotConnectedProvider`, which never invents factory answers.
To connect GPT or Claude later: implement `AssistantProvider.reply()` (e.g. with
emergentintegrations), pass factory context built from the tenant's records, and return it
from `get_provider()` when AI_PROVIDER is set in backend/.env.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class ProviderReply:
    content: str
    status: str  # "ok" | "not_connected"
    link: Optional[str] = None
    link_label: Optional[str] = None


class AssistantProvider(Protocol):
    name: str
    connected: bool

    async def reply(self, company_id: str, history: list[dict], question: str) -> ProviderReply: ...


# keyword -> (route, label): navigation hints only, never data
MODULE_HINTS: list[tuple[tuple[str, ...], str, str]] = [
    (("order", "dispatch", "customer", "delivery"), "/orders", "Open Orders"),
    (("absent", "employee", "attendance", "late", "leave", "staff", "worker"), "/attendance", "Open Attendance"),
    (("machine", "idle", "maintenance", "downtime", "running"), "/machines", "Open Machines"),
    (("production", "output", "target", "reject", "efficiency"), "/production", "Open Production"),
    (("stock", "inventory", "material", "supplier", "purchase"), "/inventory", "Open Inventory"),
    (("schedule", "meeting", "calendar", "event", "today"), "/schedule", "Open Schedule"),
    (("report", "csv", "export"), "/reports", "Open Reports"),
]


class NotConnectedProvider:
    name = "none"
    connected = False

    async def reply(self, company_id: str, history: list[dict], question: str) -> ProviderReply:
        q = question.lower()
        link, label = None, None
        for words, route, text in MODULE_HINTS:
            if any(w in q for w in words):
                link, label = route, text
                break
        content = (
            "AI integration is not connected yet. Your question has been saved to this conversation, "
            "but I can't answer it, and I won't guess at your factory data. "
            "Once a language model (GPT or Claude) is connected, the assistant will answer from your "
            "FactoryOS records."
        )
        if link:
            content += " For now, the relevant module shows this information directly."
        return ProviderReply(content=content, status="not_connected", link=link, link_label=label)


def get_provider() -> AssistantProvider:
    # Future: if os.environ.get("AI_PROVIDER") in {"openai", "anthropic"}: return LLMProvider(...)
    _ = os.environ.get("AI_PROVIDER")
    return NotConnectedProvider()
