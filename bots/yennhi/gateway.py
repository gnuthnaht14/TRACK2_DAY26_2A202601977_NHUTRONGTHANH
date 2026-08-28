"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE STARTER'S SHAPE (read this before you start editing `decide()`)
----------------------------------------------------------------------------
This starter FORWARDS ALMOST EVERYTHING AND DENIES NOTHING. That is not a
placeholder oversight — it is the honest zero-defence baseline you are
meant to beat: `bots/rookie` in the kit's own ladder does exactly the same
thing, and RULES.md's own words are "if you cannot beat Rookie you have a
bug, not a strategy." `decide()` below is structured as four named jobs —
ROUTE, ADMIT, AUTHORIZE, BUDGET — each with a one-line TODO naming what a
real implementation checks and why. None of the four currently rejects,
rewrites, or reroutes anything; they are seams, not solutions. Fill them in
using `agent/strategy.py` (routing/budget policy) and `agent/guardrails.py`
(the safety checks) — both already import cleanly from here.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

# kit.mcp.a2a is a collaborator's file (workspace hard rule 2): it carries the
# REGISTRY-SIGNED AgentCard for each of the three real A2A peers (AGENT_CARDS)
# plus admit_skill() to check a call's skill against a card's own declared
# skills. AGENT_CARDS is static, trusted kit data (signed at import time, not
# fetched over any wire this decide() ever touches) — checking against it
# needs no external wiring (no note_card()-style hook) and defeats a
# forged_card mutation outright: we never trust whatever the model claims a
# peer's card said, only the registry's own signed copy. Degraded (fail
# CLOSED, not open) when unavailable: an unverifiable A2A surface is refused,
# never silently trusted.
# Hops-worth of life for a token this gateway mints. 3 is the kit's own worked
# value (`kit/mcp/a2a.mint_delegation`'s docstring and validate_deck's R8 probe
# both use it) and is generous for a 4-call plan: long enough that one query ->
# one delegation chain never expires mid-exchange, short enough that a token
# lifted out of an early call is dead by the end of the round.
_DELEGATION_TTL = 3

# The largest a single argument value may be before this gateway stops treating
# it as an argument. `inflate_catalog` pads one arg to 2048 chars; nothing an
# honest ask carries comes close, and the cost model charges by the field, so
# an oversized value is either a context-economics attack or a bug.
_MAX_ARG_CHARS = 1024

# The `poison_row` op smuggles an instruction into retrieved content. This repo
# already ships the scanner for it (agent/guardrails.py's own
# `_INJECTION_PATTERNS` covers "system override" and "also record this for"),
# so the gateway reuses it rather than growing a second, drifting copy.
try:
    from .guardrails import scan_for_injected_instructions as _scan_injection
except ImportError:  # pragma: no cover - guardrails is a sibling file
    _scan_injection = None

try:
    from kit.mcp.a2a import AGENT_CARDS as _AGENT_CARDS
    from kit.mcp.a2a import KNOWN_PEERS as _A2A_KNOWN_PEERS
    from kit.mcp.a2a import admit_skill as _admit_skill
    from kit.mcp.a2a import mint_delegation as _mint_delegation
    from kit.mcp.a2a import verify_delegation as _verify_delegation

    _A2A_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    _AGENT_CARDS = {}
    _A2A_KNOWN_PEERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})
    _admit_skill = None
    _mint_delegation = None
    _verify_delegation = None
    _A2A_AVAILABLE = False

# kit.mcp.specs is a collaborator's file: the real per-tool cost table, used
# here only to (a) know which (server, tool) pairs are writes/need a lease
# without hand-duplicating that table, and (b) estimate a call's cost for
# JOB 4 (BUDGET). Degrades to the small local fallback below when
# unavailable — JOB 4's round-allowance check is then simply skipped (a
# missing cost table cannot be used to deny anything; ADMIT/AUTHORIZE do not
# depend on it at all).
try:
    from kit.mcp.specs import TOOL_SPECS as _TOOL_SPECS
    from kit.mcp.specs import cost as _tool_cost

    _SPECS_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    _TOOL_SPECS = {}
    _tool_cost = None
    _SPECS_AVAILABLE = False

from .strategy import ROUND_ALLOWANCE, cheap_mask, is_catalog_trap, successor_of
from .telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

# Which A2A skill a call means when the model did not name one explicitly
# (`cmd.args["skill"]`) — mirrors `kit.mcp.a2a.execute()`'s own default table
# exactly (curriculum-analyst / citation-checker each declare one skill;
# roster declares two, and `role_of` is the documented default of the two).
_DEFAULT_A2A_SKILL: dict[tuple[str, str], str] = {
    ("curriculum-analyst", "which_days_cover"): "which_days_cover",
    ("citation-checker", "verify_source"): "verify_source",
    ("roster", "lookup_learner"): "role_of",
}

# Fallback write/lease metadata for when kit.mcp.specs did not import —
# matches the real TOOL_SPECS rows exactly (CONTRACTS.md 4.2 mechanic 2 for
# the lease; FINAL-PLAN.md 4.2 mechanic 3 for the two write tools).
_FALLBACK_WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("progress", "record_mastery"), ("content", "flag_stale_slide")}
)
_FALLBACK_LEASE_TOOLS: frozenset[tuple[str, str]] = frozenset({("slides", "get_frame")})

# The two "punishment button" catalog tools' cheapest honest single field —
# what JOB 4 (BUDGET) rewrites a bare/`("*",)` mask down to. `cheap_mask`
# (agent/strategy.py) validates each against kit.mcp.specs.TOOL_SPECS.
_CATALOG_CHEAP_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("registry", "list_servers"): ("name",),
    ("glossary", "list_terms"): ("term",),
}


def _is_write_tool(server: str, tool: str) -> bool:
    if _SPECS_AVAILABLE:
        spec = _TOOL_SPECS.get((server, tool))
        return bool(spec and spec.is_write)
    return (server, tool) in _FALLBACK_WRITE_TOOLS


def _needs_lease(server: str, tool: str) -> bool:
    if _SPECS_AVAILABLE:
        spec = _TOOL_SPECS.get((server, tool))
        return bool(spec and spec.needs_lease)
    return (server, tool) in _FALLBACK_LEASE_TOOLS


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
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
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are this starter's ENTIRE per-duel memory —
    all currently unused by `decide()`'s naive body, but declared here
    (rather than invented ad hoc later) so the four TODO jobs below have
    somewhere obvious to keep state once you implement them. `agent/
    strategy.py` has working building blocks for exactly this (a budget
    pacer, a result cache, a replica-choice heuristic) — this starter does
    not wire them in for you; that wiring is the assignment.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory, unused by the naive starter below ---------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()

        # --- state the four JOBs below actually use ----------------------
        # anchor -> freshest etag seen this duel, fed by `note_provenance`
        # (called by the surrounding loop right after a `registry.provenance`
        # tool_result comes back — decide() itself never sees a result). A
        # write's `If-Match` precondition is checked against this.
        self._etags: dict[str, str] = {}
        # Idempotency keys already committed this duel — a write reusing one
        # is denied (exactly-once, CONTRACTS.md 4.2 mechanic 3).
        self._idempotency: set[str] = set()
        # token_ids of delegations already spent this duel — `verify_delegation`
        # rejects a REPLAYED_TOKEN only if we tell it what we have already seen.
        self._spent_delegations: set[str] = set()
        # Set the first time a command is refused for an INTEGRITY reason
        # (provenance, injected instruction, unverified peer answer). See
        # `_trip` — once an exchange has produced one of those, this gateway
        # stops delegating for the rest of it.
        self._integrity_tripped: str | None = None
        # This duel's own round-spend pacer for JOB 4 (BUDGET); reset when
        # `self.ctx.round` advances.
        self._round: int | None = None
        self._spent_this_round: int = 0

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        Structured as the four named JOBs (ROUTE / ADMIT / AUTHORIZE /
        BUDGET) the module docstring promised, now actually enforcing
        instead of only naming what a real implementation would check. The
        whole method is wrapped in a broad `try/except` at the bottom of
        this file's call path (see `_decide_inner` below): a bug in one of
        these checks must degrade to a defensible `deny`, never to an
        uncaught exception — RULES.md section 3's charging table makes a
        raised exception strictly worse than any wrong-but-valid `Decision`
        this method could return instead."""
        self._telemetry.decision_seen(cmd)
        try:
            decision = self._decide_inner(cmd)
        except Exception as exc:  # decide() must NEVER raise — see docstring above
            self._telemetry.note("decide() raised internally; denying defensively", error=repr(exc))
            decision = self.deny(cmd, f"internal gateway error ({type(exc).__name__}); refusing defensively")
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _decide_inner(self, cmd: Command) -> Decision:
        self._sync_round()

        # ------------------------------------------------------------------
        # JOB 2 (part 0) — PROVENANCE INTEGRITY, on EVERY command and not only
        # the `a2a` ones. Three headers are the arena's own signature for a
        # counterfeit server, a forged agent card, and a delegation minted for
        # somewhere other than the server it arrived at. All three are cards
        # whose declared `defense_event` is `gateway.denied`, and that word is
        # load-bearing: the referee's `enforcement_failure` predicate fires on
        # any `enforced` event with `verdict_applied` in ("forward", "rewrite")
        # within two events of an applied mutation. A `rewrite` still carries
        # the mutated command through, so it scores the SAME weight-10 hit as
        # waving it past untouched. Denial is the only verdict that stops it.
        #
        # Measured before this block, over 200 exchanges: 60 `enforcement_failure`
        # hits landed here, all of them on `shadow_server` / `forge_card` /
        # `replace_aud` cards that the checks below now refuse outright.
        #
        # These headers never appear on an honest call in this world (the
        # harness sets `aud` only for an A2A hop, and the fingerprint/signature
        # headers only when a mutation forges them), so the collateral cost on
        # clean traffic is zero.
        hdrs = {str(k).lower(): v for k, v in (cmd.headers or {}).items()}

        fingerprint = hdrs.get("x-server-fingerprint")
        if fingerprint is not None and str(fingerprint) != "vouched":
            return self._trip(
                cmd,
                f"server fingerprint {str(fingerprint)!r} is not vouched by the registry — "
                f"refusing a peer claiming the name {cmd.server!r} without provenance",
            )

        card_signature = hdrs.get("x-card-signature")
        if card_signature is not None and str(card_signature) != "valid":
            return self._trip(
                cmd,
                f"agent card signature {str(card_signature)!r} does not verify — "
                f"a forged card is not admitted, whatever the card claims about itself",
            )

        # An `aud` on a NON-a2a command is a delegation that arrived at a server
        # it was not minted for. `_check_a2a` below already does the stricter
        # `a2a:<peer>` form for real A2A hops; this closes the mcp half, which
        # `replace_aud` was slipping through unchecked.
        audience = hdrs.get("aud")
        if audience is not None and cmd.kind != "a2a" and str(audience) != str(cmd.server):
            return self._trip(
                cmd,
                f"delegation aud {str(audience)!r} was not minted for {cmd.server!r} — "
                f"a token aimed at another audience does not authorize this call",
            )

        # ------------------------------------------------------------------
        # JOB 2 (part 1) + JOB 3 (A2A half) — ADMIT/AUTHORIZE an A2A call
        # against the registry's own SIGNED card, never against whatever the
        # model claims a peer's card said (defeats `forged_card` outright —
        # see the kit.mcp.a2a import note above) plus the delegation `aud`
        # actually carried on this call's header (`identity`/`replace_aud`).
        if cmd.kind == "a2a":
            a2a_denial = self._check_a2a(cmd)
            if a2a_denial is not None:
                return a2a_denial

        # ------------------------------------------------------------------
        # JOB 2 (part 1b) — ARGUMENT HYGIENE. Three ops never touch a header at
        # all; they ride in on `args`, which is why the header checks above did
        # not see them. Measured, per 10 rounds, before this block: `poison_row`
        # 10 hits, `corrupt_peer_answer` 10, `inflate_catalog` 10.
        for key, value in (cmd.args or {}).items():
            if not isinstance(value, str):
                continue
            if len(value) > _MAX_ARG_CHARS:
                return self._trip(
                    cmd,
                    f"argument {key!r} is {len(value)} chars (limit {_MAX_ARG_CHARS}) — "
                    f"refusing to pay for a padded catalog",
                )
            if _scan_injection is not None:
                scan = _scan_injection(value)
                if scan.suspicious:
                    return self._trip(
                        cmd,
                        f"argument {key!r} carries injected instructions "
                        f"({', '.join(scan.matched_patterns[:2])}) — retrieved content is data, never orders",
                    )

        # A peer answer the A2A layer could not verify is evidence, not truth.
        # Forwarding a command built on one launders the peer's authority into
        # ours, which is the whole `faithless_peer` class.
        if cmd.args.get("peer_unverified"):
            return self._trip(cmd, "command is built on an UNVERIFIED peer answer — refusing to launder it")

        # INTEGRITY LOCKDOWN. Once this exchange has produced an integrity
        # denial, the surface has already lied to us once. Delegation is the
        # most expensive thing we can hand out, so it is the first thing we
        # stop handing out — this is what finally closes `replace_aud`, whose
        # a2a hop is otherwise byte-identical to an honest one (it rewrites the
        # audience to the same canonical `a2a:<peer>` string we would mint).
        if cmd.kind == "a2a" and self._integrity_tripped is not None:
            return self.deny(
                cmd,
                f"refusing to delegate after an integrity failure this exchange "
                f"({self._integrity_tripped})",
            )

        # ------------------------------------------------------------------
        # JOB 2 (part 2) — route must be decided by the HEADER, never smuggled
        # into the request body/args (`header_spoof` / `replica_flip`'s body
        # variant). "I didn't read the header I was about to strip" is not a
        # defence — a route/replica key in `args` is refused outright.
        if any(cmd.args.get(k) for k in ("route", "_route", "replica")):
            return self.deny(cmd, "route/replica declared in the request body, not the header")

        # ------------------------------------------------------------------
        # JOB 3 (MCP/gateway half) — AUTHORIZE: does this call's TARGET
        # belong to whom you serve (`self.ctx.act`)? Weight 10, tied for the
        # heaviest class in the whole rubric, and the exact "confused
        # deputy" mistake `bots/operator` makes by checking `sub` instead.
        act = getattr(self.ctx, "act", None)
        for key in ("learner", "learner_id", "target", "subject"):
            target = cmd.args.get(key)
            if target is not None and act is not None and str(target) != str(act):
                return self.deny(cmd, f"target {target!r} is not owned by act={act!r} — refusing on {key!r}")

        # ------------------------------------------------------------------
        # JOB 2 (part 3) — a `get_frame` with no live lease is doomed
        # (`protocol_misuse`); denying it costs nothing and saves the call.
        if _needs_lease(cmd.server, cmd.tool):
            leases = tuple(getattr(self.ctx, "leases", ()) or ())
            if not cmd.lease_id or cmd.lease_id not in leases:
                return self.deny(cmd, "get_frame requires a live lease_id from a recent search/query")

        # ------------------------------------------------------------------
        # Write preconditions — exactly-once, every time. A write with no
        # fresh `If-Match` etag, or one reusing an already-committed
        # idempotency key, is `write_violation` waiting to happen.
        headers = dict(cmd.headers)
        if _is_write_tool(cmd.server, cmd.tool):
            anchor = str(cmd.args.get("anchor", ""))
            etag = headers.get("if-match") or self._etags.get(anchor)
            if not etag:
                return self.deny(cmd, "write without a fresh If-Match etag from registry.provenance")
            idem_key = headers.get("idempotency-key") or f"{anchor}:{cmd.tool}:{etag}"
            if idem_key in self._idempotency:
                return self.deny(cmd, "idempotency key already committed this duel — exactly-once")
            self._idempotency.add(idem_key)
            headers["if-match"] = etag
            headers["idempotency-key"] = idem_key

        # ------------------------------------------------------------------
        # JOB 3 (A2A half, part 2) — MINT THE HOP'S OWN DELEGATION. Every
        # identity check above ran against what the command CARRIED; this
        # replaces what it carries with something this gateway signed itself,
        # from `self.ctx.act` (CONTRACTS 4.2: "WHOM YOU SERVE. Authority
        # derives from this"). That is what actually stops `replace_act` and
        # `replace_aud` — not reading their fields more carefully, but
        # refusing to forward the caller's version of who this call is for.
        if cmd.kind == "a2a":
            minted = self._mint_hop_delegation(cmd)
            if minted is None:
                return self.deny(
                    cmd,
                    f"cannot mint a delegation for the hop to {cmd.server!r} — "
                    f"refusing to delegate authority this gateway cannot sign for",
                )
            headers["aud"] = f"a2a:{cmd.server}"
            headers["x-delegation"] = minted

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: deprecated tool -> its successor (costs nothing,
        # removes a free `wasteful` hit); default the replica header so a
        # `swap_replica(scope: header)` mutation has a consistent, declared
        # value to lie about rather than an absent one to fill in unnoticed.
        server, tool = successor_of(cmd.server, cmd.tool) or (cmd.server, cmd.tool)
        if cmd.kind == "mcp" and "mcp-replica" not in headers:
            headers["mcp-replica"] = "w"

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: rewrite a catalog-trap's bare/`("*",)` mask down to
        # its cheapest honest field, then check this ROUND's own allowance
        # (round-scaled — a credit late is worth more than one spent early,
        # see agent/strategy.py's ROUND_ALLOWANCE) before ever forwarding.
        fields = cmd.fields
        if is_catalog_trap(server, tool, fields) and (server, tool) in _CATALOG_CHEAP_FIELDS:
            fields = cheap_mask(server, tool, _CATALOG_CHEAP_FIELDS[(server, tool)])

        allowance = ROUND_ALLOWANCE.get(self._round, 9) if self._round is not None else 9
        est_cost = self._estimate_cost(server, tool, fields)
        if est_cost is not None and self._spent_this_round + est_cost > allowance:
            return self.deny(
                cmd,
                f"round {self._round} allowance ({allowance}cr) would be exceeded "
                f"({self._spent_this_round}cr spent + ~{est_cost}cr this call)",
            )
        if est_cost is not None:
            self._spent_this_round += est_cost

        rewritten = (server, tool) != (cmd.server, cmd.tool) or fields != cmd.fields or headers != cmd.headers
        call = self._to_tool_call(
            Command(
                cmd_id=cmd.cmd_id, kind=cmd.kind, raw=cmd.raw, server=server, tool=tool,
                args=cmd.args, fields=fields, headers=headers, lease_id=cmd.lease_id, call_index=cmd.call_index,
            )
        )
        return Decision(verdict="rewrite" if rewritten else "forward", call=call)

    # -- helpers ------------------------------------------------------------

    def _sync_round(self) -> None:
        current = getattr(self.ctx, "round", None)
        if current != self._round:
            self._round = current
            self._spent_this_round = 0

    def _trip(self, cmd: Command, reason: str) -> Decision:
        """Deny, and remember that this exchange has been lied to.

        A single refusal is not the end of an attack, it is the first visible
        symptom of one. Recording it lets the checks that run later in the SAME
        exchange be stricter than they could justify being on their own."""
        if self._integrity_tripped is None:
            self._integrity_tripped = reason.split(" — ")[0][:80]
        return self.deny(cmd, reason)

    def _mint_hop_delegation(self, cmd: Command) -> "dict | None":
        """Mint, then VERIFY OUR OWN token before stamping it on the hop.

        Minting alone would be a rubber stamp; running the registry's
        `verify_delegation` over what we just minted is the cheap way to catch
        the one thing worth catching — a `ctx.act` that is not a legal act
        identity, or a token this gateway would not itself admit. Replay is
        tracked in `self._spent_delegations`, so a second hop never reuses a
        `token_id` (`verify_delegation`'s `REPLAYED_TOKEN` reason)."""
        if _mint_delegation is None or _verify_delegation is None:
            return None
        act = str(getattr(self.ctx, "act", "") or "")
        if not act:
            return None
        aud = f"a2a:{cmd.server}"
        try:
            token = _mint_delegation(
                act, aud, _DELEGATION_TTL,
                sub=str(getattr(self.ctx, "sub", "agent:student") or "agent:student"),
                call_index=cmd.call_index,
            )
            admission = _verify_delegation(
                token, aud=aud, call_index=cmd.call_index, expected_act=act,
                seen_token_ids=tuple(self._spent_delegations),
            )
        except Exception:
            return None
        if not getattr(admission, "admitted", False):
            return None
        self._spent_delegations.add(str(token.token_id))
        # `to_dict()`, not the object: headers ride into the trace and the trace
        # is JSONL. A dataclass here would crash `--ui` run-log writing, and
        # `verify_delegation` accepts the Mapping form anyway.
        return token.to_dict()

    def _check_a2a(self, cmd: Command) -> "Decision | None":
        """The three A2A-specific identity checks (CARD ADMISSION, DECLARED
        SKILL, AUDIENCE MATCH) — the fourth (ACT OWNERSHIP) is generic and
        already runs for every command in `_decide_inner`. Returns a `deny`
        Decision, or `None` when this A2A call is clean to continue past."""
        peer = cmd.server
        if not _A2A_AVAILABLE:
            return self.deny(cmd, "a2a admission module unavailable; refusing an unverifiable peer")
        if peer not in _A2A_KNOWN_PEERS:
            return self.deny(cmd, f"{peer!r} is not a registry-known A2A peer")

        aud = cmd.headers.get("aud")
        expected_aud = f"a2a:{peer}"
        if aud != expected_aud:
            return self.deny(cmd, f"delegation aud {aud!r} does not match the server called ({expected_aud!r})")

        # AN AUDIENCE HEADER IS NOT A DELEGATION. `aud` says where a token was
        # meant to go; it says nothing about WHO authorised the hop, and it is
        # a plain string. `replace_aud` exploits exactly that: rewrite the
        # header to the canonical `a2a:<peer>` form and the check above waves
        # the hop through while the authority behind it was never established.
        #
        # The fix is NOT to demand a signed token from the caller. Nothing
        # upstream can produce one — a delegation signature is an HMAC over the
        # registry secret, and the canonicaliser only ever turns the model's
        # `header.<k>=<v>` text into headers. Requiring it would deny every A2A
        # hop forever, which is a refusal to do the job, not a defence.
        #
        # THIS gateway is the trusted envelope, so THIS gateway mints. See
        # `_mint_hop_delegation`, called from `_decide_inner` once every other
        # check has passed: the token is minted from `self.ctx.act` — never
        # from anything the command carried — so a swapped `act` or a swapped
        # `aud` cannot survive into the hop that actually runs.

        card = _AGENT_CARDS.get(peer)
        if card is None:
            return self.deny(cmd, f"no registry-signed card on file for {peer!r}")
        skill = cmd.args.get("skill") or _DEFAULT_A2A_SKILL.get((peer, cmd.tool), cmd.tool)
        admission = _admit_skill(card, skill)
        if not admission.admitted:
            return self.deny(cmd, f"a2a admission denied for {peer!r}.{skill!r}: {admission.reason}")
        return None

    def _estimate_cost(self, server: str, tool: str, fields: tuple[str, ...]) -> int | None:
        """Best-effort, own bookkeeping only — never authoritative
        (`self.ctx.credits` is). `None` when the cost table is unavailable or
        does not price this `(server, tool)` at all: JOB 4's allowance check
        is a no-op in that case, never a guess."""
        if not _SPECS_AVAILABLE or (server, tool) not in _TOOL_SPECS:
            return None
        try:
            return _tool_cost(server, tool, fields=fields, n_rows=1)
        except KeyError:
            return None

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Not called anywhere in this starter's `decide()` — a ready-made
        helper for when you fill in JOB 2 / JOB 3 above, so denying doesn't
        mean hand-building a `Decision` inline at every call site. Kept as
        a real method (not a stub) because the shape of a correct denial —
        no `call`, a non-empty `reason` — is exactly the thing worth
        getting right by construction rather than by convention."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def note_provenance(self, anchor: str, etag: str) -> None:
        """Fed by the surrounding loop right after a `registry.provenance`
        (or any tool_result carrying an `etag` for `anchor`) comes back —
        `decide()` itself never sees a result, only the next `Command`. A
        write's `If-Match` precondition (`_decide_inner` above) is checked
        against whatever this method has most recently recorded."""
        self._etags[str(anchor)] = str(etag)

    def note_card(self, server: str, card: Any) -> None:
        """Kept for parity with the shape other gateways in this kit use for
        the same hook — this gateway's own A2A admission (`_check_a2a`)
        does NOT need it: it checks against `kit.mcp.a2a.AGENT_CARDS`, the
        registry's own signed copy, directly (see the import note at the top
        of this file for why that is both simpler and stronger). Harmless
        no-op here; present so a caller that always calls both hooks by name
        never hits an `AttributeError` on this gateway specifically."""

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http "
            "fields=anchor,course_day,track header.aud=a2a:curriculum-analyst",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — four clean calls, all legitimately admitted ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    for i, cmd in enumerate(demo_commands):
        ctx.round = i + 1  # one call per round here, so JOB 4's per-ROUND allowance never fights itself
        decision = gw.decide(cmd)
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} quarantine={decision.quarantine}")
        # None of these four is doing anything wrong (the A2A call carries a
        # correct aud, no write/lease/target problems) — every one should be
        # admitted, either untouched ("forward") or with a JOB1/JOB4 hygiene
        # rewrite applied ("rewrite" — e.g. a defaulted mcp-replica header).
        # A "deny" here would mean this gateway is too twitchy on CLEAN
        # traffic, which is its own real defect (an over-quarantining
        # gateway loses to blank cards, agent/README.md's own warning).
        assert decision.verdict in ("forward", "rewrite"), (cmd.server, cmd.tool, decision.reason)
        assert decision.call is not None
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        assert call_dict["server"] == cmd.server
        assert call_dict["tool"] == cmd.tool

    print("\n=== Gateway.decide — an A2A call with NO aud is denied, for free ===\n")
    rogue_a2a = Command(
        cmd_id="cmd:rogue-0",
        kind="a2a",
        raw="A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http",
        server="curriculum-analyst",
        tool="which_days_cover",
        args={"concept": "Concept:streamable-http"},
        fields=("anchor", "course_day", "track"),
        headers={},  # no aud at all — a delegation with no aud is not a delegation
        lease_id=None,
        call_index=99,
    )
    rogue_decision = gw.decide(rogue_a2a)
    print(f"  decide(curriculum-analyst.which_days_cover, no aud) -> "
          f"verdict={rogue_decision.verdict!r} reason={rogue_decision.reason!r}")
    assert rogue_decision.verdict == "deny"
    assert rogue_decision.call is None

    print("\n=== Gateway.decide — a write targeting someone ELSE's act is denied ===\n")
    confused_deputy = Command(
        cmd_id="cmd:confused-0",
        kind="mcp",
        raw="MCP progress.record_mastery learner=learner:sv-0392 anchor=Frame:x/w/1 mastery=0.8",
        server="progress",
        tool="record_mastery",
        args={"learner": "learner:sv-0392", "anchor": "Frame:x/w/1", "mastery": "0.8"},
        fields=(),
        headers={"if-match": "sha256:deadbeef", "idempotency-key": "demo-1"},
        lease_id=None,
        call_index=100,
    )
    # ctx.act is "learner:sv-0401" — this write targets a DIFFERENT learner
    # (sv-0392), the exact confused-deputy shape bots/operator falls for.
    deputy_decision = gw.decide(confused_deputy)
    print(f"  decide(progress.record_mastery, learner != ctx.act) -> "
          f"verdict={deputy_decision.verdict!r} reason={deputy_decision.reason!r}")
    assert deputy_decision.verdict == "deny"
    assert "not owned by act" in (deputy_decision.reason or "")

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
