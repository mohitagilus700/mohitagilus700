#!/usr/bin/env python3
"""Fetch private repo stats via GitHub App and update personal profile README.

Uses GITHUB_TOKEN from actions/create-github-app-token — zero dependencies.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ORG = os.environ.get("ORG_NAME", "agilusdiagnostics")
USER = os.environ.get("GITHUB_USER", "mohitagilus700")
TOKEN = os.environ["GITHUB_TOKEN"]
IST = timezone(timedelta(hours=5, minutes=30))

# Attempts per request, and the base for exponential backoff between them.
API_ATTEMPTS = 4
API_BACKOFF_BASE = 2
# Pause between successful calls. This script fans out hard -- three calls per repo
# plus pagination -- and GitHub's SECONDARY rate limit responds to request *rate*, one
# of its documented behaviours being to hang up rather than return 429.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "0.15"))


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    """A request failed in a way that means "unknown", not "no data".

    Kept distinct from a None return because the two need opposite handling. A 404 is
    an answer -- that repo has no languages -- and rendering it as empty is correct. A
    dropped connection is NOT an answer, and treating it as one silently truncates the
    README into something that looks fine and is wrong.
    """


def api(url):
    """GET a GitHub API URL. Returns parsed JSON, or None when the answer is "nothing".

    Raises ApiError if a transient failure survives API_ATTEMPTS retries.

    RETRIES EXIST BECAUSE OF A REAL FAILURE, not defensiveness: run 31779067581 died on
    `http.client.RemoteDisconnected: Remote end closed connection without response`
    while paginating the org repo list. That is raised from `h.getresponse()`, and
    urllib re-raises it BARE -- only errors from `h.request()` get wrapped in URLError
    -- so it never matched the old `except (HTTPError, json.JSONDecodeError)` and took
    the whole workflow down. Note the class hierarchy that made it slip through:

        RemoteDisconnected -> (ConnectionResetError -> OSError, BadStatusLine -> HTTPException)
        HTTPError          -> URLError -> OSError

    They only meet at OSError, which the old clause did not catch. `timeout=30` had the
    same hole: TimeoutError is an OSError too, and was equally uncaught.
    """
    req = Request(
        url,
        headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
            # GitHub asks every API client to identify itself, and an unidentified one
            # is likelier to be dropped by abuse detection.
            "User-Agent": f"{USER}-profile-readme",
        },
    )
    for attempt in range(1, API_ATTEMPTS + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                body = resp.read()
                # /stats/participation answers 202 with an empty body while GitHub
                # computes the statistics. That is a legitimate "nothing yet".
                if not body:
                    return None
                return json.loads(body)
        # HTTPError first: it subclasses OSError, so the broad clause below would
        # otherwise swallow every HTTP status.
        except HTTPError as exc:
            # Both of these are ANSWERS, so they return quietly rather than warn:
            #   404 -- the resource does not exist, or the token cannot see it.
            #   409 -- on /commits, GitHub reports an EMPTY repository (no commits yet)
            #          as a Conflict. Seen for agilusdiagnostics/UtilityScripts and
            #          /agilus-legacy-code in run 31779771818, where logging them as
            #          WARNINGs made a healthy run look broken.
            # Zero commits is the right reading of both; neither is worth a retry.
            if exc.code in (404, 409):
                return None
            # GitHub uses 403 for BOTH rate limiting and permission denial, and they
            # need opposite handling -- retry the first, accept the second. The headers
            # tell them apart: a throttled response carries Retry-After, or
            # x-ratelimit-remaining: 0. Without this check, every repo the App token
            # cannot see would burn four attempts and then fail the run.
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            remaining = exc.headers.get("x-ratelimit-remaining") if exc.headers else None
            throttled = exc.code == 429 or (
                exc.code == 403 and (retry_after is not None or remaining == "0")
            )
            if throttled or exc.code >= 500:
                if attempt < API_ATTEMPTS:
                    # Honour Retry-After when GitHub sends it; it knows better than we do.
                    delay = API_BACKOFF_BASE**attempt
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, min(int(retry_after), 60))
                    time.sleep(delay)
                    continue
                raise ApiError(f"{exc} for {url} after {API_ATTEMPTS} attempts") from exc
            # Read the response BODY, not just the status line. GitHub explains 4xx
            # failures in the body -- a 422 in particular carries a `message` plus an
            # `errors` array naming the offending qualifier -- and discarding it made
            # run 31779938138's search failure undiagnosable from the logs alone.
            detail = ""
            try:
                raw = exc.read()
                if raw:
                    parsed = json.loads(raw)
                    detail = f" | {parsed.get('message', '')} {parsed.get('errors', '')}".rstrip()
            except (OSError, ValueError, AttributeError):
                pass

            # 403 (not throttled) and 422 mean "not permitted" / "cannot be resolved",
            # which is UNKNOWN, not "nothing". Raising keeps that distinction: returning
            # None here is how a permission gap became a confident "0 Pull Requests" on
            # the profile for every run before 2026-08-14. No retry -- a permission
            # denial is not going to change within one run.
            if exc.code in (403, 422):
                raise ApiError(f"{exc} for {url}{detail}") from exc
            print(f"  WARNING: {exc} for {url}{detail}", file=sys.stderr)
            return None
        except (OSError, HTTPException) as exc:
            # RemoteDisconnected, connection resets, DNS failures, timeouts.
            if attempt < API_ATTEMPTS:
                time.sleep(API_BACKOFF_BASE**attempt)
                continue
            raise ApiError(f"{exc} for {url} after {API_ATTEMPTS} attempts") from exc
        except json.JSONDecodeError as exc:
            print(f"  WARNING: {exc} for {url}", file=sys.stderr)
            return None
        finally:
            if API_PACING_SECONDS:
                time.sleep(API_PACING_SECONDS)
    return None


def api_optional(url):
    """api() for enrichment data, where an unknown answer should not fail the run.

    Used for the per-repo extras (languages, participation): losing one repo's language
    breakdown degrades the README slightly and visibly, so it is not worth failing over.
    The repo LIST is deliberately not fetched this way -- see api_paginate.
    """
    try:
        return api(url)
    except ApiError as exc:
        print(f"  WARNING: {exc}", file=sys.stderr)
        return None


def api_paginate(url, max_pages=100):
    """Collect every page, letting ApiError propagate.

    DELIBERATELY NOT TOLERANT. The previous version could not tell "no more pages" from
    "that request failed" -- both came back falsy and hit the same `break` -- so a blip
    on page 2 silently returned a truncated list and the README rendered as though the
    org had a handful of repos. Failing the workflow is the better outcome: a profile
    README that occasionally does not update is harmless, one that quietly understates
    the work is not.

    max_pages was 10 -- a silent 1,000-item ceiling. agilusdiagnostics/consumer-web
    already has 396 PRs and user-service 207, so a busy repo crossing 1,000 would have
    truncated with no indication at all. Now 100 (10,000 items), and exhausting it is
    reported rather than assumed to be the end of the data. The cap still exists so a
    pagination bug cannot loop forever.
    """
    results = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in url else "?"
        data = api(f"{url}{sep}per_page=100&page={page}")
        # None (404/409) or [] (empty page) both mean there is nothing further.
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
    else:
        # for/else: the loop ran out of pages rather than breaking, so page max_pages
        # was full and there is very likely more. Never silently.
        print(
            f"  WARNING: {url} hit the {max_pages}-page cap "
            f"({len(results):,} items); the total is a LOWER BOUND",
            file=sys.stderr,
        )
    return results


def api_paginate_optional(url):
    """api_paginate for data where "unknown" must stay distinguishable from "none".

    Returns None if the pages could not be read -- which is NOT the same as [], and
    conflating the two is exactly how a 403 turned into a confident zero.
    """
    try:
        return api_paginate(url)
    except ApiError as exc:
        print(f"  WARNING: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_org_repos():
    repos = api_paginate(
        f"https://api.github.com/orgs/{ORG}/repos?sort=pushed&direction=desc&type=all"
    )
    return [r for r in repos if not r.get("archived")]


def fetch_user_commits_in_repo(repo_name, since_iso):
    commits = api_paginate(
        f"https://api.github.com/repos/{ORG}/{repo_name}/commits"
        f"?author={USER}&since={since_iso}"
    )
    return len(commits)


def fetch_repo_languages(repo_name):
    """Returns {language: bytes} dict. Optional: one repo's absence is visible, not wrong."""
    data = api_optional(f"https://api.github.com/repos/{ORG}/{repo_name}/languages")
    return data or {}


def fetch_participation(repo_name):
    data = api_optional(
        f"https://api.github.com/repos/{ORG}/{repo_name}/stats/participation"
    )
    if data and "all" in data:
        return data["all"]
    return []


def fetch_user_prs_in_repo(repo_name):
    """The user's PR count in one repo, or None if it could not be read.

    Needs `issues: read` -- GitHub's REST API treats every pull request as an issue, so
    /issues returns both and the PRs are the entries carrying a `pull_request` key
    (verified against agilusdiagnostics/consumer-web).

    `creator=` filters SERVER-side, which is why this beats the dedicated /pulls
    endpoint: /pulls has no creator parameter, so it would download every PR and discard
    most. Measured 2026-08-14 -- consumer-web has 396 PRs of which 31 are the user's, so
    /pulls means 4 full pages where this means one small one.

    None, not 0, on failure: see fetch_user_pr_count.
    """
    items = api_paginate_optional(
        f"https://api.github.com/repos/{ORG}/{repo_name}/issues"
        f"?creator={USER}&state=all&filter=all"
    )
    if items is None:
        return None
    return sum(1 for item in items if "pull_request" in item)


def fetch_user_pr_count(repo_names):
    """Total PRs the user opened across the org. None if it cannot be determined at all.

    CHEAPEST PATH FIRST, RELIABLE PATH SECOND.

    The org-wide search is ONE request against ~40, so it is always worth attempting --
    but it has failed here before with 422 "The listed users and repositories cannot be
    searched either because the resources do not exist or you do not have permission to
    view them" (run 31780436676), while the query itself is valid (84 via a user PAT).
    That was traced to the App lacking `issues: read`; whether granting it also fixes
    the org-scoped search, or only the repo-scoped endpoint, is not something we can
    know without running it. So: try the cheap one, fall back to the certain one.

    RETURNS None, NOT 0, WHEN NOTHING IS READABLE -- that distinction is the whole
    point. A zero is indistinguishable from "no PRs" and rendered a confident, wrong
    "0 Pull Requests" on the profile for every run before 2026-08-14. None renders as a
    dash, which is honest. A PARTIAL read returns the sum and says so: understating by
    one repo is worth reporting, but discarding 39 good counts is not.
    """
    try:
        data = api(
            f"https://api.github.com/search/issues"
            f"?q=author:{USER}+type:pr+org:{ORG}&per_page=1"
        )
        if data and "total_count" in data:
            print("  (via org-wide search: 1 request)")
            return data["total_count"]
    except ApiError as exc:
        print(f"  search unavailable, falling back to per-repo: {exc}", file=sys.stderr)

    total = 0
    unreadable = 0
    for name in repo_names:
        count = fetch_user_prs_in_repo(name)
        if count is None:
            unreadable += 1
        else:
            total += count

    if unreadable == len(repo_names):
        return None
    if unreadable:
        print(
            f"  WARNING: {unreadable}/{len(repo_names)} repos unreadable -- "
            f"PR count {total} is a LOWER BOUND",
            file=sys.stderr,
        )
    else:
        print(f"  (via per-repo: {len(repo_names)} requests)")
    return total


# ---------------------------------------------------------------------------
# Markdown generators
# ---------------------------------------------------------------------------

LANG_COLORS = {
    "Java": "\U0001f7e7",
    "Python": "\U0001f7e6",
    "TypeScript": "\U0001f7e6",
    "JavaScript": "\U0001f7e8",
    "HTML": "\U0001f7e5",
    "CSS": "\U0001f7ea",
    "Shell": "\U0001f7e9",
    "Dockerfile": "\U0001f7e6",
    "HCL": "\U0001f7ea",
    "Go": "\U0001f7e6",
}


def mini_bar(count, max_count, width=10):
    """Purple -> blue -> green gradient bar."""
    if max_count == 0 or count == 0:
        return "\u2b1c"
    fill = max(1, round(width * count / max_count))
    palette = [
        "\U0001f7ea", "\U0001f7ea", "\U0001f7ea",
        "\U0001f7e6", "\U0001f7e6", "\U0001f7e6", "\U0001f7e6",
        "\U0001f7e9", "\U0001f7e9", "\U0001f7e9",
    ]
    return "".join(palette[i] for i in range(fill))


def inline_graph(counts):
    """Inline horizontal graph using green squares."""
    if not counts or max(counts) == 0:
        return "\u2b1c\u2b1c\u2b1c\u2b1c"
    mx = max(counts)
    green = "\U0001f7e9"
    white = "\u2b1c"
    result = []
    for c in counts:
        if c == 0:
            result.append(white)
        elif c <= mx * 0.25:
            result.append(green)
        elif c <= mx * 0.5:
            result.append(green * 2)
        elif c <= mx * 0.75:
            result.append(green * 3)
        else:
            result.append(green * 4)
    return " ".join(result)


def md_github_stats(total_commits, total_prs, repos_contributed, total_repos):
    """GitHub stats summary."""
    lines = [
        "| Stat | Count |",
        "|:-----|------:|",
        f"| \U0001f4bb Total Commits (30d) | **{total_commits:,}** |",
        # A dash, not 0, when the count could not be fetched. Rendering an unavailable
        # value as zero states something false with full confidence -- and it did, for
        # every run before 2026-08-14. See fetch_user_pr_count.
        f"| \U0001f501 Pull Requests | **{total_prs:,}** |"
        if total_prs is not None
        else "| \U0001f501 Pull Requests | — |",
        f"| \U0001f4c2 Repos Contributed To | **{repos_contributed}** |",
        f"| \U0001f3e2 Total Org Repos | **{total_repos}** |",
    ]
    return "\n".join(lines)


def md_top_languages(lang_totals):
    """Top languages table with progress bar."""
    ranked = sorted(lang_totals.items(), key=lambda x: x[1], reverse=True)[:8]
    if not ranked:
        return "_No language data available._"
    total = sum(b for _, b in ranked)
    mx = ranked[0][1] or 1
    lines = [
        "| Language | Usage | Share |",
        "|:---------|:------|------:|",
    ]
    for lang, bytes_count in ranked:
        pct = bytes_count / total * 100 if total else 0
        color = LANG_COLORS.get(lang, "\U0001f7e6")
        bar_len = max(1, round(10 * bytes_count / mx))
        bar = color * bar_len
        lines.append(f"| **{lang}** | {bar} | `{pct:.1f}%` |")
    return "\n".join(lines)


def md_working_on(repo_data):
    """Repos user is actively contributing to."""
    active = [r for r in repo_data if r["my_commits"] > 0]
    if not active:
        return "_No recent contributions._"
    active.sort(key=lambda r: r["my_commits"], reverse=True)
    mx = active[0]["my_commits"] or 1
    lines = [
        "| Repository | Language | My Commits (30d) | Activity |",
        "|:-----------|:--------:|:----------------:|:---------|",
    ]
    for r in active[:5]:
        lang = r.get("language") or "\u2014"
        bar = mini_bar(r["my_commits"], mx)
        lines.append(
            f"| [`{r['name']}`](https://github.com/{ORG}/{r['name']}) "
            f"| `{lang}` | **{r['my_commits']}** | {bar} |"
        )
    return "\n".join(lines)


def md_weekly_activity(weekly_data):
    """Weekly activity table with inline graph."""
    if not weekly_data:
        return "_No activity data available._"

    chart_data = []
    for name, weeks_52 in weekly_data:
        last4 = weeks_52[-4:] if len(weeks_52) >= 4 else weeks_52
        while len(last4) < 4:
            last4.insert(0, 0)
        chart_data.append((name, last4))

    def get_trend(last4):
        if last4[-2] > 0:
            change = (last4[-1] - last4[-2]) / last4[-2] * 100
            if change > 25:
                return "\U0001f525"
            elif change > 10:
                return "\u2b06\ufe0f"
            elif change < -25:
                return "\u26a0\ufe0f"
            elif change < -10:
                return "\u2b07\ufe0f"
            else:
                return "\u2714\ufe0f"
        return "\u2728" if last4[-1] > 0 else "\U0001f4a4"

    lines = [
        "| Repository | W\u20113 | W\u20112 | W\u20111 | Now | Activity Graph | Last 4w Commits | Trend |",
        "|:-----------|----:|----:|----:|----:|:------|:------:|:-----:|",
    ]

    for name, last4 in chart_data:
        total = sum(last4)
        trend = get_trend(last4)
        graph = inline_graph(last4)
        lines.append(
            f"| `{name}` | {last4[0]} | {last4[1]} "
            f"| {last4[2]} | {last4[3]} | {graph} | **{total}** | {trend} |"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# README update
# ---------------------------------------------------------------------------

def update_readme(path, sections):
    with open(path) as f:
        content = f.read()

    now = datetime.now(IST).strftime("%B %d, %Y %I:%M %p IST")

    for marker, md in sections.items():
        content = re.sub(
            rf"(<!-- {marker}_START -->).*?(<!-- {marker}_END -->)",
            rf"\1\n{md}\n\2",
            content,
            flags=re.DOTALL,
        )

    content = re.sub(
        r"Last updated: \*\*.*?\*\*",
        f"Last updated: **{now}**",
        content,
    )

    with open(path, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    readme = os.path.join(os.path.dirname(__file__), "..", "README.md")

    # 1. Fetch all org repos
    print("Fetching repos...")
    repos = fetch_org_repos()
    print(f"  {len(repos)} active repos found.")

    # 2. Per-repo: my commits, languages, participation
    print("Fetching per-repo stats...")
    since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    lang_totals = {}
    repo_data = []
    total_commits = 0
    repos_contributed = 0
    weekly_data = []

    for repo in repos:
        name = repo["name"]
        print(f"  {name}...")

        # My commits
        my_commits = fetch_user_commits_in_repo(name, since_30d)
        total_commits += my_commits
        if my_commits > 0:
            repos_contributed += 1

        # Languages
        langs = fetch_repo_languages(name)
        for lang, bytes_count in langs.items():
            lang_totals[lang] = lang_totals.get(lang, 0) + bytes_count

        # Weekly activity (top 5 repos by push date)
        if len(weekly_data) < 5:
            weeks = fetch_participation(name)
            if weeks:
                weekly_data.append((name, weeks))

        repo_data.append({
            "name": name,
            "language": repo.get("language"),
            "my_commits": my_commits,
        })

    weekly_data.sort(key=lambda x: sum(x[1][-4:]), reverse=True)

    print("Fetching PR count...")
    total_prs = fetch_user_pr_count([r["name"] for r in repos])
    print(f"  PRs: {total_prs if total_prs is not None else 'unavailable (App permissions)'}")

    # 3. Generate sections
    sections = {
        "GITHUB_STATS": md_github_stats(total_commits, total_prs, repos_contributed, len(repos)),
        "TOP_LANGUAGES": md_top_languages(lang_totals),
        "WORKING_ON": md_working_on(repo_data),
        "WEEKLY_ACTIVITY": md_weekly_activity(weekly_data),
    }

    # 4. Write
    update_readme(readme, sections)
    print("README updated successfully.")


if __name__ == "__main__":
    main()
