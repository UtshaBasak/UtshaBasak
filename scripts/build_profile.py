"""Build the live images and badges shown on the GitHub profile README.

Run by ``.github/workflows/profile.yml`` every few hours. It reads public data from the GitHub
API, folds the 14-day traffic numbers into a record that never forgets, and writes everything
into one output directory, which the workflow publishes to the ``output`` branch:

    python scripts/build_profile.py <output-dir> [<previous-output-dir>]

Environment:
    GH_TOKEN       token for the GraphQL API (the workflow's own token is enough)
    TRAFFIC_TOKEN  optional; a token that may read repository traffic (Administration: read).
                   Without it the clone and view records keep their last values.

Standard library only, so the workflow needs no installation step.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

USER = "UtshaBasak"
PROFILE_REPO = USER
RECORD = "data/traffic.json"
SUMMARY = "data/profile.json"  # read by Byte, the robot on the GitHub Pages site
API = "https://api.github.com"

# Notebooks are counted by their stored outputs, not their code, so they would swamp the chart.
HIDDEN_LANGUAGES = {"Jupyter Notebook"}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

THEMES: dict[str, dict[str, Any]] = {
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "accent": "#38bdf8",
        "accent2": "#a78bfa",
        "track": "#21262d",
        "levels": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
        "snake_head": "#ff3b3b",
        "snake_body": ("#ff3b3b", "#3b82f6"),
        "eye": "#ffffff",
        "pupil": "#0d1117",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "text": "#1f2328",
        "muted": "#59636e",
        "accent": "#0969da",
        "accent2": "#8250df",
        "track": "#eff2f5",
        "levels": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "snake_head": "#dc2626",
        "snake_body": ("#dc2626", "#2563eb"),
        "eye": "#ffffff",
        "pupil": "#1f2328",
    },
}

LEVELS = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}

QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
    pullRequests { totalCount }
    issues { totalCount }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date weekday contributionCount contributionLevel } }
      }
    }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC,
                 orderBy: {field: STARGAZERS, direction: DESC}) {
      nodes {
        name
        description
        url
        homepageUrl
        createdAt
        pushedAt
        isFork
        isArchived
        stargazerCount
        forkCount
        watchers { totalCount }
        defaultBranchRef { target { ... on Commit { history { totalCount } } } }
        primaryLanguage { name color }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------- GitHub API


def request(url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", f"{USER}-profile-readme")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def fetch_profile(token: str) -> dict[str, Any]:
    result = request(f"{API}/graphql", token, {"query": QUERY, "variables": {"login": USER}})
    if result.get("errors"):
        raise RuntimeError(f"GraphQL error: {result['errors']}")
    return result["data"]["user"]


def fetch_history(token: str, joined: str, now: dt.datetime) -> list[dict[str, Any]]:
    """Every contribution day since the account was created, oldest first.

    The API returns at most one year per query, so each calendar year gets its own alias.
    """
    parts = []
    for year in range(int(joined[:4]), now.year + 1):
        start = max(f"{year}-01-01T00:00:00Z", joined)
        end = min(f"{year}-12-31T23:59:59Z", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        parts.append(
            f'y{year}: contributionsCollection(from: "{start}", to: "{end}") '
            "{ contributionCalendar { weeks { contributionDays { date contributionCount } } } }"
        )
    query = f'query {{ user(login: "{USER}") {{ {" ".join(parts)} }} }}'
    result = request(f"{API}/graphql", token, {"query": query})
    if result.get("errors"):
        raise RuntimeError(f"GraphQL error: {result['errors']}")
    days: dict[str, int] = {}
    for collection in result["data"]["user"].values():
        for week in collection["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
    return [{"date": k, "contributionCount": v} for k, v in sorted(days.items())]


def longest_streak(days: list[dict[str, Any]]) -> tuple[int, str | None, str | None]:
    """Longest run of consecutive days with contributions, with its first and last day."""
    best, best_end, run, prev = 0, None, 0, None
    for d in days:
        day = dt.date.fromisoformat(d["date"])
        if d["contributionCount"]:
            run = run + 1 if prev and day - prev == dt.timedelta(days=1) and run else 1
            if run > best:
                best, best_end = run, day
        else:
            run = 0
        prev = day
    if not best_end:
        return 0, None, None
    return best, (best_end - dt.timedelta(days=best - 1)).isoformat(), best_end.isoformat()


def projects_of(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Every public repository except the profile itself and archived ones."""
    return [
        r
        for r in user["repositories"]["nodes"]
        if not r["isArchived"] and r["name"] != PROFILE_REPO
    ]


# --------------------------------------------------------------------------- traffic record


def fold_traffic(
    record: dict[str, Any], names: list[str], token: str | None, today: dt.date
) -> tuple[dict[str, Any], bool]:
    """Fold the latest 14 days of clones and views into the record.

    A day still inside GitHub's window is overwritten, since GitHub may still be counting it;
    once it leaves the window, its last count stays. Returns the record and whether it was
    refreshed.
    """
    record.setdefault("repos", {})
    if not token:
        return record, False
    for name in names:
        repo = record["repos"].setdefault(name, {"clones": {}, "views": {}})
        for kind in ("clones", "views"):
            try:
                body = request(f"{API}/repos/{USER}/{name}/traffic/{kind}", token)
            except urllib.error.HTTPError as exc:
                # One unreadable repository must not hold back the rest of the profile.
                print(f"::warning::traffic/{kind} for {name}: HTTP {exc.code}; keeping its record.")
                continue
            for day in body.get(kind, []):
                repo[kind][day["timestamp"][:10]] = {
                    "count": int(day["count"]),
                    "uniques": int(day["uniques"]),
                }
            repo[kind] = dict(sorted(repo[kind].items()))
    if "since" not in record:
        # The first window is all GitHub could tell us; nothing older can be known.
        first = [d for repo in record["repos"].values() for kind in repo.values() for d in kind]
        record["since"] = min(first, default=today.isoformat())
    record["updated"] = today.isoformat()
    return record, True


def traffic_total(record: dict[str, Any], name: str, kind: str) -> int:
    since = record.get("since", "9999-12-31")
    days = record.get("repos", {}).get(name, {}).get(kind, {})
    return sum(v["count"] for d, v in days.items() if d >= since)


# --------------------------------------------------------------------------- helpers


def fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%b %Y')}"


def fmt_span(first: str, last: str) -> str:
    a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    if a == b:
        return fmt_date(first)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {b.strftime('%b %Y')}"
    if a.year == b.year:
        return f"{a.day} {a.strftime('%b')} – {b.day} {b.strftime('%b %Y')}"
    return f"{fmt_date(first)} – {fmt_date(last)}"


def num(n: int) -> str:
    return f"{n:,}"


def pct(t: float, total: float) -> str:
    return f"{100 * t / total:.4f}".rstrip("0").rstrip(".") + "%"


def svg_open(w: int, h: int, title: str, theme: dict[str, Any], css: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-labelledby="t">'
        f'<title id="t">{escape(title)}</title>'
        f"<style>"
        f"text{{font-family:{FONT};fill:{theme['text']}}}"
        f".m{{fill:{theme['muted']}}}.a{{fill:{theme['accent']}}}"
        f".h{{font-size:16px;font-weight:700}}"
        f".fade{{opacity:0;animation:fade .6s ease-out forwards}}"
        f"@keyframes fade{{from{{opacity:0;transform:translateY(6px)}}"
        f"to{{opacity:1;transform:none}}}}"
        f"{css}"
        f"@media (prefers-reduced-motion:reduce){{*{{animation:none!important;opacity:1!important}}}}"
        f"</style>"
        f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="10" '
        f'fill="{theme["bg"]}" stroke="{theme["border"]}"/>'
    )


def delay(i: int, step: float = 0.08, start: float = 0.1) -> str:
    return f'style="animation-delay:{start + i * step:.2f}s"'


# --------------------------------------------------------------------------- cards


def stats_card(m: dict[str, Any], theme: dict[str, Any]) -> str:
    w, h = 420, 250
    since = fmt_date(m["since"]) if m["since"] else "tracking starts"
    rows = [
        ("⭐", "Total stars", m["stars"]),
        ("🍴", "Total forks", m["forks"]),
        ("👀", "Watchers", m["watchers"]),
        ("📝", "Total commits", m["commits"]),
        ("📥", "Clones*", m["clones"]),
        ("👁️", "Project views*", m["views"]),
        ("🔀", "Pull requests", m["prs"]),
        ("🐞", "Issues", m["issues"]),
    ]
    out = [svg_open(w, h, f"{USER}'s GitHub stats", theme, "")]
    out.append(f'<text x="22" y="34" class="h fade">GitHub at a glance</text>')
    for i, (icon, label, value) in enumerate(rows):
        col, row = i % 2, i // 2
        x, y = 22 + col * 200, 72 + row * 38
        out.append(
            f'<g class="fade" {delay(i)}>'
            f'<text x="{x}" y="{y}" font-size="14">{icon}</text>'
            f'<text x="{x + 24}" y="{y}" font-size="13" class="m">{escape(label)}</text>'
            f'<text x="{x + 182}" y="{y}" font-size="14" font-weight="700" '
            f'text-anchor="end" class="a">{num(value)}</text></g>'
        )
    out.append(
        f'<text x="22" y="{h - 16}" font-size="11" class="m fade" {delay(11)}>'
        f"* since {escape(since)} · updated {escape(m['updated'])}</text></svg>"
    )
    return "".join(out)


def languages_card(langs: list[tuple[str, int, str]], theme: dict[str, Any]) -> str:
    w, h = 420, 250
    total = sum(size for _, size, _ in langs) or 1
    top = langs[:8]
    rest = total - sum(size for _, size, _ in top)
    if rest > 0:
        top.append(("Other", rest, theme["muted"]))
    css = (
        ".bar{transform-box:fill-box;transform-origin:left;transform:scaleX(0);"
        "animation:grow .9s cubic-bezier(.2,.8,.2,1) forwards}"
        "@keyframes grow{to{transform:scaleX(1)}}"
    )
    out = [svg_open(w, h, f"{USER}'s most used languages", theme, css)]
    out.append('<text x="22" y="34" class="h fade">Most used languages</text>')
    bar_w, x = w - 44, 22.0
    out.append(f'<clipPath id="c"><rect x="22" y="50" width="{bar_w}" height="10" rx="5"/></clipPath>')
    out.append(f'<rect x="22" y="50" width="{bar_w}" height="10" rx="5" fill="{theme["track"]}"/>')
    out.append('<g clip-path="url(#c)">')
    for i, (_, size, color) in enumerate(top):
        seg = bar_w * size / total
        out.append(
            f'<rect class="bar" {delay(i, .07, .2)} x="{x:.2f}" y="50" width="{seg + .5:.2f}" '
            f'height="10" fill="{color}"/>'
        )
        x += seg
    out.append("</g>")
    for i, (name, size, color) in enumerate(top):
        col, row = i % 2, i // 2
        lx, ly = 22 + col * 200, 92 + row * 30
        out.append(
            f'<g class="fade" {delay(i, .07, .3)}>'
            f'<circle cx="{lx + 6}" cy="{ly - 4}" r="5" fill="{color}"/>'
            f'<text x="{lx + 18}" y="{ly}" font-size="13">{escape(name)}</text>'
            f'<text x="{lx + 182}" y="{ly}" font-size="12" text-anchor="end" class="m">'
            f"{100 * size / total:.1f}%</text></g>"
        )
    out.append(
        f'<text x="22" y="{h - 16}" font-size="11" class="m fade" {delay(10)}>'
        "By code size across my own repositories · notebooks excluded</text></svg>"
    )
    return "".join(out)


def projects_card(rows: list[dict[str, Any]], since: str | None, theme: dict[str, Any]) -> str:
    w = 840
    row_h = 30
    h = 96 + row_h * len(rows) + 20
    cols = [
        ("Commits", 500),
        ("Stars", 560),
        ("Forks", 620),
        ("Watchers", 695),
        ("Clones*", 760),
        ("Views*", w - 22),
    ]
    out = [svg_open(w, h, "Impact of each project", theme, "")]
    out.append('<text x="22" y="34" class="h fade">Project impact</text>')
    out.append(
        f'<text x="{w - 22}" y="34" font-size="11" text-anchor="end" class="m fade">'
        f"* since {escape(fmt_date(since)) if since else 'tracking starts'}</text>"
    )
    y = 66
    out.append(f'<g class="fade" {delay(0)}><text x="22" y="{y}" font-size="12" class="m">Project</text>')
    out.append(f'<text x="300" y="{y}" font-size="12" class="m">Language</text>')
    for label, cx in cols:
        out.append(f'<text x="{cx}" y="{y}" font-size="12" text-anchor="end" class="m">{label}</text>')
    out.append("</g>")
    out.append(f'<line x1="22" x2="{w - 22}" y1="{y + 10}" y2="{y + 10}" stroke="{theme["border"]}"/>')
    for i, r in enumerate(rows):
        ty = y + 32 + i * row_h
        out.append(
            f'<g class="row fade" {delay(i + 1, .06)}>'
            f'<text x="22" y="{ty}" font-size="13" font-weight="600">{escape(r["name"])}</text>'
            f'<circle cx="306" cy="{ty - 4}" r="5" fill="{r["color"]}"/>'
            f'<text x="318" y="{ty}" font-size="12" class="m">{escape(r["language"])}</text>'
        )
        for (key, (_, cx)) in zip(("commits", "stars", "forks", "watchers", "clones", "views"), cols):
            out.append(
                f'<text x="{cx}" y="{ty}" font-size="13" text-anchor="end" '
                f'font-family="{MONO}">{num(r[key])}</text>'
            )
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def activity_card(
    days: list[dict[str, Any]], streak: tuple[int, str | None, str | None], theme: dict[str, Any]
) -> str:
    w, h = 840, 250
    months: dict[str, int] = {}
    for d in days:
        months[d["date"][:7]] = months.get(d["date"][:7], 0) + d["contributionCount"]
    keys = sorted(months)[-12:]
    peak = max((months[k] for k in keys), default=0) or 1
    total = sum(d["contributionCount"] for d in days)
    weekdays = [0] * 7
    for d in days:
        weekdays[dt.date.fromisoformat(d["date"]).isoweekday() % 7] += d["contributionCount"]
    names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    best_day = max(days, key=lambda d: d["contributionCount"]) if days else None
    active = sum(1 for d in days if d["contributionCount"])
    longest, first, last = streak
    css = (
        ".bar{transform-box:fill-box;transform-origin:bottom;transform:scaleY(0);"
        "animation:rise 1s cubic-bezier(.2,.8,.2,1) forwards}"
        "@keyframes rise{to{transform:scaleY(1)}}"
    )
    out = [svg_open(w, h, f"{USER}'s contributions by month", theme, css)]
    out.append('<text x="22" y="34" class="h fade">Contributions in the last 12 months</text>')
    out.append(
        f'<defs><linearGradient id="g" x1="0" y1="1" x2="0" y2="0">'
        f'<stop offset="0" stop-color="{theme["accent2"]}"/>'
        f'<stop offset="1" stop-color="{theme["accent"]}"/></linearGradient></defs>'
    )
    base, chart_h, x0, slot = 210, 140, 22, 46
    for i, k in enumerate(keys):
        v = months[k]
        bh = max(2.0, chart_h * v / peak) if v else 0
        bx = x0 + i * slot
        if bh:
            out.append(
                f'<rect class="bar" {delay(i, .06, .2)} x="{bx + 8}" y="{base - bh:.1f}" '
                f'width="30" height="{bh:.1f}" rx="5" fill="url(#g)"/>'
            )
        month = dt.date.fromisoformat(k + "-01").strftime("%b")
        out.append(
            f'<g class="fade" {delay(i, .06, .5)}>'
            f'<text x="{bx + 23}" y="{base - bh - 6:.1f}" font-size="11" text-anchor="middle">{v}</text>'
            f'<text x="{bx + 23}" y="{base + 18}" font-size="11" text-anchor="middle" class="m">{month}</text></g>'
        )
    facts = [
        ("Total", num(total)),
        ("Active days", f"{active} of {len(days)}"),
        (
            f"Longest streak · {fmt_span(first, last)}" if first and last else "Longest streak",
            f"{longest} days",
        ),
        ("Busiest weekday", names[weekdays.index(max(weekdays))] if days else "-"),
        (
            "Best day",
            f"{fmt_date(best_day['date'])} ({best_day['contributionCount']})" if best_day else "-",
        ),
    ]
    out.append(f'<line x1="590" x2="590" y1="60" y2="226" stroke="{theme["border"]}"/>')
    for i, (label, value) in enumerate(facts):
        fy = 70 + i * 32
        out.append(
            f'<g class="fade" {delay(i, .08, .6)}>'
            f'<text x="610" y="{fy}" font-size="11" class="m">{escape(label)}</text>'
            f'<text x="610" y="{fy + 16}" font-size="14" font-weight="700" class="a">{escape(value)}</text></g>'
        )
    out.append("</svg>")
    return "".join(out)


EDUCATION = [
    ("🎓", "BRAC University", "Dhaka", "BSc in Computer Science", "September 2026"),
    ("🏫", "Notre Dame College", "Dhaka", "Higher Secondary Certificate (HSC)", "2020"),
    ("🏫", "Bindu Basini Govt. Boys' High School", "Tangail", "Secondary School Certificate (SSC)", "2018"),
]


def education_card(theme: dict[str, Any]) -> str:
    w, row_h = 840, 44
    h = 48 + row_h * len(EDUCATION) + 16
    out = [svg_open(w, h, "Education", theme, "")]
    y = 34
    out.append(
        f'<g class="fade" {delay(0)}>'
        f'<text x="64" y="{y}" font-size="12" class="m">Institution</text>'
        f'<text x="450" y="{y}" font-size="12" class="m">Qualification</text>'
        f'<text x="{w - 24}" y="{y}" font-size="12" text-anchor="end" class="m">Year</text></g>'
    )
    out.append(f'<line x1="16" x2="{w - 16}" y1="{y + 14}" y2="{y + 14}" stroke="{theme["border"]}"/>')
    for i, (icon, school, city, qualification, year) in enumerate(EDUCATION):
        ty = y + 14 + row_h * i + row_h / 2 + 5
        if i:
            out.append(
                f'<line x1="16" x2="{w - 16}" y1="{ty - row_h / 2 - 5}" y2="{ty - row_h / 2 - 5}" '
                f'stroke="{theme["border"]}" stroke-opacity=".6"/>'
            )
        out.append(
            f'<g class="fade" {delay(i + 1, .12)}>'
            f'<text x="26" y="{ty}" font-size="18">{icon}</text>'
            f'<text x="64" y="{ty}" font-size="14"><tspan font-weight="700">{escape(school)}</tspan>'
            f'<tspan class="m">, {escape(city)}</tspan></text>'
            f'<text x="450" y="{ty}" font-size="14">{escape(qualification)}</text>'
            f'<text x="{w - 24}" y="{ty}" font-size="14" font-weight="700" text-anchor="end" '
            f'class="a">{escape(year)}</text></g>'
        )
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- snake

STEP = 0.2  # seconds per cell; the snake moves at five cells a second
START_LENGTH = 3
BITES_PER_SEGMENT = 10  # the snake grows by one segment for every ten squares it eats
CELL, PITCH, LEFT, TOP = 11, 14, 8, 24


def snake_path(weeks: list[list[dict[str, Any]]]) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
    """Walk the grid greedily from food to nearest food. Returns head positions per step and
    the step at which each food cell is eaten. Anything the head crosses on the way is eaten."""
    food = {
        (c, d["weekday"])
        for c, week in enumerate(weeks)
        for d in week
        if d["contributionCount"] > 0
    }
    # Start far enough left that every new segment appears behind the tail, never early.
    pos = (-(START_LENGTH + 3), 0)
    path = [pos]
    eaten: dict[tuple[int, int], int] = {}
    while food:
        target = min(food, key=lambda f: (abs(f[0] - pos[0]) + abs(f[1] - pos[1]), f[0], f[1]))
        while pos != target:
            if pos[1] != target[1] and pos[0] >= 0:
                pos = (pos[0], pos[1] + (1 if target[1] > pos[1] else -1))
            else:
                pos = (pos[0] + (1 if target[0] > pos[0] else -1), pos[1])
            path.append(pos)
            if pos in food:
                food.discard(pos)
                eaten[pos] = len(path) - 1
    return path, eaten


def snake_svg(weeks: list[list[dict[str, Any]]], theme: dict[str, Any]) -> str:
    cols = len(weeks)
    w, h = LEFT * 2 + cols * PITCH - (PITCH - CELL), TOP + 7 * PITCH + 6
    path, eaten = snake_path(weeks)
    order = sorted(eaten.items(), key=lambda kv: kv[1])
    length = START_LENGTH + len(order) // BITES_PER_SEGMENT
    last_eat = order[-1][1] if order else 0
    fade_start = max(last_eat, len(path) - 1) + 5
    fade_end = fade_start + 3
    steps = fade_end + 6  # a short pause before the next round
    dur = steps * STEP

    def xy(p: tuple[int, int]) -> tuple[int, int]:
        return LEFT + p[0] * PITCH, TOP + p[1] * PITCH

    def at(step: int) -> str:
        return pct(step, steps)

    # Motion: one keyframe per turn; straight runs are interpolated.
    keep = [0]
    for i in range(1, len(path) - 1):
        a, b, c = path[i - 1], path[i], path[i + 1]
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            keep.append(i)
    keep.append(len(path) - 1)
    frames = "".join(
        f"{at(i)}{{transform:translate({xy(path[i])[0]}px,{xy(path[i])[1]}px)}}" for i in keep
    )
    css = [
        f"@keyframes mv{{{frames}100%{{transform:translate({xy(path[-1])[0]}px,{xy(path[-1])[1]}px)}}}}",
        f".s{{animation:mv {dur:.1f}s linear infinite both}}",
        # The first segments start hidden: until they have moved, they still sit where the
        # previous round ended.
        f"@keyframes base{{0%,{at(START_LENGTH)}{{opacity:0}}{pct(START_LENGTH + .1, steps)},"
        f"{at(fade_start)}{{opacity:1}}{at(fade_end)},100%{{opacity:0}}}}",
    ]
    body = []
    # Food: hidden from the moment it is eaten until the snake has faded out.
    food_rects = []
    for (c, r), when in order:
        css.append(
            f"@keyframes f{c}_{r}{{0%,{at(when)}{{opacity:1}}{pct(when + .2, steps)},"
            f"{at(fade_end)}{{opacity:0}}{pct(fade_end + 1, steps)},100%{{opacity:1}}}}"
        )
    cells = []
    by_cell = {(c, d["weekday"]): d for c, week in enumerate(weeks) for d in week}
    for (c, r), d in sorted(by_cell.items()):
        x, y = xy((c, r))
        cells.append(
            f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2" fill="{theme["levels"][0]}"/>'
        )
        if (c, r) in eaten:
            level = max(1, LEVELS.get(d["contributionLevel"], 1))
            food_rects.append(
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2" '
                f'fill="{theme["levels"][level]}" style="animation:f{c}_{r} {dur:.1f}s linear infinite"/>'
            )
    # Snake: segment k follows the head k steps behind. A new segment appears with every
    # tenth bite.
    red, blue = theme["snake_body"]
    for k in range(length - 1, -1, -1):
        motion = f"animation:mv {dur:.1f}s linear {k * STEP:.1f}s infinite both,"
        if k < START_LENGTH:
            vis = f"base {dur:.1f}s linear infinite"
        else:
            born = order[(k - START_LENGTH + 1) * BITES_PER_SEGMENT - 1][1]
            css.append(
                f"@keyframes o{k}{{0%,{at(born)}{{opacity:0}}{pct(born + .3, steps)},"
                f"{at(fade_start)}{{opacity:1}}{at(fade_end)},100%{{opacity:0}}}}"
            )
            vis = f"o{k} {dur:.1f}s linear infinite"
        if k == 0:
            e, p = theme["eye"], theme["pupil"]
            body.append(
                f'<g style="{motion}{vis}">'
                f'<rect x="-1" y="-1" width="{CELL + 2}" height="{CELL + 2}" rx="4" fill="{theme["snake_head"]}"/>'
                f'<circle cx="3.5" cy="3.5" r="2" fill="{e}"/><circle cx="7.5" cy="3.5" r="2" fill="{e}"/>'
                f'<circle cx="4" cy="3.8" r="1" fill="{p}"/><circle cx="8" cy="3.8" r="1" fill="{p}"/></g>'
            )
        else:
            color = blue if k % 2 else red  # alternating stripes behind the red head
            body.append(
                f'<rect width="{CELL}" height="{CELL}" rx="3" fill="{color}" style="{motion}{vis}"/>'
            )
    labels = []
    seen: set[str] = set()
    for c, week in enumerate(weeks):
        first = week[0]["date"]
        month = dt.date.fromisoformat(first).strftime("%b")
        if first[8:10] <= "07" and month not in seen and c < cols - 2:
            seen.add(month)
            labels.append(
                f'<text x="{LEFT + c * PITCH}" y="{TOP - 9}" font-size="10" '
                f'font-family="{FONT}" fill="{theme["muted"]}">{month}</text>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="t"><title id="t">A red and blue snake eating {USER}\'s contribution '
        f"graph and growing one segment for every ten squares</title><style>{''.join(css)}"
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}</style>"
        f"{''.join(labels)}{''.join(cells)}{''.join(food_rects)}{''.join(body)}</svg>"
    )


# --------------------------------------------------------------------------- badges


def badge(label: str, message: str, color: str, logo: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
        "cacheSeconds": 3600,
    }
    if logo:
        out["namedLogo"] = logo
        out["logoColor"] = "white"
    return out


# --------------------------------------------------------------------------- main


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(argv[0])
    previous = Path(argv[1]) if len(argv) == 2 else None
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("error: GH_TOKEN is not set", file=sys.stderr)
        return 2
    traffic_token = os.environ.get("TRAFFIC_TOKEN") or None

    now = dt.datetime.now(dt.timezone.utc)
    user = fetch_profile(token)
    projects = projects_of(user)
    names = [r["name"] for r in projects]

    record: dict[str, Any] = {}
    if previous and (previous / RECORD).is_file():
        record = json.loads((previous / RECORD).read_text(encoding="utf-8"))
    record, refreshed = fold_traffic(record, names, traffic_token, now.date())
    if not refreshed:
        print("::warning::TRAFFIC_TOKEN is not set, so clones and views were not refreshed.")

    out_dir.mkdir(parents=True, exist_ok=True)
    write(out_dir / RECORD, json.dumps(record, indent=2))

    since = record.get("since")
    rows = []
    for r in projects:
        lang = r["primaryLanguage"] or {"name": "Packet Tracer", "color": "#1ba0d7"}
        rows.append(
            {
                "name": r["name"],
                "language": lang["name"],
                "color": lang["color"] or "#8b949e",
                "stars": r["stargazerCount"],
                "forks": r["forkCount"],
                "watchers": r["watchers"]["totalCount"],
                "clones": traffic_total(record, r["name"], "clones"),
                "views": traffic_total(record, r["name"], "views"),
                # Every commit on the default branch, as the repository page counts them.
                "commits": ((r["defaultBranchRef"] or {}).get("target") or {})
                .get("history", {})
                .get("totalCount", 0),
                "description": r["description"] or "",
                "url": r["url"],
                "homepage": r["homepageUrl"] or "",
                "created": r["createdAt"],
                "pushed": r["pushedAt"],
                "fork": r["isFork"],
            }
        )
    rows.sort(key=lambda r: (-r["commits"], -r["stars"], r["name"].lower()))

    sizes: dict[str, list[Any]] = {}
    for r in projects:
        if r["isFork"]:
            continue  # a fork's code is not all mine
        for edge in r["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in HIDDEN_LANGUAGES:
                continue
            entry = sizes.setdefault(name, [0, edge["node"]["color"] or "#8b949e"])
            entry[0] += edge["size"]
    langs = sorted(((n, s, c) for n, (s, c) in sizes.items()), key=lambda x: -x[1])

    cc = user["contributionsCollection"]
    calendar = cc["contributionCalendar"]
    weeks = [w["contributionDays"] for w in calendar["weeks"]]
    # The calendar starts on a Sunday, so it holds up to 371 days; the card covers a year.
    cutoff = (now.date() - dt.timedelta(days=364)).isoformat()
    days = [d for week in weeks for d in week if d["date"] >= cutoff]
    streak = longest_streak(fetch_history(token, user["createdAt"], now))
    metrics = {
        "stars": sum(r["stars"] for r in rows),
        "forks": sum(r["forks"] for r in rows),
        "watchers": sum(r["watchers"] for r in rows),
        "projects": len(rows),
        "clones": sum(r["clones"] for r in rows),
        "views": sum(r["views"] for r in rows),
        "contributions": calendar["totalContributions"],
        "commits": sum(r["commits"] for r in rows),
        "prs": user["pullRequests"]["totalCount"],
        "issues": user["issues"]["totalCount"],
        "since": since,
        "updated": now.strftime("%d %b %Y %H:%M UTC").lstrip("0"),
    }

    for name, theme in THEMES.items():
        write(out_dir / f"stats-{name}.svg", stats_card(metrics, theme))
        write(out_dir / f"languages-{name}.svg", languages_card(list(langs), theme))
        write(out_dir / f"projects-{name}.svg", projects_card(rows, since, theme))
        write(out_dir / f"education-{name}.svg", education_card(theme))
        write(out_dir / f"activity-{name}.svg", activity_card(days, streak, theme))
        write(out_dir / f"snake-{name}.svg", snake_svg(weeks, theme))

    since_label = f"since {fmt_date(since)}" if since else "tracking soon"
    badges = {
        "stars": badge("total stars", num(metrics["stars"]), "f5b301", "github"),
        "forks": badge("total forks", num(metrics["forks"]), "8250df", "github"),
        "watchers": badge("watchers", num(metrics["watchers"]), "0969da", "github"),
        "projects": badge("projects", num(metrics["projects"]), "2ea44f", "github"),
        "clones": badge(f"clones {since_label}", num(metrics["clones"]), "1f6feb", "git"),
        "views": badge(f"project views {since_label}", num(metrics["views"]), "0e8a16", "github"),
        "contributions": badge(
            "contributions (last 12 months)", num(metrics["contributions"]), "39d353", "github"
        ),
        "updated": badge("stats updated", now.strftime("%d %b %Y").lstrip("0"), "6e7681"),
    }
    for r in rows:
        badges[f"clones-{r['name']}"] = badge(f"clones {since_label}", num(r["clones"]), "1f6feb", "git")
        badges[f"views-{r['name']}"] = badge(f"views {since_label}", num(r["views"]), "0e8a16")
    for key, value in badges.items():
        write(out_dir / "badges" / f"{key}.json", json.dumps(value))

    # Byte must only repeat what anyone can see: the public profile, public repositories and the
    # numbers the README already publishes. So the profile is read without a token, which hides
    # anything only the owner could see, such as a private e-mail address.
    req = urllib.request.Request(f"{API}/users/{USER}", headers={"User-Agent": f"{USER}-profile-readme"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        account = json.loads(resp.read().decode())
    total_size = sum(size for _, size, _ in langs) or 1
    summary = {
        "updated": now.isoformat(timespec="seconds"),
        "user": {
            "login": USER,
            "name": account.get("name") or USER,
            "bio": " ".join((account.get("bio") or "").split()),
            "location": account.get("location") or "",
            "blog": account.get("blog") or "",
            "followers": account.get("followers", 0),
            "following": account.get("following", 0),
            "publicRepos": account.get("public_repos", 0),
            "joined": user["createdAt"],
        },
        "totals": {k: metrics[k] for k in ("stars", "forks", "watchers", "projects", "clones", "views", "commits")},
        "trafficSince": since,
        "contributions": {
            "lastYear": sum(d["contributionCount"] for d in days),
            "activeDays": sum(1 for d in days if d["contributionCount"]),
            "longestStreak": {"days": streak[0], "from": streak[1], "to": streak[2]},
        },
        "languages": [
            {"name": n, "percent": round(100 * size / total_size, 1)} for n, size, _ in langs[:8]
        ],
        "projects": rows,
        "education": [
            {"school": school, "city": city, "qualification": q, "year": year}
            for _, school, city, q, year in EDUCATION
        ],
    }
    write(out_dir / SUMMARY, json.dumps(summary, indent=2))

    print(
        f"{metrics['projects']} projects, {metrics['stars']} stars, {metrics['forks']} forks, "
        f"{metrics['clones']} clones and {metrics['views']} views {since_label}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
