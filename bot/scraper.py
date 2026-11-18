"""Scrape Indeed HK job cards from the embedded mosaic JSON (no public API)."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import quote_plus

from .utils import (
    ChallengeDetected,
    check_page,
    parse_salary_min,
    setup_logging,
    wait_for_challenge,
)

DEFAULT_BASE_URL = "https://hk.indeed.com"

#: Exact location of the job-card array inside the page's embedded JSON.
_MOSIC_MARKER = 'window.mosaic.providerData["mosaic-provider-jobcards"]='

# Indeed HK sometimes clears window.mosaic.providerData after rendering, but
# the SSR payload remains in the #mosaic-data script tag. Read it directly with
# a brace-balanced JSON slice instead of trusting the global object.
MOSAIC_READ_JS = """() => {
  try {
    const marker = 'window.mosaic.providerData["mosaic-provider-jobcards"]=';
    const el = document.getElementById('mosaic-data');
    if (!el) return null;
    const text = el.textContent || '';
    const at = text.indexOf(marker);
    if (at < 0) return null;
    const start = text.indexOf('{', at + marker.length);
    if (start < 0) return null;
    let depth = 0, quoted = false, escaped = false, end = -1;
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (escaped) { escaped = false; continue; }
        if (ch === '\\\\') { escaped = true; continue; }
        if (ch === '"') quoted = false;
        continue;
      }
      if (ch === '"') { quoted = true; continue; }
      if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) { end = i + 1; break; }
      }
    }
    if (end < 0) return null;
    const payload = JSON.parse(text.slice(start, end));
    const model = payload.metaData && payload.metaData.mosaicProviderJobCardsModel;
    return model && Array.isArray(model.results) ? model.results : null;
  } catch (e) {
    return null;
  }
}"""

MOSAIC_WAIT_JS = """() => {
  const el = document.getElementById('mosaic-data');
  return !!(el && (el.textContent || '').includes(
    'window.mosaic.providerData["mosaic-provider-jobcards"]='));
}"""

_TAG_RE = re.compile(r"<[^>]+>")
_JK_RE = re.compile(r"[?&]jk=([^&]+)")
_WS_RE = re.compile(r"\s+")


def build_search_url(
    base_url: str,
    query: str,
    location: str,
    start: int = 0,
    fromage: int = 7,
    sort: str = "date",
) -> str:
    """Build the Indeed HK search URL for one page of results."""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    return (
        f"{base}/jobs?q={quote_plus(str(query or ''))}"
        f"&l={quote_plus(str(location or ''))}"
        f"&start={int(start)}&fromage={int(fromage)}&sort={quote_plus(str(sort or 'date'))}"
    )


def _clean_text(value) -> str:
    """Turn a possibly-HTML snippet into plain single-line text."""
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("label") or ""
    text = str(value)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _first(item: dict, *keys):
    """Return the first present, non-empty value among *keys*."""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flag(value) -> bool | None:
    """Coerce a JSON flag to bool, or ``None`` when it is absent/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return None


def _mosaic_results(page):
    """Read the job-card array; ``None`` when the mosaic JSON is absent."""
    try:
        payload = page.evaluate(MOSAIC_READ_JS)
    except Exception:
        payload = None

    results = _extract_results(payload)
    if results is not None:
        return results

    # Fallback: some builds only expose the payload as a script tag.
    try:
        raw = page.evaluate(
            "() => { const el = document.getElementById('mosaic-data');"
            " return el ? el.textContent : null; }"
        )
    except Exception:
        raw = None
    if raw:
        return _extract_results(_script_payload(raw))
    return None


def _script_payload(text: str):
    """Parse the mosaic JSON assignment out of a #mosaic-data script body."""
    at = text.find(_MOSIC_MARKER)
    if at < 0:
        return None
    start = text.find("{", at + len(_MOSIC_MARKER))
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _extract_results(payload) -> list | None:
    """Pull the results array out of whatever shape ``evaluate`` returned."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        meta = payload.get("metaData")
        if isinstance(meta, dict):
            model = meta.get("mosaicProviderJobCardsModel")
            if isinstance(model, dict) and isinstance(model.get("results"), list):
                return model["results"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        return _find_results(payload)
    return None


def _find_results(node) -> list | None:
    """Recursively look for a list of dicts that look like job cards."""
    if isinstance(node, list):
        if node and isinstance(node[0], dict) and (
            "jobkey" in node[0] or "jk" in node[0] or "displayTitle" in node[0]
        ):
            return node
        for entry in node:
            found = _find_results(entry)
            if found is not None:
                return found
    elif isinstance(node, dict):
        for value in node.values():
            found = _find_results(value)
            if found is not None:
                return found
    return None


def normalize_job(item: dict, base_url: str = DEFAULT_BASE_URL) -> dict | None:
    """Convert one raw mosaic job card into the bot's flat job dict.

    Returns exactly the contract keys, or ``None`` when no job key is found.
    """
    if not isinstance(item, dict):
        return None

    apply_info = item.get("apply") if isinstance(item.get("apply"), dict) else {}

    jobkey = _first(item, "jobkey", "jk")
    if not jobkey:
        match = _JK_RE.search(str(item.get("url") or item.get("jobUrl") or ""))
        jobkey = match.group(1) if match else None
    jobkey = str(jobkey or "").strip()
    if not jobkey:
        return None

    salary_raw = _first(item, "salary", "salarySnippet", "formattedSalary", "estimatedSalary", "salary_text")
    if isinstance(salary_raw, dict):
        salary = _clean_text(salary_raw.get("text") or salary_raw.get("label"))
    else:
        salary = _clean_text(salary_raw)

    applyable = _flag(_first(item, "indeedApplyable", "indeed_applyable", "isIndeedApply"))
    if applyable is None:
        applyable = _flag(apply_info.get("indeedApply"))
    if applyable is None:
        applyable = False

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    return {
        "jobkey": jobkey,
        "title": _clean_text(_first(item, "title", "displayTitle")),
        "company": _clean_text(_first(item, "company", "companyName", "source")),
        "location": _clean_text(_first(item, "location", "formattedLocation")),
        "salary": salary,
        "salary_min": parse_salary_min(salary),
        "url": f"{base}/viewjob?jk={jobkey}",
        "indeed_applyable": bool(applyable),
        "snippet": _clean_text(_first(item, "snippet", "jobSnippet")),
        "posted": _clean_text(
            _first(
                item,
                "formattedRelativeTime",
                "posted",
                "date",
                "pubDate",
                "postedDate",
            )
        ),
    }


#: British spelling kept as an alias for callers that prefer it.
normalise_job = normalize_job


def scrape_jobs(session, cfg: dict) -> list[dict]:
    """Collect job cards for the configured search and return normalised jobs.

    Pages are fetched 10 at a time; scraping stops when a page returns fewer
    than 10 cards or ``search.max_pages`` is reached. Raises
    :class:`~bot.utils.ChallengeDetected` when Indeed serves a bot wall.
    """
    log = setup_logging()
    search = (cfg or {}).get("search") or {}
    site = (cfg or {}).get("site") or {}
    base_url = (site.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    query = search.get("query", "")
    location = search.get("location", "")
    max_pages = max(1, int(search.get("max_pages", 5) or 1))
    fromage = int(search.get("fromage", 0) or 0)
    sort = search.get("sort", "date") or "date"

    found: list[dict] = []
    seen: set[str] = set()
    start = 0
    pages = 0

    while pages < max_pages:
        url = build_search_url(base_url, query, location, start, fromage, sort)
        log.info("Scraping page %d: %s", pages + 1, url)
        results = None
        page = None

        # A cleared Cloudflare challenge can leave us on www.indeed.com. Give
        # the human up to three chances to clear it and land back on the HK URL.
        for attempt in range(3):
            page = session.goto(url)
            wait_for_challenge(page, cfg, log)
            try:
                page.wait_for_function(MOSAIC_WAIT_JS, timeout=20_000)
            except Exception:
                pass
            results = _mosaic_results(page)
            if results is not None:
                break
            current = str(getattr(page, "url", "") or "")
            host = base_url.split("//")[-1]
            if current and host not in current:
                log.warning(
                    "Redirected to %s instead of %s; re-navigating (attempt %d/3).",
                    current,
                    host,
                    attempt + 1,
                )
                continue
            wait_for_challenge(page, cfg, log)
            break

        if results is None:
            # A challenge wins over a missing payload; surface it loudly.
            wait_for_challenge(page, cfg, log)
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                current = page.url
            except Exception:
                current = url
            log.error(
                "mosaic JSON not found (title=%r url=%s) - returning %d job(s) "
                "collected so far.",
                title,
                current,
                len(found),
            )
            break

        if not results:
            log.info("Page %d returned no results; stopping.", pages + 1)
            break

        page_jobs = [
            job for job in (normalize_job(item, base_url) for item in results) if job
        ]
        new_jobs = [job for job in page_jobs if job["jobkey"] not in seen]
        for job in page_jobs:
            seen.add(job["jobkey"])
        found.extend(new_jobs)

        log.info(
            "Page %d: %d card(s), %d new (%d total)",
            pages + 1,
            len(results),
            len(new_jobs),
            len(found),
        )

        start += 10
        pages += 1
        if len(results) < 10:
            log.info("Fewer than 10 results on page %d; last page reached.", pages)
            break

    jobs = found
    if search.get("fetch_job_details") and jobs:
        _fetch_details(session, jobs, log)
    return jobs


def _fetch_details(session, jobs: list[dict], log) -> None:
    """Optionally open each ``/viewjob`` page for richer text. Never fatal."""
    log.info("Fetching detail pages for %d job(s) ...", len(jobs))
    for job in jobs:
        if job.get("snippet"):
            continue
        try:
            page = session.goto(job["url"])
            body = page.locator("body").inner_text(timeout=10_000)
            job["snippet"] = _clean_text(body)[:4_000]
        except ChallengeDetected:
            raise
        except Exception as exc:
            log.warning("Could not read details for %s: %s", job["jobkey"], exc)
