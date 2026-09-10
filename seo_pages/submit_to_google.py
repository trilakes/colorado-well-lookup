"""Step 3: tell Google about the new pages.

  1. Re-submits sitemap.xml to Search Console (sc-domain:coloradowell.com).
  2. Pushes URLs through the Indexing API (URL_UPDATED), highest-value first,
     up to the daily quota. Keeps a ledger so re-running tomorrow continues
     where it left off instead of re-submitting the same URLs.

    python submit_to_google.py            # sitemap + up to 200 URLs
    python submit_to_google.py --max 50   # smaller batch

Uses the OAuth refresh token in Master API Keys/_gsc_token.json (same one the
foremanai GSC script uses). Read-only against the site; only talks to Google.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
SITE_DIR = HERE.parent
KEYS = Path(r"C:\Users\17192\OneDrive\Desktop\Master API Keys")
TOKEN_FILE = KEYS / "_gsc_token.json"
LEDGER = HERE / "data" / "indexing_ledger.json"
SITE = "sc-domain:coloradowell.com"
BASE = "https://coloradowell.com"
GSC = "https://www.googleapis.com/webmasters/v3"
IDX = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def env() -> dict[str, str]:
    d: dict[str, str] = {}
    for raw in (KEYS / "MASTER_API_KEYS.env").read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def access_token() -> str:
    e = env()
    tok = json.loads(TOKEN_FILE.read_text())
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": e["GOOGLE_CLIENT_ID"], "client_secret": e["GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, timeout=30)
    r.raise_for_status()
    TOKEN_FILE.write_text(json.dumps({**tok, **r.json()}))
    return r.json()["access_token"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=200)
    a = ap.parse_args()
    H = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"}

    # 1. sitemap
    live = requests.get(f"{BASE}/sitemap.xml", timeout=30)
    n_urls = live.text.count("<loc>")
    print(f"live sitemap: HTTP {live.status_code}, {n_urls} URLs")
    sm = requests.put(f"{GSC}/sites/{requests.utils.quote(SITE, safe='')}/sitemaps/{requests.utils.quote(BASE + '/sitemap.xml', safe='')}",
                      headers=H, timeout=30)
    print(f"sitemap submit: HTTP {sm.status_code} {sm.text[:120]}")
    lst = requests.get(f"{GSC}/sites/{requests.utils.quote(SITE, safe='')}/sitemaps", headers=H, timeout=30).json()
    for s in lst.get("sitemap", []):
        print(f"  {s.get('path')}  lastSubmitted={s.get('lastSubmitted','')[:19]}  pending={s.get('isPending')}  errors={s.get('errors')}")

    # 2. indexing API, priority order
    pages = json.loads((HERE / "data" / "stats.json").read_text(encoding="utf-8"))
    on_disk = {p["slug"] for p in pages if (SITE_DIR / "wells" / p["slug"] / "index.html").exists()}
    counties = sorted([p for p in pages if p["kind"] == "county" and p["slug"] in on_disk], key=lambda p: -p["wells_total"])
    places = sorted([p for p in pages if p["kind"] == "place" and p["slug"] in on_disk], key=lambda p: -p["wells_total"])
    urls = [f"{BASE}/wells/"] + [f"{BASE}/wells/{p['slug']}/" for p in counties + places]
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    todo = [u for u in urls if u not in ledger][: a.max]
    print(f"\nindexing API: {len(urls)} URLs total, {len(ledger)} already submitted, sending {len(todo)} now")
    ok = fail = 0
    for u in todo:
        chk = requests.head(u, timeout=20, allow_redirects=True)
        if chk.status_code != 200:
            print(f"  skip {u} (live HTTP {chk.status_code})")
            continue
        r = requests.post(IDX, headers=H, json={"url": u, "type": "URL_UPDATED"}, timeout=30)
        if r.status_code == 200:
            ok += 1
            ledger[u] = date.today().isoformat()
        else:
            fail += 1
            msg = r.json().get("error", {}).get("message", r.text[:100])
            print(f"  FAIL {u}: HTTP {r.status_code} {msg[:120]}")
            if r.status_code == 429:
                print("  daily quota hit; run again tomorrow")
                break
        time.sleep(0.25)
    LEDGER.write_text(json.dumps(ledger, indent=1))
    print(f"indexing API: ok={ok} fail={fail}; ledger now {len(ledger)} URLs. Remaining: {len(urls) - len(ledger)}")


if __name__ == "__main__":
    main()
