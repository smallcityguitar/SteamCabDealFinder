#!/usr/bin/env python3
"""
fighting_deals.py

Checks Steam for fighting games on sale at or above a discount threshold,
and pushes ONE summary ntfy notification (not one per game) when the set of
qualifying deals changes. The notification links to a small generated page
(docs/index.html) listing every current deal, sortable by discount, price,
review score, and review count - meant to be served via GitHub Pages.

Steam has no official "search by tag+discount" JSON API, so this uses the
same public search endpoint the Steam store website itself calls
(store.steampowered.com/search/results/), filtered to the "Fighting" tag
(tag id 1743) and specials only. No API key or login required.

Run it however you like: a cron job, a systemd timer, or just manually.
See the README for a systemd timer example (SteamOS) and for the GitHub
Actions + GitHub Pages setup this is primarily designed around.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config - edit these or override via command-line flags / config.json
# ---------------------------------------------------------------------------

DEFAULTS = {
    # ntfy topic to publish to. Anyone who knows the topic name can read it
    # unless you put it behind auth, so pick something unguessable, e.g.
    # "will-arcade-deals-8f2a".
    "ntfy_topic": "CHANGE_ME_arcade_deals",
    # Use "https://ntfy.sh" for the public service, or your own server URL
    # (e.g. "https://ntfy.example.com") if you self-host.
    "ntfy_server": "https://ntfy.sh",
    # Minimum discount percentage (as a positive integer) to notify about.
    "min_discount": 60,
    # Steam "Fighting" tag id. (Verified via SteamDB/steam250 tag listing.)
    "steam_tag_id": 1743,
    # Steam country/region code - affects pricing and which regional sales apply.
    "country_code": "us",
    # Where to remember the last-notified set of deals, so we don't spam on
    # every run. Kept relative (not in $HOME) so the GitHub Actions workflow
    # can commit it back to the repo between runs.
    "state_file": str(Path(__file__).with_name("state.json")),
    # Where to write the generated results page + data for GitHub Pages.
    "docs_dir": str(Path(__file__).with_name("docs")),
    # The public URL the notification should link to. If not set, this is
    # built from the GITHUB_REPOSITORY env var GitHub Actions provides
    # (assumes Pages is set to serve from main /docs).
    "pages_url": "",
    # Max results to pull per run (Steam paginates in chunks of 50).
    "max_results": 100,
}

SEARCH_URL = "https://store.steampowered.com/search/results/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Steam search
# ---------------------------------------------------------------------------

def _parse_review_summary(row) -> dict:
    """Pull rating label / % positive / review count out of the review
    tooltip Steam embeds in each search result row, if present."""
    summary = {"review_label": None, "review_pct": None, "review_count": 0}

    el = row.select_one(".search_review_summary")
    tooltip_html = el.get("data-tooltip-html") if el else None
    if not tooltip_html:
        return summary

    text = BeautifulSoup(tooltip_html, "html.parser").get_text(" ", strip=True)
    m = re.match(r"^(.*?)\s*(\d+)%\s+of the\s+([\d,]+)\s+user reviews", text)
    if m:
        summary["review_label"] = m.group(1).strip() or None
        summary["review_pct"] = int(m.group(2))
        summary["review_count"] = int(m.group(3).replace(",", ""))
    return summary


def fetch_fighting_specials(cfg: dict) -> list[dict]:
    """Query Steam's search endpoint for discounted games tagged Fighting."""
    results = []
    start = 0
    page_size = 50

    while start < cfg["max_results"]:
        params = {
            "query": "",
            "start": start,
            "count": page_size,
            "specials": 1,                 # on sale only
            "tags": cfg["steam_tag_id"],    # Fighting
            "category1": 998,               # Games (excludes DLC/software/etc.)
            "infinite": 1,
            "cc": cfg["country_code"],
            "l": "english",
        }
        resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        html = data.get("results_html", "")
        if not html.strip():
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("a.search_result_row")
        if not rows:
            break

        for row in rows:
            appid = row.get("data-ds-appid")
            if not appid:
                continue

            name_el = row.select_one(".title")
            name = name_el.get_text(strip=True) if name_el else "Unknown title"

            discount_el = row.select_one(".discount_pct")
            discount_text = discount_el.get_text(strip=True) if discount_el else ""
            m = re.search(r"-?(\d+)%", discount_text)
            discount_pct = int(m.group(1)) if m else 0

            final_price_el = row.select_one(".discount_final_price")
            final_price = final_price_el.get_text(strip=True) if final_price_el else "?"

            orig_price_el = row.select_one(".discount_original_price")
            original_price = orig_price_el.get_text(strip=True) if orig_price_el else final_price

            review = _parse_review_summary(row)

            results.append(
                {
                    "appid": appid,
                    "name": name,
                    "discount_pct": discount_pct,
                    "final_price": final_price,
                    "original_price": original_price,
                    "url": f"https://store.steampowered.com/app/{appid}/",
                    "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
                    **review,
                }
            )

        total = int(data.get("total_count", 0))
        start += page_size
        if start >= total:
            break

    return results


# ---------------------------------------------------------------------------
# Secondary verification: Steam's search tag filter matches ANY tag a game
# has, even one applied by a handful of users and buried in a long list
# (e.g. Star Wars Jedi: Fallen Order and Ghostrunner both turn up under the
# "Fighting" tag despite being action-adventure games). Worse, plain
# "Fighting" is itself too broad even when genuinely a top tag - For Honor
# is legitimately tagged "Fighting" prominently but is a third-person melee
# action game, not a traditional fighter. So instead of checking for
# "Fighting" at all, check each *candidate's* own store page for the more
# precise sub-genre tags ("2D Fighter" / "3D Fighter") among its top,
# prominently-displayed tags - the ones Steam shows directly under the
# game's description. Verified against a reference list of ~18 traditional
# fighting games (Street Fighter 6, Tekken 8, Guilty Gear, BlazBlue, etc.)
# which all carry one of these two tags; For Honor, Jedi: Fallen Order, and
# Ghostrunner carry neither.
# ---------------------------------------------------------------------------

FIGHTING_VERIFY_TAGS = {"2D Fighter", "3D Fighter"}
VERIFY_TOP_N_TAGS = 15

# Cookies to skip the mature-content interstitial some game pages show,
# which would otherwise hide the tag list behind an age-check page.
AGE_GATE_COOKIES = {
    "birthtime": "0",
    "lastagecheckage": "1-January-1970",
    "wants_mature_content": "1",
}


def verify_is_fighting_game(appid: str):
    """Return True/False if we could confirm one way or the other, or None
    if the check itself failed (e.g. network error) - callers should treat
    None as "keep it, we just don't know" rather than as a rejection."""
    try:
        resp = requests.get(
            f"https://store.steampowered.com/app/{appid}/",
            headers=HEADERS,
            cookies=AGE_GATE_COOKIES,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    tag_container = soup.select_one(".glance_tags.popular_tags") or soup.select_one(".glance_tags")
    if not tag_container:
        return None
    tag_els = tag_container.select("a.app_tag") or tag_container.select("a")
    if not tag_els:
        return None

    top_tags = [t.get_text(strip=True) for t in tag_els[:VERIFY_TOP_N_TAGS]]
    return any(t in FIGHTING_VERIFY_TAGS for t in top_tags)


def filter_to_genuine_fighting_games(games: list[dict]) -> list[dict]:
    """Runs the secondary per-game check above over a (already small,
    discount-filtered) candidate list."""
    verified = []
    for g in games:
        result = verify_is_fighting_game(g["appid"])
        time.sleep(0.4)  # be polite to Steam's servers
        if result is False:
            print(f"  Dropping '{g['name']}' - not actually tagged as a fighting game")
            continue
        verified.append(g)
    return verified


# ---------------------------------------------------------------------------
# State (avoid re-notifying when nothing's changed)
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# ntfy - one summary notification, not one per game
# ---------------------------------------------------------------------------

def send_ntfy_summary(cfg: dict, qualifying: list[dict], pages_url: str) -> None:
    url = f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}"
    count = len(qualifying)
    title = f"{count} fighting game{'s' if count != 1 else ''} \u2265{cfg['min_discount']}% off"

    top = sorted(qualifying, key=lambda g: -g["discount_pct"])[:5]
    lines = [f"-{g['discount_pct']}% {g['name']} ({g['final_price']})" for g in top]
    if count > len(top):
        lines.append(f"...and {count - len(top)} more")
    message = "\n".join(lines) if lines else "No qualifying deals right now."

    headers = {
        # See earlier note: bytes here bypasses Python's Latin-1-only header
        # encoding, since game titles can contain non-Latin-1 characters.
        "Title": title.encode("utf-8"),
        "Tags": "fire,video_game",
        "Priority": "default",
    }
    if pages_url:
        headers["Click"] = pages_url

    resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Results page (for GitHub Pages)
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fighting Game Deals</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0f1115; color: #e6e6e6;
         margin: 0; padding: 16px; }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  #updated { color: #9aa0a6; font-size: 0.85rem; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #2a2d34; }
  th { cursor: pointer; user-select: none; color: #9aa0a6; font-weight: 600;
       white-space: nowrap; position: sticky; top: 0; background: #0f1115; }
  th.sorted::after { content: " \\25BE"; }
  th.sorted.asc::after { content: " \\25B4"; }
  tr:hover { background: #171a20; }
  a { color: #6cb6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .discount { color: #7ee787; font-weight: 700; }
  .orig { color: #9aa0a6; text-decoration: line-through; margin-right: 6px; }
  .empty { color: #9aa0a6; padding: 24px 8px; }
  .table-wrap { overflow-x: auto; }
  .game-cell { display: flex; align-items: center; gap: 10px; }
  .thumb { width: 120px; height: 56px; object-fit: cover; border-radius: 4px;
           flex-shrink: 0; background: #1c1f26; }
</style>
</head>
<body>
<h1>Fighting Game Deals</h1>
<div id="updated"></div>
<div class="table-wrap">
<table id="deals">
  <thead>
    <tr>
      <th data-key="name">Game</th>
      <th data-key="discount_pct">Discount</th>
      <th data-key="final_price">Price</th>
      <th data-key="review_pct">Rating</th>
      <th data-key="review_count">Reviews</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
<div class="empty" id="empty" hidden>No qualifying deals right now.</div>
</div>
<script>
let deals = [];
let sortKey = "review_count";
let sortAsc = false;

function render() {
  const tbody = document.querySelector("#deals tbody");
  const empty = document.getElementById("empty");
  const sorted = [...deals].sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (av == null) av = sortKey === "name" ? "" : -1;
    if (bv == null) bv = sortKey === "name" ? "" : -1;
    if (typeof av === "string") { av = av.toLowerCase(); bv = bv.toLowerCase(); }
    if (av < bv) return sortAsc ? -1 : 1;
    if (av > bv) return sortAsc ? 1 : -1;
    return 0;
  });

  tbody.innerHTML = "";
  empty.hidden = sorted.length > 0;

  for (const g of sorted) {
    const tr = document.createElement("tr");
    const rating = g.review_pct != null
      ? `${g.review_pct}% <span style="color:#9aa0a6">(${g.review_label || ""})</span>`
      : "\u2014";
    const reviewCount = g.review_count ? g.review_count.toLocaleString() : "\u2014";
    tr.innerHTML = `
      <td>
        <div class="game-cell">
          <img class="thumb" src="${g.header_image || ""}" alt="" loading="lazy"
               onerror="this.style.display='none'">
          <a href="${g.url}" target="_blank" rel="noopener">${g.name}</a>
        </div>
      </td>
      <td class="discount">-${g.discount_pct}%</td>
      <td><span class="orig">${g.original_price}</span>${g.final_price}</td>
      <td>${rating}</td>
      <td>${reviewCount}</td>
    `;
    tbody.appendChild(tr);
  }

  document.querySelectorAll("th[data-key]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.key === sortKey);
    th.classList.toggle("asc", th.dataset.key === sortKey && sortAsc);
  });
}

document.querySelectorAll("th[data-key]").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (sortKey === key) { sortAsc = !sortAsc; }
    else { sortKey = key; sortAsc = key === "name"; }
    render();
  });
});

fetch("deals.json")
  .then(r => r.json())
  .then(data => {
    deals = data.deals || [];
    document.getElementById("updated").textContent =
      `Updated ${new Date(data.updated_at).toLocaleString()} \u00b7 ${deals.length} deal(s) \u2265${data.min_discount}% off`;
    render();
  })
  .catch(() => {
    document.getElementById("updated").textContent = "Couldn't load deals.json";
  });
</script>
</body>
</html>
"""


def write_pages(docs_dir: str, qualifying: list[dict], min_discount: int) -> None:
    import datetime

    out_dir = Path(docs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "index.html").write_text(PAGE_TEMPLATE)

    payload = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "min_discount": min_discount,
        "deals": qualifying,
    }
    (out_dir / "deals.json").write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Pending-notification handoff. --generate-only writes this instead of
# sending the ntfy push immediately, so a later --send-pending step (after
# the GitHub Pages deploy has had time to finish) can send it once the page
# is actually live. Purely a same-job handoff file; never committed to git.
# ---------------------------------------------------------------------------

def pending_notification_path() -> Path:
    return Path(__file__).with_name("pending_notification.json")


def save_pending_notification(qualifying: list[dict], pages_url: str, min_discount: int) -> None:
    payload = {"qualifying": qualifying, "pages_url": pages_url, "min_discount": min_discount}
    pending_notification_path().write_text(json.dumps(payload))


def send_pending_notification(cfg: dict, dry_run: bool = False) -> int:
    path = pending_notification_path()
    if not path.exists():
        print("No pending notification to send.")
        return 0

    payload = json.loads(path.read_text())
    qualifying = payload["qualifying"]
    pages_url = payload.get("pages_url") or cfg["pages_url"]

    if dry_run:
        print(f"[DRY RUN] Would send pending summary notification for {len(qualifying)} deal(s):")
        for g in qualifying:
            print(f"  -{g['discount_pct']}% {g['name']} ({g['final_price']})")
        return 0

    send_ntfy_summary(cfg, qualifying, pages_url)
    path.unlink()
    print(f"Sent pending notification for {len(qualifying)} deal(s).")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    config_path = Path(__file__).with_name("config.json")
    if config_path.exists():
        cfg.update(json.loads(config_path.read_text()))

    # Environment variables take precedence over config.json, so secrets
    # (like the ntfy topic) never have to live in a committed file - this
    # is how the GitHub Actions workflow passes them in.
    env_map = {
        "NTFY_TOPIC": "ntfy_topic",
        "NTFY_SERVER": "ntfy_server",
        "MIN_DISCOUNT": "min_discount",
        "COUNTRY_CODE": "country_code",
        "STATE_FILE": "state_file",
        "DOCS_DIR": "docs_dir",
        "PAGES_URL": "pages_url",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        # GitHub Actions sets an env var to "" (not unset) when the
        # underlying secret doesn't exist, so treat blank as not-provided.
        if val:
            if cfg_key == "min_discount":
                val = int(val)
            cfg[cfg_key] = val

    if not cfg["pages_url"]:
        repo = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo"
        if repo and "/" in repo:
            owner, name = repo.split("/", 1)
            cfg["pages_url"] = f"https://{owner}.github.io/{name}/"

    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-discount", type=int, help="Override min discount %%")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would happen, but don't call ntfy or write files")
    parser.add_argument("--generate-only", action="store_true",
                         help="Fetch/filter and write docs/+state, but don't send ntfy yet - "
                              "instead save a pending notification for a later --send-pending run")
    parser.add_argument("--send-pending", action="store_true",
                         help="Send the notification saved by an earlier --generate-only run, if any")
    args = parser.parse_args()

    cfg = load_config()
    if args.min_discount is not None:
        cfg["min_discount"] = args.min_discount

    if cfg["ntfy_topic"] == "CHANGE_ME_arcade_deals" and not args.dry_run:
        print(
            "Set your ntfy topic in config.json (see config.example.json) "
            "before running for real, or use --dry-run.",
            file=sys.stderr,
        )
        return 1

    if args.send_pending:
        return send_pending_notification(cfg, dry_run=args.dry_run)

    try:
        games = fetch_fighting_specials(cfg)
    except requests.RequestException as e:
        print(f"Error fetching Steam data: {e}", file=sys.stderr)
        return 1

    qualifying = sorted(
        (g for g in games if g["discount_pct"] >= cfg["min_discount"]),
        key=lambda g: -g["discount_pct"],
    )
    if qualifying:
        qualifying = filter_to_genuine_fighting_games(qualifying)
    current_ids = sorted(g["appid"] for g in qualifying)

    state = load_state(cfg["state_file"])
    previously_notified_ids = state.get("qualifying_ids", [])

    changed = current_ids != previously_notified_ids
    sent = False
    deferred = False

    if changed and qualifying:
        if args.dry_run:
            print(f"[DRY RUN] Would send summary notification for {len(qualifying)} deal(s):")
            for g in qualifying:
                print(f"  -{g['discount_pct']}% {g['name']} ({g['final_price']})")
        elif args.generate_only:
            save_pending_notification(qualifying, cfg["pages_url"], cfg["min_discount"])
            deferred = True
        else:
            send_ntfy_summary(cfg, qualifying, cfg["pages_url"])
            sent = True

    if not args.dry_run:
        write_pages(cfg["docs_dir"], qualifying, cfg["min_discount"])
        save_state(cfg["state_file"], {"qualifying_ids": current_ids})

    if deferred:
        status = "notification deferred (run --send-pending after the page deploys)"
    elif sent:
        status = "notification sent"
    else:
        status = "no notification (unchanged or none qualifying)"
    print(f"Checked {len(games)} fighting-game specials, "
          f"{len(qualifying)} at >= {cfg['min_discount']}%, {status}.")
    if cfg["pages_url"]:
        print(f"Results page: {cfg['pages_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
