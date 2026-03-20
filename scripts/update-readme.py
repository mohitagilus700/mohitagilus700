#!/usr/bin/env python3
"""Fetch private repo stats via GitHub App and update personal profile README.

Uses GITHUB_TOKEN from actions/create-github-app-token — zero dependencies.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ORG = os.environ.get("ORG_NAME", "agilusdiagnostics")
USER = os.environ.get("GITHUB_USER", "mohitagilus700")
TOKEN = os.environ["GITHUB_TOKEN"]
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def api(url):
    req = Request(url, headers={
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read()
            if not body:
                return None
            return json.loads(body)
    except (HTTPError, json.JSONDecodeError) as e:
        print(f"  WARNING: {e} for {url}", file=sys.stderr)
        return None


def api_paginate(url, max_pages=10):
    results = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in url else "?"
        data = api(f"{url}{sep}per_page=100&page={page}")
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
    """Returns {language: bytes} dict."""
    data = api(f"https://api.github.com/repos/{ORG}/{repo_name}/languages")
    return data or {}


def fetch_participation(repo_name):
    data = api(f"https://api.github.com/repos/{ORG}/{repo_name}/stats/participation")
    if data and "all" in data:
        return data["all"]
    return []


def fetch_user_prs(repo_name):
    prs = api_paginate(
        f"https://api.github.com/repos/{ORG}/{repo_name}/pulls"
        f"?state=all&sort=updated&direction=desc"
    )
    return len([p for p in prs if p.get("user", {}).get("login") == USER])


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
    total_prs = 0
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

        # PRs (only for repos I contributed to, to save API calls)
        if my_commits > 0:
            total_prs += fetch_user_prs(name)

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
