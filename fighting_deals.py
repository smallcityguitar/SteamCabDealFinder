#!/usr/bin/env python3
"""
fighting_deals.py

Checks Steam for fighting games on sale at or above a discount threshold,
and pushes an ntfy notification for each new/changed deal.

Steam has no official "search by tag+discount" JSON API, so this uses the
same public search endpoint the Steam store website itself calls
(store.steampowered.com/search/results/), filtered to the "Fighting" tag
(tag id 1743) and specials only. No API key or login required.

Run it however you like: a cron job, a systemd timer, or just manually.
See the bottom of this file / README for a systemd timer example that
works well on a Steam Deck (SteamOS).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config - edit these or override via command-line flags / config.json
# ---------------------------------------------------------------------------

DEFAULTS = {
    # ntfy topic to publish to. Anyone who knows the topic name can read it
    # unless you put it behind auth, so pick something unguessable, e.g.
    # "will-arcade-deals-8f2a".
    "ntfy_topic": "steam_cab_deal_finder",
    # Use "https://ntfy.sh" for the public service, or your own server URL
    # (e.g. "https://ntfy.example.com") if you self-host.
    "ntfy_server": "https://ntfy.sh",
    # Minimum discount percentage (as a positive integer) to notify about.
    "min_discount": 60,
    # Steam "Fighting" tag id. (Verified via SteamDB/steam250 tag listing.)
    "steam_tag_id": 1743,
    # Steam country/region code - affects pricing and which regional sales apply.
    "country_code": "us",
    # Where to remember which deals we've already notified about, so we
    # don't spam the same discount every run. Deleting this file resets state.
    # Kept relative (not in $HOME) so the GitHub Actions workflow can commit
    # it back to the repo between runs; override with STATE_FILE if needed.
    "state_file": str(Path(__file__).with_name("state.json")),
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

def fetch_fighting_specials(cfg: dict) -> list[dict]:
    """Query Steam's search endpoint for discounted games tagged Fighting.

    Returns a list of dicts: {appid, name, discount_pct, final_price, url}
    """
    results = []
    start = 0
    page_size = 50

    while start < cfg["max_results"]:
        params = {
            "query": "",
            "start": start,
            "count": page_size,
            "specials": 1,             # on sale only
            "tags": cfg["steam_tag_id"],  # Fighting
            "category1": 998,          # Games (excludes DLC/software/etc.)
            "infinite": 1,
            "cc": cfg["country_code"],
            "l": "english",
        }
        resp = requests.get(
            SEARCH_URL, params=params, headers=HEADERS, timeout=20
        )
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

            price_el = row.select_one(".discount_final_price")
            final_price = price_el.get_text(strip=True) if price_el else "?"

            results.append(
                {
                    "appid": appid,
                    "name": name,
                    "discount_pct": discount_pct,
                    "final_price": final_price,
                    "url": f"https://store.steampowered.com/app/{appid}/",
                }
            )

        total = int(data.get("total_count", 0))
        start += page_size
        if start >= total:
            break

    return results


# ---------------------------------------------------------------------------
# State (avoid re-notifying the same deal every run)
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
# ntfy
# ---------------------------------------------------------------------------

def send_ntfy(cfg: dict, game: dict) -> None:
    url = f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}"
    title = f"-{game['discount_pct']}% {game['name']}"
    message = f"Now {game['final_price']} on Steam.\n{game['url']}"

    resp = requests.post(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Click": game["url"],
            "Tags": "fire,video_game",
            "Priority": "default",
        },
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_config() -> dict:
    import os

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
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        # GitHub Actions sets an env var to "" (not unset) when the
        # underlying secret doesn't exist, so treat blank as not-provided.
        if val:
            if cfg_key == "min_discount":
                val = int(val)
            cfg[cfg_key] = val

    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-discount", type=int, help="Override min discount %%")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would be notified, but don't call ntfy")
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

    try:
        games = fetch_fighting_specials(cfg)
    except requests.RequestException as e:
        print(f"Error fetching Steam data: {e}", file=sys.stderr)
        return 1

    qualifying = [g for g in games if g["discount_pct"] >= cfg["min_discount"]]

    state = load_state(cfg["state_file"])
    notified = 0

    for game in qualifying:
        prev_discount = state.get(game["appid"])
        # Notify if we've never seen this deal, or the discount got deeper.
        if prev_discount is None or game["discount_pct"] > prev_discount:
            if args.dry_run:
                print(f"[DRY RUN] Would notify: -{game['discount_pct']}% "
                      f"{game['name']} ({game['final_price']}) {game['url']}")
            else:
                send_ntfy(cfg, game)
                print(f"Notified: -{game['discount_pct']}% {game['name']}")
            notified += 1
        state[game["appid"]] = game["discount_pct"]

    # Drop games from state that are no longer on sale at/above threshold,
    # so if a deal reappears later it notifies again.
    current_ids = {g["appid"] for g in qualifying}
    state = {k: v for k, v in state.items() if k in current_ids}

    if not args.dry_run:
        save_state(cfg["state_file"], state)

    print(f"Checked {len(games)} fighting-game specials, "
          f"{len(qualifying)} at >= {cfg['min_discount']}%, "
          f"{notified} new notification(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
