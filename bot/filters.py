"""Job filtering, scoring and the review-prompt selection parser."""

from __future__ import annotations

import unicodedata

#: Statuses that mean "do not touch this job again" (see SPEC.md decision 8).
TERMINAL_STATUSES = frozenset({"applied", "skipped", "failed", "chatted"})


def _lower_list(values) -> list[str]:
    """Normalise a config list of keywords to non-empty lowercase strings."""
    if not values:
        return []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _known_jobkeys(store) -> set[str]:
    """Terminal job keys from the store; tolerant of simple test doubles."""
    if store is None:
        return set()
    try:
        known = store.known_jobs(terminal_only=True)
    except TypeError:  # a store double without the keyword argument
        known = store.known_jobs()
    except Exception:
        return set()
    if isinstance(known, dict):
        return {str(key) for key in known}
    return {str(key) for key in (known or ())}


def filter_jobs(jobs: list[dict], cfg: dict, store) -> list[dict]:
    """Apply the configured criteria and return ranked matches.

    Order of operations: terminal-status dedupe, keyword exclusion (wins over
    inclusion), keyword inclusion, salary floor, locations, Indeed-Apply-only,
    then deterministic scoring. Input dicts are not mutated.
    """
    if not jobs:
        return []

    rules = (cfg or {}).get("filters") or {}
    include = _lower_list(rules.get("keywords_include"))
    exclude = _lower_list(rules.get("keywords_exclude"))
    locations = _lower_list(rules.get("locations"))
    try:
        min_salary = float(rules.get("min_salary_hkd", 0) or 0)
    except (TypeError, ValueError):
        min_salary = 0.0
    include_unknown = bool(rules.get("include_unknown_salary", False))
    apply_only = bool(rules.get("indeed_apply_only", True))

    known = _known_jobkeys(store)
    scored: list[tuple[int, int, dict]] = []

    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        jobkey = str(job.get("jobkey") or "")
        if jobkey and jobkey in known:
            continue

        title = str(job.get("title") or "").lower()
        company = str(job.get("company") or "").lower()
        location = str(job.get("location") or "").lower()
        snippet = str(job.get("snippet") or "").lower()
        haystack = " ".join((title, company, location, snippet))

        # Exclusion always wins.
        if exclude and any(word in haystack for word in exclude):
            continue
        if include and not any(word in haystack for word in include):
            continue

        if apply_only:
            if not job.get("indeed_applyable"):
                continue
            if job.get("redirectToThirdPartySite"):
                continue

        if min_salary > 0:
            salary = job.get("salary_min") or 0
            try:
                salary = float(salary)
            except (TypeError, ValueError):
                salary = 0.0
            if salary <= 0:
                if not include_unknown:
                    continue
            elif salary < min_salary:
                continue

        location_match = bool(locations) and any(word in location for word in locations)
        if locations and not location_match:
            continue

        score = 0
        for word in include:
            if word in title:
                score += 3
            if word in company or word in location:
                score += 2
            if word in snippet:
                score += 1
        if location_match:
            score += 1
        if min_salary > 0 and (job.get("salary_min") or 0) >= min_salary:
            score += 1

        enriched = dict(job)
        enriched["score"] = score
        scored.append((score, index, enriched))

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [job for _score, _index, job in scored]


def parse_selection(text: str, count: int) -> list[int]:
    """Parse the review prompt into sorted, deduped 1-based indices.

    Accepts ``1,3,5``, ``(1,2)``, ``1-5``, ``1,3-5`` and ``all``. An empty
    answer, ``q``/``quit`` or anything unparseable/out of range yields ``[]``
    (nothing is applied); ranges that run past *count* are clamped.
    """
    if count <= 0:
        return []

    raw = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    raw = raw.replace(" to ", "-")
    tokens = [
        token.strip()
        for token in raw.replace("，", ",").replace("、", ",").replace(";", ",").split(",")
    ]
    # Quit always wins, even inside a mixed answer such as "q,1,2".
    if any(token in ("q", "quit") for token in tokens):
        return []
    if raw in ("all", "a", "*", "全部"):
        return list(range(1, count + 1))

    numbers: set[int] = set()
    for token in tokens:
        chunk = token.strip("()[]{}<> \t")
        if not chunk or chunk.startswith("-"):
            continue
        if "-" in chunk:
            bounds = [part.strip() for part in chunk.split("-") if part.strip()]
            if len(bounds) != 2 or not all(part.isdigit() for part in bounds):
                continue
            first, last = int(bounds[0]), int(bounds[1])
            if first > last:
                first, last = last, first
            first, last = max(1, first), min(count, last)
            if first <= last:
                numbers.update(range(first, last + 1))
        elif chunk.isdigit():
            number = int(chunk)
            if 1 <= number <= count:
                numbers.add(number)

    return sorted(numbers)
