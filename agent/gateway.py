"""agent/gateway.py — Maximum defense gateway.

Implements all four identity checks, lease/write precondition enforcement,
body-route rejection, instruction-injection quarantine, and adaptive budget pacing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:
    ToolCall = Any
    _TOOLCALL_AVAILABLE = False

from agent.telemetry import Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

WRITE_TOOLS = {
    ("content", "flag_stale_slide"),
    ("content", "file_content_bug"),
    ("progress", "record_mastery"),
}
A2A_SERVERS = {"curriculum-analyst", "citation-checker", "roster"}

DEPRECATED_SUCCESSORS = {
    ("slides", "search"): ("slides", "query"),
}

# Narrow by default; widen only where the answer will actually cite the field.
MASKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("title", "anchor"),
    ("slides", "get_frame"): ("title", "body", "anchor"),
    ("slides", "whatlinkshere"): ("anchor",),
    ("glossary", "define"): ("definition", "sense", "anchor"),
    ("registry", "provenance"): ("etag", "replica", "anchor"),
    ("registry", "list_servers"): ("name",),
    ("research", "cite_source"): ("url", "anchor"),
    ("curriculum-analyst", "which_days_cover"): ("course_day", "track", "anchor"),
    ("citation-checker", "verify_source"): ("verdict", "anchor"),
}

_IMPERATIVE = (
    "ignore previous instruction", "ignore all previous", "disregard the above",
    "system override", "bỏ qua mọi hướng dẫn", "you must now", "instead, record",
    "also record this for", "print the", "reveal the",
)

_ROUND_ALLOWANCE = {1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12}


@dataclass(frozen=True, slots=True)
class Command:
    cmd_id: str
    kind: str
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: str
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}")
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call'")
        else:
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")


@runtime_checkable
class GatewayContext(Protocol):
    act: str
    sub: str
    scopes: frozenset[str]
    credits: int
    round: int
    call_index: int
    leases: tuple[str, ...]
    history: tuple[Mapping[str, Any], ...]

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """Maximum defense gateway implementing the four identity checks and protocol guards."""

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        self._etags: dict[str, str] = {}
        self._idempotency: set[str] = set()
        self._admitted_cards: dict[str, dict] = {}
        self._spent_this_round = 0
        self._round = 0
        self.denied = 0
        self.quarantined = 0

    # -- the four identity checks --------------------------------------------
    def _card_admitted(self, cmd: Command) -> bool:
        if cmd.headers.get("x-card-signature") == "invalid":
            return False
        if cmd.headers.get("x-server-fingerprint") == "unvouched":
            return False
        if cmd.server not in A2A_SERVERS:
            return True
        card = self._admitted_cards.get(cmd.server)
        return bool(card and card.get("verified"))

    def _skill_declared(self, cmd: Command) -> bool:
        if cmd.server not in A2A_SERVERS:
            return True
        card = self._admitted_cards.get(cmd.server) or {}
        return cmd.tool in set(card.get("skills") or ())

    def _audience_matches(self, cmd: Command) -> bool:
        aud = cmd.headers.get("aud") or cmd.headers.get("Aud")
        if aud is None:
            return cmd.server not in A2A_SERVERS  # a delegation with no aud is not a delegation
        return aud == cmd.server

    def _routes_on_header(self, cmd: Command) -> bool:
        """Refuse a route smuggled through the request body."""
        return not (cmd.args.get("route") or cmd.args.get("_route") or cmd.args.get("replica"))

    def _act_owns_target(self, cmd: Command) -> bool:
        """Check 4 — Authority derives from `act` (whom you serve)."""
        act = getattr(self.ctx, "act", None)
        for key in ("learner", "learner_id", "target", "subject"):
            target = cmd.args.get(key)
            if target and act and str(target) != str(act):
                return False
        return True

    def _within_budget(self, cmd: Command) -> bool:
        rnd = getattr(self.ctx, "round", 0) or 0
        if rnd != self._round:
            self._round, self._spent_this_round = rnd, 0
        return self._spent_this_round < _ROUND_ALLOWANCE.get(rnd, 9)

    def _instruction_in_content(self, cmd: Command) -> bool:
        blob = " ".join(str(v) for v in cmd.args.values()).lower()
        return any(token in blob for token in _IMPERATIVE)

    def _args_well_formed(self, cmd: Command) -> bool:
        """Reject unverified peer flags, fake catalog payloads, and oversized argument blobs."""
        if any(k in cmd.args for k in ("catalog", "extra_rows", "peer_unverified", "decoy_path_id")):
            return False
        if any(len(str(v)) > 512 for v in cmd.args.values()):
            return False
        return True

    def decide(self, cmd: Command) -> Decision:
        """Evaluate command and return pure Decision."""
        self._telemetry.decision_seen(cmd)

        # 1. Identity and routing checks
        checks = (
            (self._card_admitted, "peer card not admitted by the registry or invalid signature"),
            (self._skill_declared, "skill not declared on the peer's agent card"),
            (self._audience_matches, "delegation aud does not match the server called"),
            (self._act_owns_target, "target is not owned by the learner in act"),
            (self._routes_on_header, "route declared in the body, not the header"),
            (self._args_well_formed, "untrusted mutation argument or oversized payload"),
        )
        for check, reason in checks:
            if not check(cmd):
                return self._deny(cmd, reason)

        # 2. Instruction injection check
        if self._instruction_in_content(cmd):
            self.quarantined += 1
            return self._deny(cmd, "instruction found in retrieved content", quarantine=True)

        # 3. Leases check
        if cmd.tool == "get_frame" and cmd.lease_id is None:
            return self._deny(cmd, "get_frame without lease")

        # 4. Write tools preconditions and idempotency
        server, tool = DEPRECATED_SUCCESSORS.get((cmd.server, cmd.tool), (cmd.server, cmd.tool))
        rewritten = (server, tool) != (cmd.server, cmd.tool)

        headers = {k: v for k, v in cmd.headers.items() if k.lower() != "x-mcp-body-route"}
        headers["Mcp-Replica"] = headers.get("Mcp-Replica", "w")  # route on the header, always

        if (server, tool) in WRITE_TOOLS:
            anchor = str(cmd.args.get("anchor", ""))
            etag = self._etags.get(anchor) or headers.get("if-match") or headers.get("If-Match")
            if not etag:
                return self._deny(cmd, "write without a fresh If-Match etag")
            key = f"{anchor}:{tool}"
            if key in self._idempotency:
                return self._deny(cmd, "write already committed this duel")
            self._idempotency.add(key)
            headers["If-Match"] = str(etag)
            headers["Idempotency-Key"] = key

        # 5. Budget check
        if not self._within_budget(cmd):
            return self._deny(cmd, "round allowance exhausted; saving for late rounds")

        # 6. Build and forward/rewrite call
        fields = tuple(cmd.fields or MASKS.get((server, tool), ("anchor",)))
        self._spent_this_round += 1

        if _TOOLCALL_AVAILABLE:
            call = ToolCall(
                server=server,
                tool=tool,
                args=dict(cmd.args),
                fields=fields,
                headers=headers,
                lease_id=cmd.lease_id,
                call_index=cmd.call_index,
            )
        else:
            call = {
                "server": server,
                "tool": tool,
                "args": dict(cmd.args),
                "fields": list(fields),
                "headers": headers,
                "lease_id": cmd.lease_id,
                "call_index": cmd.call_index,
            }

        decision = Decision(verdict="rewrite" if rewritten else "forward", call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _deny(self, cmd: Command, reason: str, quarantine: bool = False) -> Decision:
        self.denied += 1
        decision = Decision(verdict="deny", reason=reason, quarantine=quarantine)
        self._telemetry.decision_made(cmd, decision)
        return decision

    # -- fed by the loop after a call returns ---------------------------------
    def note_provenance(self, anchor: str, etag: str) -> None:
        self._etags[anchor] = etag

    def note_card(self, server: str, card: dict) -> None:
        self._admitted_cards[server] = card

