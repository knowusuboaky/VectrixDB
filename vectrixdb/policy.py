"""Entitlement policy: a filter the collection cannot be queried without.

An entitlement filter that the caller builds on every query is a control that
works until somebody forgets. `search_with_acl(acl_field="_acl")` is opt-in,
so a call site that omits it returns everything, and nothing anywhere says so.
A policy is the same predicate declared once, against the collection, with no
way to run a search that skips it.

Nothing here authenticates anybody. A principal is a plain mapping the host
resolved from its own directory, CRM or control room, and this module has no
way to check it and does not try. What it does is decide, given that mapping,
which documents a query may see, which is the same category of work as the
filter engine it compiles down to.

Two properties are load-bearing and both come from the filter engine rather
than from anything written here:

* **An absent field on a document denies.** `FilterCondition.matches` returns
  False for `MISSING` before any operator runs, so a chunk that arrives with
  no entitlement metadata is invisible rather than universal. There is no
  option to turn that off. A mixed collection with genuinely public documents
  should say so in a field and have a rule that permits it, which is a
  statement a reviewer can read, rather than a global fail-open.
* **An empty principal value matches nothing, on its own.** `$in []` matches
  no document and `$nin []` excludes none, which is the correct reading of
  both without a special case. That is only true because membership was fixed
  to mean overlap earlier in 2.2; before that, `$in`/`$nin` tested the whole
  field against the operand and a list-valued field never matched.

What is *not* free is a principal missing a key the policy names. That is not
a denial, it is the host's entitlement resolver failing, and conflating the
two hides a broken resolver behind traffic that looks normal. It raises.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, ClassVar, Iterable, Mapping, Optional, Sequence

from .exceptions import PolicyDefinitionError, PrincipalIncomplete

__all__ = [
    "Outcome",
    "Predicate",
    "Overlap",
    "Equals",
    "AtMost",
    "AtLeast",
    "Excludes",
    "Present",
    "Decision",
    "Policy",
]


# ============================================================================
# THE OUTCOME
# ============================================================================
#
# INPUT   one policied search
# OUTPUT  how it ended
#
# Allowed, denied, refused, or undecidable, with the words.


class Outcome(str, Enum):
    """How one policied search ended.

    Four families, because they belong on different dashboards. A denial is
    normal traffic and expected in volume. An undecidable or a refusal means
    something upstream is broken and somebody should be woken up. A single
    value covering both is how a policy fails closed for a week unnoticed,
    every record reading "denied" and denials looking like a busy Tuesday.
    """

    #: Everything the principal matched was returned.
    ALLOWED = "allowed"
    #: Returned, with some in-scope documents withheld by a redaction rule.
    ALLOWED_WITH_REDACTION = "allowed_with_redaction"
    #: The principal matched the scope and every candidate was redacted.
    DENIED_IN_SCOPE = "denied_in_scope"
    #: The principal did not match the scope. Indistinguishable from a query
    #: for something that does not exist, which is the point.
    DENIED_OUT_OF_SCOPE = "denied_out_of_scope"
    #: A candidate lacked a field the policy names. Denied, and a bug.
    UNDECIDABLE_DOCUMENT = "undecidable_document"
    #: The principal lacked a key the policy names. The resolver is broken.
    UNDECIDABLE_PRINCIPAL = "undecidable_principal"
    #: A policied collection was queried with no principal.
    REFUSED_NO_PRINCIPAL = "refused_no_principal"
    #: The stored policy is not the one the caller opened with.
    REFUSED_POLICY_MISMATCH = "refused_policy_mismatch"
    #: The policy required engine-side filtering and the backend post-filters.
    REFUSED_NO_PUSHDOWN = "refused_no_pushdown"
    #: The audit sink was unreachable and its failure policy is to deny.
    REFUSED_AUDIT_UNAVAILABLE = "refused_audit_unavailable"


# ============================================================================
# THE PREDICATES: one rule each
# ============================================================================
#
# INPUT   a field on the document and a key on the principal
# OUTPUT  overlap, equals, at most, at least, excludes, present: each relating
#         the two, with what values it can be tested against
#
# A rule that cannot be tested against a value says so, rather than deciding.


def _is_membership_value(value: Any) -> bool:
    """A value membership can be tested against.

    An empty list passes: a document nobody is allowed to see is a thing
    somebody may legitimately write, and the same reasoning covers a null.
    What does not pass is a shape the comparison can only ever be false
    against, which is a mistake rather than a statement.
    """
    if value is None:
        return True
    if isinstance(value, (list, tuple, set)):
        return all(isinstance(item, (str, int, float)) for item in value)
    return isinstance(value, (str, int, float))


def _is_ordered_value(value: Any) -> bool:
    """A value an ordering can be applied to.

    Booleans are numbers in Python, so a rank of True would compare as 1 and
    pass. It is refused: nobody means that, and a clearance comparison that
    quietly reads True as level one is worse than a refused write.
    """
    if value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Predicate:
    """One rule, relating a field on the document to a key on the principal.

    `doc` is a dotted path into the document's metadata, the same notation the
    filter engine already reads. `principal` is a key in the principal
    mapping. `scope` marks the rule as defining *whether the principal may
    know this document exists at all*, as against redacting something inside a
    scope they already hold; see `Decision` for why that distinction has to be
    declared rather than inferred.
    """

    doc: str
    principal: Optional[str] = None
    scope: bool = False

    #: The filter operator this compiles to.
    OPERATOR: ClassVar[str] = ""
    #: Whether the rule reads a value from the principal at all.
    READS_PRINCIPAL: ClassVar[bool] = True
    #: Whether an empty principal value makes the whole policy unsatisfiable,
    #: so a search can be skipped rather than run to return nothing.
    MATCHES_NOTHING_WHEN_EMPTY: ClassVar[bool] = False
    #: What this rule can decide about, in words, for the message a write
    #: gets when it stamps something else.
    ACCEPTS: ClassVar[str] = "any value"

    def accepts(self, value: Any) -> bool:
        """Whether this rule can reach a verdict on such a document value.

        Not whether it passes. A rank of 9 is decidable and denied; a rank of
        "high" is neither, because it is not greater than any clearance and
        not less than one either, so the rule is false for every principal
        and the document is invisible to everybody. That is a pipeline bug
        worth catching at the write, where the stack trace points at the
        pipeline.
        """
        return True

    def operand(self, value: Any) -> Any:
        """The principal's value, shaped for the operator."""
        return value

    def clause(self, value: Any) -> dict[str, Any]:
        """This rule as one filter clause."""
        return {"field": self.doc, "op": self.OPERATOR, "value": self.operand(value)}

    def describe(self) -> str:
        """A short label, used in decision records and test failures."""
        if not self.READS_PRINCIPAL:
            return f"{type(self).__name__}({self.doc})"
        return f"{type(self).__name__}({self.doc} <- {self.principal})"


@dataclass(frozen=True)
class Overlap(Predicate):
    """The document's field shares at least one value with the principal's.

    The workhorse: roles against allowed roles, a care team against the teams
    someone is on, a client id against a book of business. Either side may be
    a list or a scalar.
    """

    OPERATOR: ClassVar[str] = "in"
    MATCHES_NOTHING_WHEN_EMPTY: ClassVar[bool] = True
    ACCEPTS: ClassVar[str] = "a value, or a list of them"

    def operand(self, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    def accepts(self, value: Any) -> bool:
        return _is_membership_value(value)


@dataclass(frozen=True)
class Equals(Predicate):
    """The document's field is exactly the principal's value."""

    OPERATOR: ClassVar[str] = "eq"


@dataclass(frozen=True)
class AtMost(Predicate):
    """The document's field is no greater than the principal's value.

    Clearance. This is why a classification wants a numeric rank beside its
    label: "cleared to at least this level" is an ordering question, and set
    membership cannot answer it without enumerating every level below.

    A principal value of zero is a real clearance, not an absent one, which is
    why nothing here tests it for truthiness.
    """

    OPERATOR: ClassVar[str] = "lte"
    ACCEPTS: ClassVar[str] = "a number"

    def accepts(self, value: Any) -> bool:
        return _is_ordered_value(value)


@dataclass(frozen=True)
class AtLeast(Predicate):
    """The document's field is no less than the principal's value."""

    OPERATOR: ClassVar[str] = "gte"
    ACCEPTS: ClassVar[str] = "a number"

    def accepts(self, value: Any) -> bool:
        return _is_ordered_value(value)


@dataclass(frozen=True)
class Excludes(Predicate):
    """The document's field is none of the principal's values.

    Walls and conflicts: the list names what this principal may not see. An
    empty list excludes nothing, which is the right reading, and needs no
    special case because `$nin []` already matches every document.
    """

    OPERATOR: ClassVar[str] = "nin"
    ACCEPTS: ClassVar[str] = "a value, or a list of them"

    def operand(self, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    def accepts(self, value: Any) -> bool:
        return _is_membership_value(value)


@dataclass(frozen=True)
class Present(Predicate):
    """The document has the field at all.

    Reads nothing from the principal. Use it to require that a field the rest
    of the policy depends on was actually stamped at ingestion, so a gap in
    the pipeline reads as a gap rather than as a permissions question.
    """

    OPERATOR: ClassVar[str] = "exists"
    READS_PRINCIPAL: ClassVar[bool] = False

    def operand(self, value: Any) -> Any:
        return True


# ============================================================================
# THE DECISION, AND THE POLICY
# ============================================================================
#
# INPUT   a policy as data, a principal, and a document's metadata
# OUTPUT  the outcome of evaluating the policy against one document; the rule
#         set, evaluated as one conjunction, with its fingerprint
#
# An entitlement filter the caller builds on every query is a control that
# works until someone forgets it; a policy the collection cannot be queried
# without is one that cannot be forgotten.


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating a policy against one document.

    `in_scope` is the field that keeps an ethical wall standing. A principal
    who matched every scope rule and lost a document to a clearance rule can
    safely be told something was withheld: they already know the client, the
    matter, the patient exists. A principal who failed a scope rule cannot be
    told anything at all, because "one result withheld" confirms the thing is
    there, and that is exactly what a wall is built to prevent.

    Which rules are scope cannot be worked out from the rules themselves, so
    `Predicate.scope` declares it.
    """

    allowed: bool
    in_scope: bool
    failed: tuple[str, ...] = ()

    @property
    def disclosable(self) -> bool:
        """Whether the caller may be told this document was withheld."""
        return self.in_scope and not self.allowed


class Policy:
    """An entitlement rule set, evaluated as one conjunction.

    Every rule must pass. There is no `or` and no nesting, deliberately: the
    value of a small vocabulary is that somebody reviewing a policy can read
    it once and be certain what it does. A case study that genuinely needs a
    seventh predicate should get the predicate rather than an expression
    language nobody can audit.

        >>> policy = Policy([
        ...     Overlap("entitlements.allowed_roles", "roles"),
        ...     AtMost("entitlements.classification_rank", "clearance_rank"),
        ...     Overlap("entitlements.client_id", "client_coverage", scope=True),
        ...     Excludes("entitlements.client_id", "wall_restrictions", scope=True),
        ... ])
        >>> policy.required_principal_keys == frozenset(
        ...     {"roles", "clearance_rank", "client_coverage", "wall_restrictions"}
        ... )
        True
    """

    def __init__(
        self,
        rules: Sequence[Predicate],
        version: Optional[str] = None,
        require_pushdown: bool = False,
        db_role_key: Optional[str] = None,
        on_incomplete_document: str = "reject",
    ) -> None:
        """
        Args:
            rules: the predicates, all of which must pass.
            version: an optional human label carried on every decision
                record beside the fingerprint. The fingerprint is what
                identifies the rule set; this is what a person reads.
            db_role_key: the principal key holding the PostgreSQL role this
                read should run as, so row level security applies underneath
                the filter. The host maps its own principals to roles; this
                only says which key to look in, the same way the rules say
                which keys to read.
            on_incomplete_document: what to do when a document arrives
                without a field the rules name. Reject refuses the write,
                warn writes it and says so, allow is silent. It refuses by
                default, because the alternative is a chunk nobody can ever
                see failing in a way that reads as a permissions problem.
                Warn is for backfilling an existing collection.
            require_pushdown: refuse to open a collection whose backend
                filters after fetching rather than in the query. Nothing
                does the latter yet, so this refuses everywhere today, which
                is the honest answer and the reason it exists: a deployment
                that needs engine-side enforcement should be told it does
                not have it rather than assume it does.
        """
        rules = tuple(rules)
        if not rules:
            # A policy with no rules permits everything, which is a more
            # dangerous thing to have written by accident than to have
            # omitted: it looks like a control and is not one.
            raise PolicyDefinitionError("a policy needs at least one rule; an empty one allows all")
        for rule in rules:
            if not isinstance(rule, Predicate):
                raise PolicyDefinitionError(f"not a predicate: {rule!r}")
            if type(rule) is Predicate:
                raise PolicyDefinitionError(
                    "Predicate is the base class and has no operator. Use Overlap, "
                    "Equals, AtMost, AtLeast, Excludes or Present."
                )
            if rule.READS_PRINCIPAL and not rule.principal:
                raise PolicyDefinitionError(
                    f"{type(rule).__name__}({rule.doc!r}) needs a principal key to read"
                )
            if not rule.READS_PRINCIPAL and rule.principal:
                raise PolicyDefinitionError(
                    f"{type(rule).__name__}({rule.doc!r}) reads nothing from the principal, "
                    f"so principal={rule.principal!r} would be silently ignored"
                )
        self.rules: tuple[Predicate, ...] = rules
        self.version = version
        self.require_pushdown = require_pushdown
        if on_incomplete_document not in ("reject", "warn", "allow"):
            raise PolicyDefinitionError(
                f"on_incomplete_document is 'reject', 'warn' or 'allow', "
                f"got {on_incomplete_document!r}"
            )
        self.db_role_key = db_role_key
        self.on_incomplete_document = on_incomplete_document

    # -- identity ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The canonical form, for persisting beside the collection.

        ``on_incomplete_document`` is written only when it is not the default,
        ``reject``. Until 2.2 it was never written at all, so a policy read back
        from a collection silently became one that rejects; written always, it
        would change the fingerprint of every policy already stored and every
        policied collection would refuse to open. Written when it differs, a
        policy stored before keeps its fingerprint exactly, and one that warns
        or allows still says so after the round trip.
        """
        data: dict[str, Any] = {
            "version": 1,
            "label": self.version,
            "rules": [
                {
                    "kind": type(rule).__name__,
                    "doc": rule.doc,
                    "principal": rule.principal,
                    "scope": rule.scope,
                }
                for rule in self.rules
            ],
        }
        if self.on_incomplete_document != "reject":
            data["on_incomplete_document"] = self.on_incomplete_document
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Policy":
        """Rebuild a policy from `to_dict`, what to do with an incomplete document included."""
        kinds = {k.__name__: k for k in (Overlap, Equals, AtMost, AtLeast, Excludes, Present)}
        rules = []
        for entry in data.get("rules", ()):
            kind = kinds.get(entry.get("kind"))
            if kind is None:
                raise PolicyDefinitionError(
                    f"unknown predicate {entry.get('kind')!r}; this collection was written "
                    f"by a newer VectrixDB and cannot be opened safely"
                )
            rules.append(
                kind(
                    doc=entry["doc"],
                    principal=entry.get("principal"),
                    scope=bool(entry.get("scope", False)),
                )
            )
        return cls(rules, version=data.get("label"), on_incomplete_document=str(data.get("on_incomplete_document") or "reject"))

    @property
    def fingerprint(self) -> str:
        """A stable hash of the rule set.

        Carried on every decision record. A version number can be forgotten
        when somebody edits a rule; a fingerprint cannot, which is the whole
        point. Without it, a past decision that no longer reproduces gives no
        way to tell a policy change from a data change.
        """
        rules_only = {k: v for k, v in self.to_dict().items() if k != "label"}
        canonical = json.dumps(rules_only, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    @property
    def required_principal_keys(self) -> frozenset[str]:
        """Every key a principal must carry for this policy to be evaluable."""
        return frozenset(r.principal for r in self.rules if r.READS_PRINCIPAL and r.principal)

    @property
    def scope_rules(self) -> tuple[Predicate, ...]:
        return tuple(r for r in self.rules if r.scope)

    def describe(self) -> str:
        return " AND ".join(r.describe() for r in self.rules)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Policy {len(self.rules)} rules {self.fingerprint}>"

    # -- evaluation -------------------------------------------------------

    def _value(self, principal: Mapping[str, Any], rule: Predicate) -> Any:
        """The principal's value for one rule, or raise.

        A key that is absent, or present and None, is the host's resolver
        failing rather than a principal without permission. Failing closed is
        right; failing closed *quietly* is how the failure hides.
        """
        assert rule.principal is not None  # guaranteed by __init__
        if rule.principal not in principal:
            raise PrincipalIncomplete(rule.principal, rule.describe())
        value = principal[rule.principal]
        if value is None:
            raise PrincipalIncomplete(rule.principal, rule.describe(), reason="resolved to None")
        return value

    @property
    def required_document_fields(self) -> tuple[str, ...]:
        """Every document path the rules read.

        A document missing any of them is invisible to everybody, because an
        absent field denies. That is what makes them a contract a write has
        to meet, and the reason it is worth meeting at write time.
        """
        return tuple(dict.fromkeys(rule.doc for rule in self.rules))

    def missing_fields(self, metadata: Mapping[str, Any]) -> list[str]:
        """Which required paths this document does not have."""
        from .core.types import MISSING, FilterCondition

        probe = FilterCondition(field="", operator="exists", value=True)
        return [
            path
            for path in self.required_document_fields
            if probe._get_nested_value(dict(metadata), path) is MISSING
        ]

    def undecidable_fields(self, metadata: Mapping[str, Any]) -> list[tuple[str, Any, str]]:
        """Fields this document stamps that no rule can reach a verdict on.

        The companion to `missing_fields`, and the same failure: a
        classification rank stamped as "high" is not greater than any
        clearance and not less than one either, so the rule is false for
        every principal and the document is invisible to everybody. Absent
        and undecidable are one bug with two spellings.

        Returns (path, value, what the rule needs) for each.
        """
        from .core.types import MISSING, FilterCondition

        probe = FilterCondition(field="", operator="exists", value=True)
        found = []
        for rule in self.rules:
            value = probe._get_nested_value(dict(metadata), rule.doc)
            if value is MISSING:
                continue  # missing_fields has this one
            if not rule.accepts(value):
                found.append((rule.doc, value, rule.ACCEPTS))
        return found

    def db_role(self, principal: Mapping[str, Any]) -> Optional[str]:
        """The database role this principal reads as, if the policy names one.

        Missing is the resolver failing, the same as any other key the policy
        reads: running as the connection's own role instead would mean the
        read happens with whatever privileges the application holds, which is
        the opposite of the point.
        """
        if self.db_role_key is None:
            return None
        if self.db_role_key not in principal:
            raise PrincipalIncomplete(self.db_role_key, "the database role this reads as")
        role = principal[self.db_role_key]
        if not role:
            raise PrincipalIncomplete(
                self.db_role_key, "the database role this reads as", reason="resolved to empty"
            )
        return str(role)

    def compile(
        self, principal: Mapping[str, Any], *, scope_only: bool = False
    ) -> Optional[dict[str, Any]]:
        """This policy as a filter for `search(filter=...)`.

        With `scope_only`, just the scope rules: the part a store can be
        handed so a document outside the scope never leaves it, while the
        redaction rules are still decided here where they can be counted.

        Returns None when the policy cannot match anything, so the caller can
        skip the search entirely rather than run one guaranteed to come back
        empty. That shortcut leaks nothing about the documents: whether a
        principal holds any groups is a fact about the principal, which they
        already know.

        The `$and` form is used rather than a field-keyed dict so that two
        rules over the same field, which is exactly how a wall sits beside a
        coverage list, cannot collide.
        """
        clauses = []
        for rule in self.rules:
            if scope_only and not rule.scope:
                continue
            if not rule.READS_PRINCIPAL:
                clauses.append(rule.clause(None))
                continue
            value = self._value(principal, rule)
            operand = rule.operand(value)
            if rule.MATCHES_NOTHING_WHEN_EMPTY and not operand:
                return None
            clauses.append(rule.clause(value))
        return {"$and": clauses}

    def decide(self, principal: Mapping[str, Any], metadata: Mapping[str, Any]) -> Decision:
        """Evaluate the policy against one document's metadata.

        Used for the withheld counts rather than for filtering, which the
        engine does. Scope rules are evaluated separately from the rest so
        that a document lost to a clearance rule can be reported and one lost
        to a wall cannot.
        """
        from .core.types import FilterCondition  # heavy import, and only needed here

        def passes(rule: Predicate) -> bool:
            value = None if not rule.READS_PRINCIPAL else self._value(principal, rule)
            clause = rule.clause(value)
            condition = FilterCondition(
                field=clause["field"], operator=clause["op"], value=clause["value"]
            )
            return condition.matches(dict(metadata))

        failed_scope = [r for r in self.rules if r.scope and not passes(r)]
        # A document out of scope is not evaluated further. Nothing depends on
        # it, and the fewer rules run against something the principal may not
        # know exists, the less there is to leak through timing.
        if failed_scope:
            return Decision(
                allowed=False,
                in_scope=False,
                failed=tuple(r.describe() for r in failed_scope),
            )

        failed_other = [r for r in self.rules if not r.scope and not passes(r)]
        return Decision(
            allowed=not failed_other,
            in_scope=True,
            failed=tuple(r.describe() for r in failed_other),
        )

    def classify(
        self,
        principal: Mapping[str, Any],
        items: Iterable[Any],
        metadata_of: Optional[Callable[[Any], Mapping[str, Any]]] = None,
    ) -> tuple[list[Any], int, int, tuple[str, ...]]:
        """Split anything carrying metadata into allowed and the two withhelds.

        Keeping the two counts apart is the only thing standing between an
        audit record and a broken ethical wall, so it is implemented once,
        here, and both callers go through it. `metadata_of` pulls the
        metadata off whatever the caller is holding; the default treats each
        item as the metadata itself.

        Returns the allowed items, the disclosable count, the undisclosable
        count, and every rule that rejected at least one candidate.
        """
        get = metadata_of or (lambda item: item)
        allowed: list[Any] = []
        disclosable = 0
        undisclosable = 0
        denied: set[str] = set()
        for item in items:
            decision = self.decide(principal, get(item) or {})
            if decision.allowed:
                allowed.append(item)
            elif decision.in_scope:
                disclosable += 1
            else:
                undisclosable += 1
            denied.update(decision.failed)
        return allowed, disclosable, undisclosable, tuple(sorted(denied))

    def partition(
        self,
        principal: Mapping[str, Any],
        candidates: Iterable[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], int, int]:
        """The metadata-only form of :meth:`classify`, without the rules.

        A caller handed a single "suppressed" number will render it, and the
        first time somebody outside a scope sees "7 results withheld" the wall
        is gone. That is why this returns two.
        """
        allowed, disclosable, undisclosable, _ = self.classify(principal, candidates)
        return allowed, disclosable, undisclosable

    def outcome(self, returned: int, disclosable: int, undisclosable: int) -> Outcome:
        """The outcome value for a finished search."""
        if returned:
            return Outcome.ALLOWED_WITH_REDACTION if disclosable else Outcome.ALLOWED
        if disclosable:
            return Outcome.DENIED_IN_SCOPE
        return Outcome.DENIED_OUT_OF_SCOPE
