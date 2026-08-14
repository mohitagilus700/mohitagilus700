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
            if exc.code == 404:
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
            # A plain 403 with quota left is "you may not see this" -- an answer.
            print(f"  WARNING: {exc} for {url}", file=sys.stderr)
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


def api_paginate(url, max_pages=10):
    """Collect every page, letting ApiError propagate.

    DELIBERATELY NOT TOLERANT. The previous version could not tell "no more pages" from
    "that request failed" -- both came back falsy and hit the same `break` -- so a blip
    on page 2 silently returned a truncated list and the README rendered as though the
    org had a handful of repos. Failing the workflow is the better outcome: a profile
    README that occasionally does not update is harmless, one that quietly understates
    the work is not.
    """
    results = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in url else "?"
        data = api(f"{url}{sep}per_page=100&page={page}")
        # None (404) or [] (empty page) both mean there is nothing further.
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
    return results


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


def fetch_user_pr_count():
    """Count user's PRs across the org using the search API (no pull_requests permission needed).

    Optional, and the search API is the endpoint most likely to need it: its rate limit
    is far tighter than the core API (30 requests/minute authenticated), so it is the
    first thing to be throttled during a burst.
    """
    data = api_optional(
        f"https://api.github.com/search/issues"
        f"?q=author:{USER}+type:pr+org:{ORG}&per_page=1"
    )
    if data and "total_count" in data:
        return data["total_count"]
    return 0


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
        f"| \U0001f501 Pull Requests | **{total_prs:,}** |",
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

    # PRs via search API (single call instead of per-repo)
    print("Fetching PR count...")
    total_prs = fetch_user_pr_count()

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
