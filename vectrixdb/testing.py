"""Assert that a policy does what you think it does.

A policy is a security control written as data, which makes it exactly the
kind of thing that is easy to get subtly wrong and hard to notice. The
failures are quiet by construction: a rule that is too tight shows up as an
empty result somebody blames on the index, and a rule that is too loose shows
up as nothing at all.

So these are shipped rather than kept in this project's own tests. The
policies that matter are yours, not ours.

Three kinds of check, in the order they earn their keep:

* :func:`assert_visibility` is a table of who can see what, and the failure
  prints the whole table with the disagreements marked. A reviewer can read
  it; so can a model risk validator, which is usually the point.
* :func:`assert_allows` and :func:`assert_denies` are the single-document
  form, and their failures name the rule that decided and show both sides of
  the comparison, because "assert False" tells you nothing you did not
  already know.
* :func:`assert_indistinguishable` is the ethical wall assertion: two
  searches that must not differ in any way a caller can observe. It is the
  property the whole design rests on and the one nothing else can check.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Optional

__all__ = [
    "explain",
    "assert_allows",
    "assert_denies",
    "assert_visibility",
    "assert_indistinguishable",
]


# ============================================================================
# EXPLAIN, ALLOW, DENY, THE TABLE, AND INDISTINGUISHABLE
# ============================================================================
#
# INPUT   a policy, principals and documents; two searches
# OUTPUT  why the policy reached its verdict; an assertion that this principal
#         may, or may not, see this document; the whole table of who can see
#         what asserted; two searches a caller must not be able to tell apart
#
# A policy is a security control written as data, which makes it exactly the
# kind of thing that is easy to get subtly wrong; these are the tests to write
# for one.


def explain(policy: Any, principal: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    """Why this policy reached its verdict on this document.

    Readable on purpose. A decision that is wrong is usually wrong in one
    rule, and the useful output is which rule and what the two sides held,
    not a boolean.
    """
    decision = policy.decide(principal, metadata)
    verdict = (
        "ALLOWED"
        if decision.allowed
        else ("WITHHELD, in scope" if decision.in_scope else "WITHHELD, out of scope")
    )
    lines = [verdict]

    from .core.types import MISSING, FilterCondition

    probe = FilterCondition(field="", operator="exists", value=True)
    for rule in policy.rules:
        document_value = probe._get_nested_value(dict(metadata), rule.doc)
        shown = "<absent>" if document_value is MISSING else repr(document_value)
        if rule.READS_PRINCIPAL:
            held = principal.get(rule.principal, "<absent>")
            detail = f"document {rule.doc}={shown}  principal {rule.principal}={held!r}"
        else:
            detail = f"document {rule.doc}={shown}"
        mark = "x" if rule.describe() in decision.failed else " "
        lines.append(f"  [{mark}] {rule.describe()}")
        lines.append(f"        {detail}")
    return "\n".join(lines)


def assert_allows(
    policy: Any,
    principal: Mapping[str, Any],
    metadata: Mapping[str, Any],
    message: Optional[str] = None,
) -> None:
    """This principal may see this document."""
    decision = policy.decide(principal, metadata)
    if decision.allowed:
        return
    raise AssertionError(
        (message or "expected this document to be visible")
        + "\n"
        + explain(policy, principal, metadata)
    )


def assert_denies(
    policy: Any,
    principal: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    disclosable: Optional[bool] = None,
    message: Optional[str] = None,
) -> None:
    """This principal may not see this document.

    ``disclosable`` asserts *which kind* of withholding, which is worth being
    explicit about: in scope means the caller may be told something was held
    back, out of scope means telling them anything confirms the document
    exists. Getting those two the wrong way round is how an ethical wall
    fails while every test still passes.
    """
    decision = policy.decide(principal, metadata)
    if decision.allowed:
        raise AssertionError(
            (message or "expected this document to be withheld")
            + "\n"
            + explain(policy, principal, metadata)
        )
    if disclosable is None:
        return
    if decision.disclosable != disclosable:
        wanted = "in scope, so disclosable" if disclosable else "out of scope, so not disclosable"
        got = "in scope" if decision.in_scope else "out of scope"
        raise AssertionError(
            f"{message or 'withheld, but the wrong kind'}: expected {wanted}, got {got}\n"
            + explain(policy, principal, metadata)
        )


def assert_visibility(
    policy: Any,
    principals: Mapping[str, Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, Iterable[str]],
) -> None:
    """Assert the whole table of who can see what.

    The form a reviewer can actually check. Each principal maps to the set of
    document names it should see; anything not named is expected to be
    withheld, so adding a document without deciding who sees it fails rather
    than passing by omission.

        >>> assert_visibility(
        ...     policy,
        ...     principals={"on the deal team": ANALYST_A, "walled off": ANALYST_B},
        ...     documents={"covenant": covenant_meta, "memo": memo_meta},
        ...     expected={"on the deal team": {"covenant"}, "walled off": set()},
        ... )
    """
    missing = set(principals) - set(expected)
    if missing:
        raise AssertionError(
            f"no expectation given for {sorted(missing)}. Every principal needs a row, "
            f"or a policy change could widen access and still pass."
        )

    actual = {
        name: {doc_name for doc_name, meta in documents.items() if policy.decide(who, meta).allowed}
        for name, who in principals.items()
    }
    wanted = {name: set(rows) for name, rows in expected.items() if name in principals}

    unknown = {d for rows in wanted.values() for d in rows} - set(documents)
    if unknown:
        raise AssertionError(f"expectation names documents that were not given: {sorted(unknown)}")

    if actual == wanted:
        return

    width = max(len(n) for n in principals)
    lines = ["the visibility table does not match:", ""]
    for name in principals:
        got, want = actual[name], wanted[name]
        mark = " " if got == want else "x"
        lines.append(f"  [{mark}] {name:<{width}}  saw {sorted(got) or '[]'}")
        if got != want:
            lines.append(f"      {'':<{width}}  wanted {sorted(want) or '[]'}")
            for extra in sorted(got - want):
                lines.append(f"      {'':<{width}}  + {extra} should have been withheld")
            for absent in sorted(want - got):
                lines.append(f"      {'':<{width}}  - {absent} should have been visible")
    raise AssertionError("\n".join(lines))


#: What a caller can observe about a result, and therefore everything two
#: results must agree on for one to be indistinguishable from the other.
_OBSERVABLE = ("truncated", "cut_count", "degraded")


def assert_indistinguishable(
    one: Callable[[], Any],
    other: Callable[[], Any],
    message: Optional[str] = None,
    *,
    score_tolerance: Optional[float] = 0.10,
) -> None:
    """Two searches a caller must not be able to tell apart.

    The ethical wall assertion. A principal walled off from a client and a
    principal asking about a client that was never onboarded have to get the
    same answer, because "no documents found for that client" has already
    confirmed whether a relationship exists.

    Both arguments are callables so that a raise counts as an observation: an
    exception from one and a result from the other is a difference, and it is
    the difference people miss.

        >>> assert_indistinguishable(
        ...     lambda: walled_view.search("covenant thresholds"),
        ...     lambda: absent_view.search("covenant thresholds"),
        ... )

    A score counts as something the caller can observe, so it is compared,
    but to a tolerance rather than exactly, and the default comes from two
    measurements rather than taste.

    Embedding is not bit stable across batches. The same text embedded
    alongside others comes back with a vector up to 0.015 from the one it
    gets alone, which moved a cosine score by as much as 2 percent over the
    texts this was measured on. Nothing about that has to do with
    entitlements, and a security test that fails on noise is a security test
    somebody turns off.

    A sparse score, on the other hand, is a corpus statistic. Inverse
    document frequency and average document length count every document in
    the index, including the ones this principal was refused, so the same
    visible document scored 0.1131 with a walled client in the index and
    0.1823 without it: 38 percent, on a number handed straight to the caller.

    Ten percent sits five times above the noise and four times below the
    leak. Pass ``score_tolerance=None`` to skip the check, once you have
    satisfied yourself the scores are noise.

    What it does not check is **time**. A selective policy can take longer
    than a permissive one, and that is visible from outside; closing it needs
    a timing floor in the service layer rather than an assertion.
    """
    prefix = (message or "these two must be indistinguishable") + ": "

    def run(fn):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - the raise is the observation
            return None, exc

    first, first_error = run(one)
    second, second_error = run(other)

    if (first_error is None) != (second_error is None):
        raised, quiet = (first_error, "second") if first_error else (second_error, "first")
        raise AssertionError(
            f"{prefix}one raised {type(raised).__name__} and the {quiet} did not. "
            f"A catchable exception is itself an answer."
        )
    if first_error is not None and second_error is not None:
        if type(first_error) is not type(second_error):
            raise AssertionError(
                f"{prefix}they raised different exceptions, "
                f"{type(first_error).__name__} and {type(second_error).__name__}."
            )
        return

    if len(first) != len(second):
        raise AssertionError(f"{prefix}{len(first)} results against {len(second)}.")

    for label, left, right in (
        ("texts", [r.text for r in first], [r.text for r in second]),
        ("ids", [r.id for r in first], [r.id for r in second]),
    ):
        if left != right:
            raise AssertionError(f"{prefix}the {label} differ.\n  {left}\n  {right}")

    for field in _OBSERVABLE:
        here, there = getattr(first, field, None), getattr(second, field, None)
        if here != there:
            raise AssertionError(f"{prefix}{field} is {here!r} against {there!r}.")

    if score_tolerance is None:
        return

    for rank, (left_hit, right_hit) in enumerate(zip(first, second)):
        mine = getattr(left_hit, "score", None)
        theirs = getattr(right_hit, "score", None)
        if mine is None or theirs is None:
            continue
        spread = abs(mine - theirs)
        if spread > score_tolerance * max(abs(mine), abs(theirs), 1e-12):
            raise AssertionError(
                f"{prefix}result {rank} scores {mine!r} against {theirs!r}, a spread of "
                f"{spread:.4g} against a tolerance of {score_tolerance:.0%}. Two ways to "
                f"read that. A sparse score is a corpus statistic, so it moves with "
                f"documents this principal cannot see, and then the score is the leak. "
                f"Or your embedding is noisier across batches than the default allows, "
                f"and then widen score_tolerance."
            )
