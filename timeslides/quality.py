"""Data-quality checks applied at ingestion, before anything reaches a chart.

A waterfall of provenance-labelled points is only as good as the claim that
each point is one independent report. Two records from the same source at the
same epoch break that claim, and they break it invisibly: they overplot, so the
chart looks the same whether a provider sent one report or five.

Two distinct things get conflated as "duplicates", and they matter differently:

  duplicate  Same source, same epoch, same values. Redundant transport. The
             chart is unaffected; the point is simply reported more than once.
             Collapsed, and counted, because a count of them says something
             about the feed.

  conflict   Same source, same epoch, DIFFERENT values. One of them is plotted
             and the other is not, so the chart depends on which is picked.
             This is the one worth interrupting somebody about, and it is
             surfaced in the report rather than only in the pod log.

Which record wins a conflict is decided here rather than left to arrival order,
so the same feed produces the same chart on every run.
"""

from __future__ import annotations

from .audit import event


def _created(rec) -> str:
    """The record's own creation stamp, if the feed gave one.

    A conflict is resolved in favour of the most recently created record, which
    is the one a provider that reissued a correction would want plotted. When
    the feed carries no stamp there is nothing to order by, so first seen wins
    and the finding says the choice was arbitrary.
    """
    return getattr(rec, "created", "") or ""


def dedupe(records: list, source_label: str, sat_no=None) -> tuple:
    """(kept, finding). Collapse duplicates, resolve conflicts, count both.

    Every record given here must be from ONE source. Two sources reporting the
    same epoch is not duplication, it is two independent reports, which is the
    whole point of the plot. Use dedupe_mixed for a batch that can hold
    several originators.

    `kept` preserves the input order of first appearance, so a caller that
    sorted by epoch stays sorted. `finding` is None when the batch was clean,
    which is the case worth keeping cheap.
    """
    by_identity, order = {}, []
    duplicates, conflicts = 0, []
    for rec in records:
        key = rec.epoch
        identity = rec.identity()
        seen = by_identity.get(key)
        if seen is None:
            by_identity[key] = rec
            order.append(key)
            continue
        if seen.identity() == identity:
            duplicates += 1
            continue
        # Same epoch, different values. Decide it the same way every run.
        #
        # `arbitrary` says whether the stamps actually decided it, not merely
        # whether a stamp was present. Two records carrying the SAME stamp are
        # as undecidable as two carrying none, and reporting that as "resolved
        # by the feed's creation stamp" would tell an analyst the pick meant
        # something when it did not.
        winner, loser = seen, rec
        mine, theirs = _created(rec), _created(seen)
        arbitrary = mine == theirs
        if not arbitrary and mine > theirs:
            winner, loser = rec, seen
        by_identity[key] = winner
        conflicts.append({
            "epoch": key.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kept": _created(winner) or "first seen",
            "dropped": _created(loser) or "later arrival",
            "arbitrary": arbitrary,
        })

    kept = [by_identity[key] for key in order]
    if not duplicates and not conflicts:
        return kept, None

    finding = {
        "source": source_label,
        "satNo": sat_no,
        "received": len(records),
        "plotted": len(kept),
        "duplicates": duplicates,
        "conflicts": conflicts,
    }
    event("quality.duplicates", source=source_label, sat_no=sat_no,
          received=len(records), plotted=len(kept), duplicates=duplicates,
          conflicts=len(conflicts),
          arbitrary=sum(1 for c in conflicts if c["arbitrary"]))
    return kept, finding


def summarise(findings: list) -> dict:
    """Totals across a run, for the report's data-quality band."""
    live = [f for f in findings if f]
    return {
        "sources": len(live),
        "duplicates": sum(f["duplicates"] for f in live),
        "conflicts": sum(len(f["conflicts"]) for f in live),
        "arbitrary": sum(1 for f in live for c in f["conflicts"] if c["arbitrary"]),
        "findings": live,
    }


def dedupe_mixed(records: list, fallback_label: str, sat_no=None) -> tuple:
    """(kept, findings). For a batch that can hold several originators.

    The element-set query is not filtered by source, so whatever the tenant
    holds on /udl/elset comes back and one batch can carry records from several
    originators. Two of them at the same epoch is not duplication: they are two
    independent element sets for the same object, and plotting both is correct.
    So each source is deduplicated against itself and never against another.

    Records with no source of their own are grouped together under
    `fallback_label`, which is the honest reading: the feed did not say, so
    they cannot be told apart.
    """
    groups, order = {}, []
    for rec in records:
        label = (rec.source or "").strip() or fallback_label
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(rec)

    kept, findings = [], []
    for label in order:
        group_kept, finding = dedupe(groups[label], label, sat_no)
        kept.extend(group_kept)
        if finding:
            findings.append(finding)
    kept.sort(key=lambda r: r.epoch)
    return kept, findings
