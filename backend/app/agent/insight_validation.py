"""Domain-agnostic grounding validation for LLM analytical narratives."""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.dashboard_insight_facts import (
    DashboardInsightFacts,
    InsightFactStatus,
    _as_number,
    _format_number,
)


class GroundingOutcome(str, Enum):
    VALID = "validated"
    REGENERATED = "regenerated"
    FALLBACK = "fallback"
    INSUFFICIENT_DATA = "insufficient_data"
    ERROR = "error"


class NumericClaim(BaseModel):
    """A numeric value tied to semantic labels for association checks."""

    value: float
    formatted: str = ""
    metric: str | None = None
    dimension: str | None = None
    dimension_value: str | None = None
    unit: str | None = None
    aliases: list[str] = Field(default_factory=list)


class RankingFact(BaseModel):
    dimension: str
    ordered_labels: list[str] = Field(default_factory=list)
    ordered_values: list[float] = Field(default_factory=list)


class TrendDirectionFact(BaseModel):
    label: str
    direction: Literal["up", "down", "flat", "unknown"]
    points: int = 0
    change_pct: float | None = None


class ComparisonDirectionFact(BaseModel):
    label: str
    direction: Literal["up", "down", "flat", "unknown"]
    change_pct: float | None = None
    left_label: str | None = None
    right_label: str | None = None


class EntityLeaderFact(BaseModel):
    dimension: str
    leader_label: str
    leader_value: float
    runner_up_label: str | None = None


class GroundingContext(BaseModel):
    status: InsightFactStatus = InsightFactStatus.INSUFFICIENT_DATA
    insufficient_reason: str | None = None
    numeric_claims: list[NumericClaim] = Field(default_factory=list)
    rankings: list[RankingFact] = Field(default_factory=list)
    trends: list[TrendDirectionFact] = Field(default_factory=list)
    comparisons: list[ComparisonDirectionFact] = Field(default_factory=list)
    entity_leaders: list[EntityLeaderFact] = Field(default_factory=list)
    active_filters: list[dict[str, Any]] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    tenant_id: str = ""
    connection_id: str = ""
    allows_causation: bool = False


_NUMBER_RE = re.compile(
    r"(?<![\w/])"
    r"(?:"
    r"[$€£₹]\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
    # K/M/B-suffixed numbers (e.g. "16.86B") MUST be tried before the plain
    # decimal-number branch below. Regex alternation takes the first branch
    # that leads to an overall match, not the longest one — for "16.86B",
    # the plain-number branch below can match just "16" by backtracking off
    # its optional decimal group (since "." isn't a word character, the
    # trailing negative lookahead is satisfied by that shorter match), which
    # steals the match before this branch ever runs. That silently split
    # every abbreviated number the app formats ("16.86B" -> "16" + "86B")
    # into two tokens, each of which then fails to match any known value —
    # which made the grounding validator reject essentially every
    # LLM-written narrative that used the app's own number formatting,
    # forcing a permanent fallback to the terse deterministic bullet list.
    r"|-?\d+(?:\.\d+)?\s*[KMBkmb]\b"
    r"|-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?"
    r")"
    r"(?![\w/])"
)

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_ORDINAL_RE = re.compile(r"\b\d+(st|nd|rd|th)\b", re.IGNORECASE)

_INCREASE_RE = re.compile(
    r"\b(increased?|grew|growth|higher|up|rose|gained|improved|expanded|climbed)\b",
    re.IGNORECASE,
)
_DECREASE_RE = re.compile(
    r"\b(decreased?|declined?|lower|down|fell|dropped|reduced|shrunk|contracted)\b",
    re.IGNORECASE,
)
_RANK_FIRST_RE = re.compile(
    r"\b(highest|largest|biggest|top|first|best[- ]performing|won|leading|most)\b",
    re.IGNORECASE,
)
_RANK_LAST_RE = re.compile(
    r"\b(lowest|smallest|bottom|last|worst[- ]performing|least|fewest)\b",
    re.IGNORECASE,
)
_CAUSATION_RE = re.compile(
    r"\b(because|due to|caused by|as a result of|thanks to|driven by|resulting from)\b",
    re.IGNORECASE,
)

_COMPARISON_PCT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*%\s*(?:higher|lower|more|less|increase|decrease|growth|decline)?",
    re.IGNORECASE,
)


def _tokenize(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1 or (len(t) == 1 and t.isalpha())
    }


def _aliases_from_label(label: str) -> list[str]:
    base = label.strip().lower()
    if not base:
        return []
    parts = re.split(r"[\s_/\-]+", base)
    return list({base, *parts})


def parse_numeric_token(token: str) -> float | None:
    raw = (token or "").strip()
    if not raw:
        return None
    is_pct = raw.endswith("%")
    cleaned = raw.lstrip("$€£₹").strip().rstrip("%").replace(",", "")
    km_match = re.match(r"^(-?\d+(?:\.\d+)?)\s*([KMBkmb])$", cleaned)
    if km_match:
        num = float(km_match.group(1))
        suffix = km_match.group(2).upper()
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
        val = num * mult
        return val if math.isfinite(val) else None
    val = _as_number(cleaned)
    if val is None:
        return None
    if is_pct:
        return val
    return val


def numeric_matches_allowed(value: float, allowed: set[float]) -> bool:
    if value in allowed:
        return True
    for ref in allowed:
        if ref == 0:
            if abs(value) < 0.01:
                return True
        elif abs(value - ref) <= max(0.05, abs(ref) * 0.02):
            return True
        elif abs(value - ref * 100) <= max(0.05, abs(ref * 100) * 0.02):
            return True
        elif ref != 0 and abs(value / 100 - ref) <= max(0.0005, abs(ref) * 0.02):
            return True
    return False


def collect_allowed_numeric_values(ctx: GroundingContext) -> set[float]:
    allowed: set[float] = set()

    def add(v: float | None) -> None:
        if v is None or not math.isfinite(v):
            return
        allowed.add(v)
        allowed.add(round(v, 1))
        allowed.add(round(v, 2))
        allowed.add(abs(v))

    for claim in ctx.numeric_claims:
        add(claim.value)
    for trend in ctx.trends:
        add(trend.change_pct)
        add(abs(trend.change_pct) if trend.change_pct is not None else None)
    for comp in ctx.comparisons:
        add(comp.change_pct)
        add(abs(comp.change_pct) if comp.change_pct is not None else None)
    for rank in ctx.rankings:
        for val in rank.ordered_values:
            add(val)
    for leader in ctx.entity_leaders:
        add(leader.leader_value)
    return allowed


def _should_skip_numeric_token(token: str, val: float, content: str, span: tuple[int, int]) -> bool:
    if _ORDINAL_RE.search(token):
        return True
    start, end = span
    window = content[max(0, start - 12) : min(len(content), end + 12)]
    if _DATE_RE.search(window):
        return True
    if not token.endswith("%") and 1900 <= abs(val) <= 2100 and float(val).is_integer():
        if _YEAR_RE.search(token) or re.fullmatch(r"\d{4}", token.strip("%")):
            return True
    if not token.endswith("%") and abs(val) <= 3 and "." not in token:
        return True
    return False


def _known_label_spans(content: str, ctx: "GroundingContext") -> list[tuple[int, int]]:
    """Where known entity labels that contain digits occur in the text."""
    labels: set[str] = set()
    for claim in ctx.numeric_claims:
        if claim.dimension_value:
            labels.add(claim.dimension_value)
            labels.update(part.strip() for part in claim.dimension_value.split(" / "))
    for leader in ctx.entity_leaders:
        labels.update(filter(None, (leader.leader_label, leader.runner_up_label)))
    for ranking in ctx.rankings:
        labels.update(ranking.ordered_labels)
    spans: list[tuple[int, int]] = []
    lowered = (content or "").lower()
    for label in labels:
        label = (label or "").strip().lower()
        if not label or not re.search(r"\d", label):
            continue
        for m in re.finditer(re.escape(label), lowered):
            spans.append(m.span())
    return spans


def _claim_matches_context(claim: NumericClaim, context_tokens: set[str]) -> bool:
    if not claim.aliases:
        return True
    alias_tokens = set()
    for alias in claim.aliases:
        alias_tokens.update(_tokenize(alias))
    return bool(context_tokens & alias_tokens)


def _strict_context_tokens(content: str, start: int, end: int) -> set[str]:
    left = content[max(0, start - 12) : start]
    right = content[end : min(len(content), end + 12)]
    # Clause boundaries, not just line breaks — an LLM routinely states two
    # unrelated facts on one bulleted line ("Total Gross Profit is 44.06B;
    # Average Margin Percent is 6.62."). Without also cutting at ". "/"; ",
    # the window bleeds past the end of the first fact into the second
    # one's own number/label, and wrongly treats it as context for this
    # number. These can't clip the number's own decimal point — `left`/
    # `right` start after / end before the number's own span.
    for boundary in ("\n", ". ", "; "):
        if boundary in left:
            left = left[left.rfind(boundary) + len(boundary) :]
    for boundary in ("\n", ". ", "; "):
        if boundary in right:
            right = right[: right.find(boundary)]
    return _tokenize(left + right)


def _label_tokens(content: str, start: int) -> set[str]:
    """Words of the label a reader attaches to the number at ``start``: the
    text before it back to the previous number or clause boundary ("**Budgeted
    Sales Quantity:** 12.17M"; "... below Budgeted Sales Quantity (49.72M)")."""
    left = content[max(0, start - 80) : start]
    for boundary in ("\n", ". ", "; "):
        if boundary in left:
            left = left[left.rfind(boundary) + len(boundary) :]
    previous_number = None
    for previous_number in _NUMBER_RE.finditer(left):
        pass
    if previous_number is not None:
        left = left[previous_number.end() :]
    return _tokenize(left)


def _numeric_semantically_valid(
    val: float,
    context_tokens: set[str],
    claims: list[NumericClaim],
    *,
    strict_tokens: set[str] | None = None,
    label_tokens: set[str] | None = None,
) -> bool:
    conflict_tokens = strict_tokens if strict_tokens is not None else context_tokens
    entity_tokens = conflict_tokens | (label_tokens or set())
    matching = [c for c in claims if numeric_matches_allowed(val, {c.value})]
    if not matching:
        return False
    matched_entities = {
        c.dimension_value.strip().lower() for c in matching if c.dimension_value
    }
    # Words of the entity this number belongs to. Another entity is only
    # "named" next to the number by words that set it apart: "Actual Sales
    # Quantity 12.17M" must not read as a claim about "Budgeted Sales
    # Quantity" just because both names contain "sales quantity" — which
    # rejected every narrative comparing actual vs target, 2024 vs 2025...
    matched_entity_tokens: set[str] = set()
    for entity in matched_entities:
        matched_entity_tokens |= _tokenize(entity)
    # A category-total claim (e.g. "Gross Profit by Customer Segment" as a
    # whole) has no dimension_value, but its `metric` is literally the same
    # string as `dimension` on that category's own per-entry claims — that
    # structural link is what distinguishes a genuine category total from
    # an ordinary standalone KPI claim (e.g. "Orders", "Revenue") which also
    # has no dimension_value but ISN'T a category total and must keep the
    # full strict check below (an adversarial narrative attributing the
    # wrong KPI's number to "Orders" must still be rejected).
    category_titles = {c.dimension for c in claims if c.dimension}

    for claim in claims:
        if not claim.aliases:
            continue
        if (
            not claim.dimension_value
            and claim.metric
            and claim.metric in category_titles
            and not any(c is claim for c in matching)
        ):
            # `claim` is that category's own total. Its aliases (the
            # category title's words: "gross", "profit", "segment"...) are
            # a subset of what EVERY entry in that category also carries as
            # aliases, so they inevitably overlap the text around any
            # per-entry number in the same category — e.g. "Key generated
            # 16.86B gross profit" overlaps the *category's* aliases even
            # though the number is Key's entry, not the total; "Total Net
            # Sales Value was 888.10B" can likewise collide with an
            # unrelated category's own grand total that happens to land
            # within tolerance of the same number. `val` already passed
            # the real check — it equals a genuine fact (`matching` above,
            # computed from the actual numbers, not from words) — so a
            # same-magnitude category total's coincidental word overlap
            # must not veto that.
            continue
        if (
            claim.dimension_value
            and claim.dimension_value.strip().lower() in matched_entities
            and not any(c is claim for c in matching)
        ):
            # Same entity as an already-confirmed match, but a different
            # numeric *role* for it — e.g. "Key" has both a raw value
            # (16.86B) and a share-of-total percentage (38.3%) as separate
            # claims, both tagged with the same entity name and aliases.
            # Checking the value token would otherwise also pick up the
            # percentage claim for "Key" (same alias, same dimension_value)
            # and demand 16.86B == 38.3, which is nonsensical — they're
            # different facts about the same entity, not competing values
            # for the same fact. Keyed by entity alone: the same holds across
            # measures ("Plant 2" has a count, a quantity and a value — each
            # is its own fact about that plant, none contradicts the others).
            continue
        aliases: set[str] = set()
        for alias in claim.aliases:
            aliases.update(_tokenize(alias))
        if not (conflict_tokens & aliases):
            continue
        if claim.dimension_value:
            distinctive = _tokenize(claim.dimension_value) - matched_entity_tokens
            if not (entity_tokens & distinctive):
                continue
        if not numeric_matches_allowed(val, {claim.value}):
            return False

    if len(matching) == 1:
        return True

    labeled_matching = [c for c in matching if c.aliases]
    if not labeled_matching:
        return True
    return any(_claim_matches_context(c, context_tokens) for c in labeled_matching)


def _validate_trend_language(content: str, ctx: GroundingContext) -> bool:
    lower = content.lower()
    has_increase = bool(_INCREASE_RE.search(lower))
    has_decrease = bool(_DECREASE_RE.search(lower))
    if not has_increase and not has_decrease:
        return True
    if not ctx.trends:
        return True
    for trend in ctx.trends:
        if trend.points < 2 and (has_increase or has_decrease):
            return False
        if trend.direction == "up" and has_decrease and not has_increase:
            return False
        if trend.direction == "down" and has_increase and not has_decrease:
            return False
        if trend.direction == "flat" and (has_increase or has_decrease):
            return False
    return True


def _entity_label_mentioned(label: str, content: str, content_tokens: set[str]) -> bool:
    if label.lower() in (content or "").lower():
        return True
    label_tokens = _tokenize(label)
    return bool(label_tokens) and label_tokens <= content_tokens


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+|;\s+")


def _validate_ranking_language(content: str, ctx: GroundingContext) -> bool:
    # A ranking word is a claim about the sentence it sits in: "X has the
    # most issues. Y follows with 87." must not be read as ranking Y first.
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(content or "") if part.strip()]
    if len(sentences) > 1:
        return all(_validate_ranking_sentence(part, ctx) for part in sentences)
    return _validate_ranking_sentence(content, ctx)


def _validate_ranking_sentence(content: str, ctx: GroundingContext) -> bool:
    lower = content.lower()
    if not _RANK_FIRST_RE.search(lower) and not _RANK_LAST_RE.search(lower):
        return True
    if not ctx.entity_leaders and not ctx.rankings:
        return False
    content_tokens = _tokenize(content)
    if ctx.rankings:
        for ranking in ctx.rankings:
            if not ranking.ordered_labels:
                continue
            if _RANK_FIRST_RE.search(lower):
                for label in ranking.ordered_labels[1:]:
                    if _entity_label_mentioned(label, content, content_tokens):
                        return False
                top = ranking.ordered_labels[0]
                if _entity_label_mentioned(top, content, content_tokens):
                    return True
            if _RANK_LAST_RE.search(lower):
                bottom = ranking.ordered_labels[-1]
                for label in ranking.ordered_labels[:-1]:
                    if _entity_label_mentioned(label, content, content_tokens):
                        return False
                if _entity_label_mentioned(bottom, content, content_tokens):
                    return True
    if ctx.entity_leaders and _RANK_FIRST_RE.search(lower):
        best = max(ctx.entity_leaders, key=lambda e: e.leader_value)
        mentioned = [
            leader
            for leader in ctx.entity_leaders
            if _entity_label_mentioned(leader.leader_label, content, content_tokens)
        ]
        if mentioned:
            return all(leader.leader_value >= best.leader_value for leader in mentioned)
        return False
    if ctx.entity_leaders and _RANK_LAST_RE.search(lower):
        worst = min(ctx.entity_leaders, key=lambda e: e.leader_value)
        mentioned = [
            leader
            for leader in ctx.entity_leaders
            if _entity_label_mentioned(leader.leader_label, content, content_tokens)
        ]
        if mentioned:
            return all(leader.leader_value <= worst.leader_value for leader in mentioned)
        return False
    if _RANK_FIRST_RE.search(lower) or _RANK_LAST_RE.search(lower):
        return bool(ctx.entity_leaders or ctx.rankings)
    return True


def _validate_comparison_language(content: str, ctx: GroundingContext) -> bool:
    for match in _COMPARISON_PCT_RE.finditer(content or ""):
        pct = _as_number(match.group(1))
        if pct is None:
            continue
        window = content[max(0, match.start() - 50) : match.end() + 50].lower()
        if _DECREASE_RE.search(window) or "lower" in window or "less" in window:
            expected_sign = -1
        elif _INCREASE_RE.search(window) or "higher" in window or "more" in window:
            expected_sign = 1
        else:
            continue
        matched = False
        for comp in ctx.comparisons:
            if comp.change_pct is None:
                continue
            if numeric_matches_allowed(abs(pct), {abs(comp.change_pct)}):
                if expected_sign > 0 and comp.direction in {"up", "flat"}:
                    matched = True
                if expected_sign < 0 and comp.direction in {"down", "flat"}:
                    matched = True
        for trend in ctx.trends:
            if trend.change_pct is None:
                continue
            if numeric_matches_allowed(abs(pct), {abs(trend.change_pct)}):
                if expected_sign > 0 and trend.direction == "up":
                    matched = True
                if expected_sign < 0 and trend.direction == "down":
                    matched = True
        if (ctx.comparisons or ctx.trends) and not matched:
            return False
    lower = content.lower()
    if (_INCREASE_RE.search(lower) or _DECREASE_RE.search(lower)) and ctx.comparisons:
        for comp in ctx.comparisons:
            if comp.direction == "up" and _DECREASE_RE.search(lower) and not _INCREASE_RE.search(lower):
                return False
            if comp.direction == "down" and _INCREASE_RE.search(lower) and not _DECREASE_RE.search(lower):
                return False
    return True


def _validate_causation_language(content: str, ctx: GroundingContext) -> bool:
    if not _CAUSATION_RE.search(content or ""):
        return True
    return ctx.allows_causation


def validate_grounded_narrative(content: str, ctx: GroundingContext) -> bool:
    """Return True when narrative is consistent with deterministic facts."""
    if ctx.status == InsightFactStatus.INSUFFICIENT_DATA:
        return True
    if not (content or "").strip():
        return False

    if not _validate_causation_language(content, ctx):
        return False
    if not _validate_trend_language(content, ctx):
        return False
    if not _validate_ranking_language(content, ctx):
        return False
    if not _validate_comparison_language(content, ctx):
        return False

    allowed = collect_allowed_numeric_values(ctx)
    max_allowed = max((abs(v) for v in allowed), default=0.0)
    claims = ctx.numeric_claims
    label_spans = _known_label_spans(content, ctx)

    for match in _NUMBER_RE.finditer(content or ""):
        token = match.group(0)
        val = parse_numeric_token(token)
        if val is None:
            continue
        span = match.span()
        if _should_skip_numeric_token(token, val, content, span):
            continue
        if any(s <= span[0] and span[1] <= e for s, e in label_spans):
            # Part of an entity name ("Industrial Customer 045", "Plant 2"),
            # not a claimed value.
            continue
        context_tokens = _tokenize(content[max(0, span[0] - 40) : span[1] + 40])
        strict_tokens = _strict_context_tokens(content, span[0], span[1])
        if claims:
            if not _numeric_semantically_valid(
                val,
                context_tokens,
                claims,
                strict_tokens=strict_tokens,
                label_tokens=_label_tokens(content, span[0]),
            ):
                return False
        elif max_allowed > 0:
            if abs(val) > max_allowed * 1.05 and not numeric_matches_allowed(val, allowed):
                return False
            if not numeric_matches_allowed(val, allowed):
                return False
        elif abs(val) > 3:
            return False
    return True


def render_deterministic_grounded_answer(ctx: GroundingContext) -> str:
    if ctx.status == InsightFactStatus.INSUFFICIENT_DATA:
        return ctx.insufficient_reason or "There isn't enough data to answer that question."
    if ctx.highlights:
        return "\n".join(f"- {line}" for line in ctx.highlights[:6])
    lines: list[str] = []
    for claim in ctx.numeric_claims[:5]:
        label = claim.metric or claim.dimension_value or claim.dimension or "Value"
        formatted = claim.formatted or _format_number(claim.value)
        if claim.unit:
            lines.append(f"- {label}: {formatted} {claim.unit}".strip())
        else:
            lines.append(f"- {label}: {formatted}")
    for leader in ctx.entity_leaders[:3]:
        lines.append(
            f"- {leader.leader_label} had the highest {leader.dimension} "
            f"({_format_number(leader.leader_value)})."
        )
    return "\n".join(lines) if lines else "There isn't enough data to answer that question."


def grounding_context_from_dashboard_facts(facts: DashboardInsightFacts) -> GroundingContext:
    claims: list[NumericClaim] = []
    rankings: list[RankingFact] = []
    trends: list[TrendDirectionFact] = []
    comparisons: list[ComparisonDirectionFact] = []
    leaders: list[EntityLeaderFact] = []

    for kpi in facts.kpis:
        claims.append(
            NumericClaim(
                value=kpi.value or 0.0,
                formatted=kpi.formatted_value,
                metric=kpi.title,
                aliases=_aliases_from_label(kpi.title),
            )
        )

    for cat in facts.categories:
        if cat.total is not None:
            claims.append(
                NumericClaim(
                    value=cat.total,
                    formatted=_format_number(cat.total),
                    metric=cat.title,
                    aliases=_aliases_from_label(cat.title),
                )
            )
        labels: list[str] = []
        values: list[float] = []
        # top and bottom overlap whenever a category has few enough entries
        # that "top 3" and "bottom 3" are the same entries (e.g. exactly 3
        # categories total) — without dedup, every one of those entries
        # gets a duplicate NumericClaim, which defeats the narrator
        # validator's "exactly one claim matches this number" fast path
        # and forces it through a much weaker context-window check instead.
        seen_entry_labels: set[str] = set()
        deduped_entries = []
        for entry in cat.top + cat.bottom:
            if entry.label in seen_entry_labels:
                continue
            seen_entry_labels.add(entry.label)
            deduped_entries.append(entry)
        for entry in deduped_entries:
            labels.append(entry.label)
            values.append(entry.value)
            claims.append(
                NumericClaim(
                    value=entry.value,
                    formatted=_format_number(entry.value),
                    dimension=cat.title,
                    dimension_value=entry.label,
                    aliases=_aliases_from_label(entry.label) + _aliases_from_label(cat.title),
                )
            )
            if entry.share_pct is not None:
                claims.append(
                    NumericClaim(
                        value=entry.share_pct,
                        formatted=f"{entry.share_pct:.1f}%",
                        dimension=cat.title,
                        dimension_value=entry.label,
                        unit="%",
                        aliases=_aliases_from_label(entry.label),
                    )
                )
        if labels:
            rankings.append(RankingFact(dimension=cat.title, ordered_labels=labels, ordered_values=values))
            if cat.top:
                top = cat.top[0]
                runner = cat.top[1].label if len(cat.top) > 1 else None
                leaders.append(
                    EntityLeaderFact(
                        dimension=cat.title,
                        leader_label=top.label,
                        leader_value=top.value,
                        runner_up_label=runner,
                    )
                )

    for trend in facts.trends:
        trends.append(
            TrendDirectionFact(
                label=trend.title,
                direction=trend.direction,
                points=trend.points,
                change_pct=trend.change_pct,
            )
        )
        for field, label in (
            (trend.first_value, "first"),
            (trend.last_value, "last"),
            (trend.change_abs, "change"),
            (trend.change_pct, "change_pct"),
        ):
            if field is not None:
                claims.append(
                    NumericClaim(
                        value=float(field),
                        formatted=_format_number(float(field)),
                        metric=trend.title,
                        aliases=_aliases_from_label(trend.title) + [label],
                    )
                )
        if trend.change_pct is not None:
            comparisons.append(
                ComparisonDirectionFact(
                    label=trend.title,
                    direction=trend.direction,
                    change_pct=trend.change_pct,
                )
            )
            claims.append(
                NumericClaim(
                    value=float(trend.change_pct),
                    formatted=f"{trend.change_pct:.1f}%",
                    metric=trend.title,
                    unit="%",
                    aliases=_aliases_from_label(trend.title)
                    + ["increased", "decreased", "growth", "change", "percent"],
                )
            )

    for cmp in facts.comparisons:
        if cmp.ratio is not None:
            claims.append(
                NumericClaim(
                    value=cmp.ratio,
                    formatted=f"{cmp.ratio:.2f}",
                    metric=cmp.label,
                    aliases=_aliases_from_label(cmp.left) + _aliases_from_label(cmp.right),
                )
            )

    return GroundingContext(
        status=facts.status,
        insufficient_reason=facts.insufficient_reason,
        numeric_claims=claims,
        rankings=rankings,
        trends=trends,
        comparisons=comparisons,
        entity_leaders=leaders,
        active_filters=facts.active_filters,
        highlights=facts.ranked_highlights,
    )


def validate_narrative_numbers(content: str, facts: DashboardInsightFacts) -> bool:
    """Backward-compatible dashboard numeric validation."""
    ctx = grounding_context_from_dashboard_facts(facts)
    return validate_grounded_narrative(content, ctx)
