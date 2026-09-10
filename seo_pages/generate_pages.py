"""Step 2 of the programmatic SEO build: write + render one page per county/place.

    python generate_pages.py --only teller-county florissant-co     # test a few
    python generate_pages.py --limit 20                             # first N by well count
    python generate_pages.py                                        # everything

Reads  seo_pages/data/stats.json        (from compute_stats.py)
Caches seo_pages/data/ai/<slug>.json    (AI copy; re-runs never re-pay for a cached page)
Writes wells/<slug>/index.html          (the live page tree, committed with the site)
       wells/index.html                 (hub page linking every county + town)
       sitemap.xml                      (regenerated with every URL)

The AI is only allowed to write around the numbers in stats.json. Every figure on
the page comes from the Colorado DWR permit records in the wells database.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
SITE = HERE.parent
DATA = HERE / "data"
AI_DIR = DATA / "ai"
AI_DIR.mkdir(parents=True, exist_ok=True)
OUT_ROOT = SITE / "wells"
ENV = Path(r"C:\Users\17192\OneDrive\Desktop\Master API Keys\MASTER_API_KEYS.env")
MODEL = "gpt-5.6-luna"
FALLBACK_MODEL = "gpt-5-mini"
MIN_PLACE_WELLS = 100
BASE_URL = "https://coloradowell.com"
LEAD_ENDPOINT = "https://trilakes.co/portal/api/build-feasibility"   # -> portal Leads page + email to kyle@trilakes.co + SMS
HQ_LEAD_ENDPOINT = "https://trilakeshq.com/api/lead"
TODAY = date.today().isoformat()
_print_lock = threading.Lock()


def env() -> dict[str, str]:
    d: dict[str, str] = {}
    for raw in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


OPENAI_KEY = env()["OPENAI_API_KEY"]

# ----------------------------------------------------------------------------- facts -> prompt
def pct(x):
    return f"{round(x * 100)}%" if x is not None else "n/a"


def money(n):
    return f"${n:,.0f}"


def facts_for(r: dict) -> str:
    """Plain-English fact sheet the model must stay inside."""
    is_county = r["kind"] == "county"
    where = r["name"] if is_county else f"{r['name']} in {r['county_name']}"
    scope = (f"all permitted wells in {r['name']}" if is_county
             else f"permitted wells within {r['radius_miles']} miles of {r['name']}, Colorado ({r['county_name']})")
    L = [f"PLACE: {where}, Colorado. Region type: {r['region']}. Data scope: {scope}.",
         f"Wells in Colorado DWR permit records: {r['wells_total']:,} total; {r['wells_constructed']:,} recorded as constructed; "
         f"{r['wells_with_depth']:,} report a total depth; {r['wells_with_yield']:,} report a pump yield; {r['wells_with_swl']:,} report a static water level."]
    if r["depth_median"]:
        L.append(f"Depth (ft): median {r['depth_median']}, middle half {r['depth_p25']}-{r['depth_p75']}, 90th percentile {r['depth_p90']}, deepest on record {r['depth_max']}.")
        b = r["depth_bands"]
        L.append(f"Depth mix: under 100 ft {pct(b.get('under_100'))}; 100-300 ft {pct(b.get('100_300'))}; 300-600 ft {pct(b.get('300_600'))}; 600+ ft {pct(b.get('over_600'))}.")
    if r["yield_median"] is not None:
        y = r["yield_bands"]
        L.append(f"Yield (gpm): median {r['yield_median']}, middle half {r['yield_p25']}-{r['yield_p75']}. Under 1 gpm {pct(y.get('under_1gpm'))}; 1-5 gpm {pct(y.get('1_5gpm'))}; 5-15 gpm {pct(y.get('5_15gpm'))}; 15+ gpm {pct(y.get('over_15gpm'))}.")
    if r["swl_median"]:
        L.append(f"Static water level (depth to water, ft): median {r['swl_median']}, middle half {r['swl_p25']}-{r['swl_p75']}.")
    if len(r["swl_trend_by_decade"]) >= 2:
        L.append("Median static water level by decade of measurement (ft below surface): " + ", ".join(f"{k}s: {v}" for k, v in sorted(r["swl_trend_by_decade"].items())) + ".")
    if r["permits_by_decade"]:
        L.append("Permits issued by decade: " + ", ".join(f"{k}s: {v:,}" for k, v in sorted(r["permits_by_decade"].items())) + f". Newest permit year: {r['newest_permit_year']}.")
    L.append(f"Use mix: domestic/household {pct(r['use_domestic_share'])}, stock {pct(r['use_stock_share'])}, irrigation {pct(r['use_irrigation_share'])}, monitoring {pct(r['use_monitoring_share'])}.")
    if r["aquifers_top"]:
        L.append("Aquifers named on permits: " + ", ".join(f"{a} ({n:,} wells)" for a, n in r["aquifers_top"]) + f". Denver Basin aquifer wells: {pct(r['denver_basin_share'])}.")
    if r["top_drillers"]:
        L.append("Most active drillers in the records: " + ", ".join(f"{d} ({n})" for d, n in r["top_drillers"]) + ".")
    c = r["contamination_10mi"]
    L.append(f"Mapped environmental sites within about 10 miles: {c.get('pfas', 0)} PFAS sampling/contamination sites, {c.get('mines', 0)} historic mine features, {c.get('superfund', 0)} EPA Superfund/cleanup sites.")
    if r["cost"]:
        k = r["cost"]
        L.append(f"Drilling cost model: ground is {k['geology']}; typical rate {money(k['rate_low'])}-{money(k['rate_high'])} per foot; "
                 f"at the median depth the drilled hole is about {money(k['hole_low'])}-{money(k['hole_high'])}, and a finished well with pump, tank, trenching and electrical about {money(k['total_low'])}-{money(k['total_high'])}.")
    if is_county and r.get("places"):
        L.append("Towns and areas in this county with their own page: " + ", ".join(p["name"] for p in r["places"][:12]) + ".")
    if r.get("neighbors"):
        L.append("Nearby areas: " + ", ".join(f"{n['name']} ({n['miles']} mi)" for n in r["neighbors"]) + ".")
    return "\n".join(L)


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string"}, "meta_description": {"type": "string"}, "h1": {"type": "string"},
        "eyebrow": {"type": "string"}, "intro_html": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "properties": {"heading": {"type": "string"}, "html": {"type": "string"}},
                     "required": ["heading", "html"]}},
        "faq": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "properties": {"q": {"type": "string"}, "a": {"type": "string"}}, "required": ["q", "a"]}},
        "tri_lakes_blurb": {"type": "string"},
    },
    "required": ["title", "meta_description", "h1", "eyebrow", "intro_html", "sections", "faq", "tri_lakes_blurb"],
}

SYSTEM = """You write local reference pages for Colorado Well Finder (coloradowell.com), a site that maps every Colorado
water-well permit on record. The reader is usually someone buying rural land or a home on a well, or planning to build, and
wants to know how deep wells are, how much water they make, and what a well will cost in this specific place.

Voice: a knowledgeable Colorado well and excavation contractor explaining things plainly to a buyer. Direct, specific,
no marketing fluff, no exclamation points, no "nestled", no "boasts", no "whether you're". Short paragraphs.

HARD RULES
- Every number you use must come from the FACTS block. Do not invent depths, yields, costs, dates, counts or percentages.
- You may use general Colorado well knowledge that is not a statistic: DWR permits, exempt household-use wells, the 35 gpm
  exempt-well pumping limit, augmentation plans in over-appropriated basins, fractured-granite unpredictability, casing/grout,
  the value of checking neighboring wells before buying. Keep it accurate and brief.
- Interpret the numbers for the reader: what a 260 ft median with a 180-360 middle half means for a driller's bid; what a
  10% share under 1 gpm means for a buyer; whether the water-level-by-decade series suggests a dropping table (only if the
  facts show it). If a statistic is missing from FACTS, do not mention it.
- Name real nearby areas from FACTS for local context. Never name businesses other than the drillers listed in FACTS.
- Output HTML fragments only inside intro_html and section html: <p>, <strong>, <em>, <ul><li>. No headings inside html,
  no links, no tables, no scripts.
- Write 5 to 7 sections, 80-170 words each, with these topics in a natural order and headings written for this place
  (never generic): well depth and what drives it here; well yield and how much water to expect; water table and trend;
  who is drilling and when (permit history); what a new well costs here; risks to check (contamination, low yield, dry
  holes, septic and building considerations); advice for buying or building on a lot here.
- FAQ: exactly 5 questions a buyer would type into Google about this place ("how deep are wells in X", "cost to drill a
  well in X", "is well water safe in X", etc.), each answered in 40-80 words using the FACTS.
- title: at most 60 characters, includes the place name and one of: well depth / wells / drilling cost. meta_description:
  at most 155 characters, includes a real number from FACTS. h1: natural headline with the place name. eyebrow: 3-6 words.
- tri_lakes_blurb: one or two sentences, localized to this place, introducing Tri-Lakes Contracting as a licensed Colorado
  contractor for access driveways, site development, new-build septic systems and custom homes. No numbers.
- Make each page structurally and verbally different from a template: vary sentence openers, section order within the
  rules, and which facts you lead with."""


def call_openai(model: str, facts: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": "FACTS\n" + facts + "\n\nWrite the page."}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "well_page", "strict": True, "schema": SCHEMA}},
    }
    if model.startswith("gpt-5"):
        body["reasoning_effort"] = "low"
    r = requests.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                      json=body, timeout=240)
    if r.status_code != 200:
        raise RuntimeError(f"{model} HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    content = j["choices"][0]["message"]["content"]
    out = json.loads(content)
    out["_model"] = model
    out["_usage"] = j.get("usage", {})
    return out


def write_copy(r: dict) -> dict:
    cache = AI_DIR / f"{r['slug']}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    facts = facts_for(r)
    last = None
    for attempt, model in enumerate((MODEL, MODEL, FALLBACK_MODEL)):
        try:
            out = call_openai(model, facts)
            if len(out["sections"]) < 4 or len(out["faq"]) < 3:
                raise RuntimeError("too few sections/faq")
            cache.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"AI failed for {r['slug']}: {last}")


# ----------------------------------------------------------------------------- rendering
BASE_CSS = re.search(r"<style>(.*?)</style>", (SITE / "well-drilling-cost-colorado" / "index.html").read_text(encoding="utf-8"), re.S).group(1)

EXTRA_CSS = """
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0 8px}
.stat{background:rgba(15,23,42,.6);border:1px solid rgba(148,163,184,.18);border-radius:12px;padding:14px 14px 12px}
.stat b{display:block;font-size:1.55rem;color:#e6c76b;line-height:1.1;letter-spacing:-.01em}
.stat span{display:block;color:#94a3b8;font-size:.82rem;margin-top:4px;line-height:1.3}
.bands{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:10px 0 6px;background:rgba(148,163,184,.12)}
.bands i{display:block;height:100%}
.bands-key{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:.82rem;color:#94a3b8;margin-bottom:14px}
.bands-key em{font-style:normal;color:#e2e8f0}
.k1{background:#7dd3fc}.k2{background:#38bdf8}.k3{background:#0ea5e9}.k4{background:#0369a1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:700px){.two{grid-template-columns:1fr}}
.mini{width:100%;border-collapse:collapse;font-size:.9rem}
.mini th,.mini td{padding:7px 8px;border-bottom:1px solid rgba(148,163,184,.14);text-align:left}
.mini th{color:#94a3b8;font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.mini td:last-child,.mini th:last-child{text-align:right}
.tl{margin:34px 0;padding:26px 24px;border-radius:16px;border:1px solid rgba(230,199,107,.35);background:linear-gradient(135deg,rgba(230,199,107,.10),rgba(15,23,42,.4))}
.tl h2{margin:0 0 6px;font-size:1.35rem}.tl .who{color:#e6c76b;font-size:.85rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.tl p{color:#cbd5e1;margin:8px 0}.tl ul{list-style:none;padding:0;margin:12px 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px 14px}
.tl ul li{color:#e2e8f0;padding-left:22px;position:relative;font-size:.95rem}.tl ul li::before{content:"\\2713";position:absolute;left:0;color:#e6c76b;font-weight:700}
.tl .lic{font-size:.85rem;color:#94a3b8;margin-top:6px}
.tl .btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.tl a.b1,.tl a.b2{display:inline-block;padding:11px 18px;border-radius:9px;font-weight:700;text-decoration:none;font-size:.95rem}
.tl a.b1{background:linear-gradient(135deg,#e6c76b,#b8901f);color:#0f172a}.tl a.b2{border:1px solid rgba(230,199,107,.5);color:#e6c76b}
.lead{margin:26px 0 34px;padding:24px;border-radius:16px;background:rgba(15,23,42,.65);border:1px solid rgba(148,163,184,.2)}
.lead h2{margin:0 0 4px;font-size:1.3rem}.lead p.sub{color:#94a3b8;margin:0 0 14px;font-size:.92rem}
.lead form{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:600px){.lead form{grid-template-columns:1fr}}
.lead input,.lead select,.lead textarea{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid rgba(148,163,184,.3);background:rgba(2,6,23,.6);color:#e2e8f0;font-size:.95rem;font-family:inherit}
.lead textarea{grid-column:1/-1;min-height:84px;resize:vertical}.lead .full{grid-column:1/-1}
.lead button{grid-column:1/-1;padding:13px;border:none;border-radius:9px;background:linear-gradient(135deg,#e6c76b,#b8901f);color:#0f172a;font-weight:800;font-size:1rem;cursor:pointer}
.lead button[disabled]{opacity:.6;cursor:wait}.lead .ok{display:none;color:#86efac;font-weight:600;margin-top:10px}.lead .err{display:none;color:#fca5a5;margin-top:10px}
.lead input.hp,.sheet input.hp{position:absolute!important;left:-9999px!important;opacity:0!important;height:0!important;width:0!important;padding:0!important;border:0!important;margin:0!important}
.crumbs{font-size:.85rem;color:#94a3b8;margin:6px 0 0}.crumbs a{color:#94a3b8}.crumbs a:hover{color:#e6c76b}
.linkgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px 14px;margin:10px 0}
.linkgrid a{color:#cbd5e1;text-decoration:none;font-size:.93rem;padding:6px 0;border-bottom:1px solid rgba(148,163,184,.1)}
.linkgrid a:hover{color:#e6c76b}.linkgrid a small{color:#64748b;margin-left:6px}
.env{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}@media(max-width:600px){.env{grid-template-columns:1fr}}
.env div{background:rgba(2,6,23,.5);border-radius:10px;padding:12px;text-align:center}.env b{display:block;font-size:1.4rem;color:#e2e8f0}.env span{font-size:.8rem;color:#94a3b8}
body{padding-bottom:84px}
.fab{position:fixed;left:0;right:0;bottom:0;z-index:9000;display:flex;align-items:center;gap:10px;padding:10px 14px calc(10px + env(safe-area-inset-bottom));background:linear-gradient(180deg,rgba(10,17,32,.92),rgba(10,17,32,.98));border-top:1px solid rgba(230,199,107,.45);backdrop-filter:blur(8px);transform:translateY(110%);transition:transform .35s ease}
.fab.on{transform:none}
.fab .txt{flex:1 1 auto;min-width:0;color:#e2e8f0;font-size:.92rem;line-height:1.25}.fab .txt b{color:#f0d894}.fab .txt small{display:block;color:#94a3b8;font-size:.78rem}
.fab .go{flex:0 0 auto;background:linear-gradient(135deg,#f3dd9a,#e6c76b 55%,#caa63e);color:#0f172a;font-weight:800;border:0;padding:12px 18px;border-radius:11px;cursor:pointer;font-size:.95rem;white-space:nowrap;box-shadow:0 8px 26px rgba(230,199,107,.35)}
.fab .x{flex:0 0 auto;background:none;border:0;color:#94a3b8;font-size:1.3rem;cursor:pointer;padding:4px 6px;line-height:1}
@media(max-width:600px){.fab .txt small{display:none}.fab .txt{font-size:.86rem}}
.sheet-bg{position:fixed;inset:0;background:rgba(2,6,23,.6);z-index:9001;display:none}.sheet-bg.on{display:block}
.sheet{position:fixed;left:0;right:0;bottom:0;z-index:9002;max-height:92vh;overflow:auto;background:#0d1526;border-top:1px solid rgba(230,199,107,.5);border-radius:18px 18px 0 0;padding:18px 18px calc(18px + env(safe-area-inset-bottom));transform:translateY(105%);transition:transform .3s ease;box-shadow:0 -20px 60px rgba(0,0,0,.5)}
.sheet.on{transform:none}
@media(min-width:760px){.sheet{left:auto;right:22px;bottom:22px;width:440px;border-radius:18px;border:1px solid rgba(230,199,107,.5)}}
.sheet h3{margin:0 26px 4px 0;color:#fff;font-size:1.15rem}.sheet p.sub{margin:0 0 12px;color:#94a3b8;font-size:.88rem}
.sheet .x{position:absolute;top:10px;right:12px;background:none;border:0;color:#94a3b8;font-size:1.5rem;cursor:pointer;line-height:1}
.sheet form{display:grid;grid-template-columns:1fr 1fr;gap:8px}.sheet form .full,.sheet form textarea,.sheet form button{grid-column:1/-1}
.sheet input,.sheet select,.sheet textarea{width:100%;box-sizing:border-box;padding:10px 11px;border-radius:9px;border:1px solid rgba(148,163,184,.3);background:rgba(2,6,23,.6);color:#e2e8f0;font-size:.93rem;font-family:inherit}
.sheet textarea{min-height:70px;resize:vertical}
.sheet button.send{padding:12px;border:0;border-radius:9px;background:linear-gradient(135deg,#e6c76b,#b8901f);color:#0f172a;font-weight:800;font-size:.98rem;cursor:pointer}
.sheet .alt{margin:10px 0 0;font-size:.82rem;color:#94a3b8;text-align:center}.sheet .alt a{color:#e6c76b}
.sheet .ok,.sheet .err{display:none;margin-top:8px;font-weight:600}.sheet .ok{color:#86efac}.sheet .err{color:#fca5a5}
"""

TRACK_JS = '<script src="https://trilakeshq.com/api/track.js?app=well-tool" defer></script>'


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def clean_frag(s: str) -> str:
    """Model output is constrained, but strip anything outside the allowed tags anyway."""
    s = re.sub(r"<(?!/?(p|strong|em|ul|li|br)\b)[^>]*>", "", s or "")
    return s


def fmt_int(n):
    return f"{n:,}" if isinstance(n, (int, float)) and n is not None else "n/a"


def search_link(r: dict) -> str:
    q = r.get("display", r["name"])
    rad = r.get("radius_miles") or 5
    return f"/?lat={r['lat']}&lon={r['lon']}&r={rad}&q={requests.utils.quote(q)}"


def bands_html(b: dict, keys, labels) -> str:
    if not b:
        return ""
    bar = "".join(f'<i class="k{i+1}" style="width:{max(0.0, b.get(k, 0)) * 100:.1f}%"></i>' for i, k in enumerate(keys))
    key = "".join(f'<span><em>{pct(b.get(k))}</em> {lbl}</span>' for k, lbl in zip(keys, labels))
    return f'<div class="bands">{bar}</div><div class="bands-key">{key}</div>'


def render(r: dict, ai: dict) -> str:
    is_county = r["kind"] == "county"
    disp = r.get("display", r["name"])
    url = f"{BASE_URL}/wells/{r['slug']}/"
    title, desc = ai["title"][:70], ai["meta_description"][:170]
    c = r.get("cost") or {}
    env_ = r["contamination_10mi"]
    scope = ("all permitted wells in the county" if is_county else f"permitted wells within {r['radius_miles']} miles of {r['name']}")

    stats = [
        (fmt_int(r["wells_total"]), "wells in DWR permit records"),
        (f"{fmt_int(r['depth_median'])} ft" if r["depth_median"] else "n/a", "median well depth"),
        (f"{fmt_int(r['depth_p25'])}–{fmt_int(r['depth_p75'])} ft" if r["depth_p25"] else "n/a", "middle half of well depths"),
        (f"{r['yield_median']} gpm" if r["yield_median"] is not None else "n/a", "median pump yield"),
        (f"{fmt_int(r['swl_median'])} ft" if r["swl_median"] else "n/a", "median depth to water"),
        (str(r["newest_permit_year"] or "n/a"), "newest permit on record"),
    ]
    stats_html = "".join(f"<div class='stat'><b>{esc(v)}</b><span>{esc(l)}</span></div>" for v, l in stats)

    depth_bands = bands_html(r["depth_bands"], ["under_100", "100_300", "300_600", "over_600"],
                             ["under 100 ft", "100–300 ft", "300–600 ft", "600+ ft"])
    yield_bands = bands_html(r["yield_bands"], ["under_1gpm", "1_5gpm", "5_15gpm", "over_15gpm"],
                             ["under 1 gpm", "1–5 gpm", "5–15 gpm", "15+ gpm"])

    decades = sorted(r["permits_by_decade"].items())
    dec_rows = "".join(f"<tr><td>{k}s</td><td>{v:,}</td></tr>" for k, v in decades if k >= 1950)
    swl_rows = "".join(f"<tr><td>{k}s</td><td>{v} ft</td></tr>" for k, v in sorted(r["swl_trend_by_decade"].items()))
    aq_rows = "".join(f"<tr><td>{esc(a)}</td><td>{n:,}</td></tr>" for a, n in r["aquifers_top"])
    dr_rows = "".join(f"<tr><td>{esc(d)}</td><td>{n:,}</td></tr>" for d, n in r["top_drillers"])

    cost_html = ""
    if c:
        cost_html = f"""
        <div class="cost-breakdown">
            <div class="cost-breakdown-title">Estimated cost of a new well &middot; {esc(disp)} &middot; median depth {fmt_int(r['depth_median'])} ft</div>
            <div class="cost-row"><div class="cost-row-label">State &amp; county well permit <small>DWR application + county fee</small></div><div class="cost-row-value">$100&ndash;$400</div></div>
            <div class="cost-row"><div class="cost-row-label">Drilling, casing &amp; grouting <small>{fmt_int(r['depth_median'])} ft &times; {money(c['rate_low'])}&ndash;{money(c['rate_high'])}/ft in {esc(c['geology'])}</small></div><div class="cost-row-value">{money(c['hole_low'])}&ndash;{money(c['hole_high'])}</div></div>
            <div class="cost-row"><div class="cost-row-label">Pump, pressure tank &amp; install <small>scales with depth</small></div><div class="cost-row-value">$3,000&ndash;$8,000</div></div>
            <div class="cost-row"><div class="cost-row-label">Trenching, water line, electrical, water test</div><div class="cost-row-value">$2,100&ndash;$8,400</div></div>
            <div class="cost-row total"><div class="cost-row-label">Typical finished total</div><div class="cost-row-value">{money(c['total_low'])}&ndash;{money(c['total_high'])}</div></div>
        </div>
        <p class="note"><strong>How this is built:</strong> the median permitted depth here times the per-foot range for this ground, plus the normal system costs. A lot at the 75th-percentile depth ({fmt_int(r['depth_p75'])} ft) would run roughly {money(r['depth_p75'] * c['rate_low'] + 6000)}&ndash;{money(r['depth_p75'] * c['rate_high'] + 12000)}. See the <a href="/well-drilling-cost-colorado/">full Colorado well cost guide</a>.</p>"""

    sections_html = ""
    for s in ai["sections"]:
        sections_html += f"<section class='block'><h2>{esc(s['heading'])}</h2>{clean_frag(s['html'])}</section>\n"

    faq_html = "".join(f"<div class='faq-item'><h3>{esc(f['q'])}</h3><p>{clean_frag(f['a'])}</p></div>" for f in ai["faq"])
    faq_plain = [(re.sub(r"<[^>]+>", "", f["q"]), re.sub(r"<[^>]+>", "", f["a"])) for f in ai["faq"]]

    # internal links
    if is_county:
        places = r.get("places", [])
        link_block = ""
        if places:
            link_block += f"<h3>Towns and areas in {esc(r['name'])}</h3><div class='linkgrid'>" + "".join(
                f"<a href='/wells/{p['slug']}/'>{esc(p['name'])}<small>{p['wells']:,} wells</small></a>" for p in places) + "</div>"
        link_block += "<h3>Neighboring counties</h3><div class='linkgrid'>" + "".join(
            f"<a href='/wells/{n['slug']}/'>{esc(n['name'])}</a>" for n in r.get("neighbors", [])) + "</div>"
        crumbs = f"<a href='/'>Colorado Well Finder</a> &rsaquo; <a href='/wells/'>Wells by county</a> &rsaquo; {esc(r['name'])}"
        parent_ld = {"@type": "ListItem", "position": 3, "name": r["name"], "item": url}
    else:
        cslug = re.sub(r"[^a-z0-9]+", "-", r["county_name"].lower()).strip("-")
        link_block = f"<h3>More in {esc(r['county_name'])}</h3><p><a href='/wells/{cslug}/'>All wells in {esc(r['county_name'])}</a> &mdash; county-wide depth, yield and cost figures.</p>"
        link_block += "<h3>Nearby areas</h3><div class='linkgrid'>" + "".join(
            f"<a href='/wells/{n['slug']}/'>{esc(n['name'])}<small>{n['miles']} mi</small></a>" for n in r.get("neighbors", [])) + "</div>"
        crumbs = f"<a href='/'>Colorado Well Finder</a> &rsaquo; <a href='/wells/'>Wells by county</a> &rsaquo; <a href='/wells/{cslug}/'>{esc(r['county_name'])}</a> &rsaquo; {esc(r['name'])}"
        parent_ld = {"@type": "ListItem", "position": 3, "name": r["county_name"], "item": f"{BASE_URL}/wells/{cslug}/"}

    ld_faq = {"@context": "https://schema.org", "@type": "FAQPage",
              "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq_plain]}
    crumb_items = [{"@type": "ListItem", "position": 1, "name": "Colorado Well Finder", "item": BASE_URL + "/"},
                   {"@type": "ListItem", "position": 2, "name": "Wells by county", "item": BASE_URL + "/wells/"}, parent_ld]
    if not is_county:
        crumb_items.append({"@type": "ListItem", "position": 4, "name": disp, "item": url})
    ld_crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": crumb_items}
    ld_page = {"@context": "https://schema.org", "@type": "Article", "headline": ai["h1"], "description": desc,
               "url": url, "datePublished": TODAY, "dateModified": TODAY,
               "author": {"@type": "Organization", "name": "Colorado Well Finder", "url": BASE_URL},
               "publisher": {"@type": "Organization", "name": "Colorado Well Finder", "url": BASE_URL},
               "about": {"@type": "Place", "name": disp, "geo": {"@type": "GeoCoordinates", "latitude": r["lat"], "longitude": r["lon"]}},
               "isBasedOn": "https://dwr.state.co.us/Tools/WellPermits"}

    tri_blurb = esc(ai.get("tri_lakes_blurb", ""))
    lead_default = "Access driveway" if r["region"] == "mountain" else "Site development"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="theme-color" content="#0f172a">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <link rel="apple-touch-icon" href="/favicon.svg">
    <title>{esc(title)}</title>
    <meta name="description" content="{esc(desc)}">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{url}">
    <meta property="og:type" content="article">
    <meta property="og:url" content="{url}">
    <meta property="og:title" content="{esc(title)}">
    <meta property="og:description" content="{esc(desc)}">
    <meta property="og:image" content="{BASE_URL}/og-image.png">
    <meta property="og:site_name" content="Colorado Well Finder">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{esc(title)}">
    <meta name="twitter:description" content="{esc(desc)}">
    <meta name="geo.region" content="US-CO">
    <meta name="geo.placename" content="{esc(disp)}">
    <meta name="geo.position" content="{r['lat']};{r['lon']}">
    <script type="application/ld+json">{json.dumps(ld_page, ensure_ascii=False)}</script>
    <script type="application/ld+json">{json.dumps(ld_faq, ensure_ascii=False)}</script>
    <script type="application/ld+json">{json.dumps(ld_crumbs, ensure_ascii=False)}</script>
    <style>{BASE_CSS}{EXTRA_CSS}</style>
    {TRACK_JS}
</head>
<body>
    <div class="container">
        <nav class="topnav">
            <a class="brand" href="/">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C12 2 6 8.5 6 13C6 16.5 8.5 20 12 20C15.5 20 18 16.5 18 13C18 8.5 12 2 12 2Z"/><path d="M12 16C13.5 16 15 14.5 15 13C15 11 12 8 12 8"/></svg>
                Colorado Well Finder
            </a>
            <a class="back-link" href="{search_link(r)}">&larr; Map the wells near {esc(r['name'])}</a>
        </nav>

        <header class="hero">
            <span class="hero-eyebrow">{esc(ai['eyebrow'])}</span>
            <h1>{esc(ai['h1'])}</h1>
            <p class="crumbs">{crumbs}</p>
            {clean_frag(ai['intro_html'])}
        </header>

        <div class="stats">{stats_html}</div>
        <p class="note">Figures are computed from Colorado Division of Water Resources permit records for {esc(scope)}. Not every permit reports every field; each statistic uses the wells that report it.</p>

        <section class="block">
            <h2>How deep are wells around {esc(r['name'])}?</h2>
            {depth_bands}
            <div class="two">
                <div><table class="mini"><thead><tr><th>Depth statistic</th><th>Feet</th></tr></thead><tbody>
                    <tr><td>25th percentile</td><td>{fmt_int(r['depth_p25'])}</td></tr>
                    <tr><td>Median</td><td>{fmt_int(r['depth_median'])}</td></tr>
                    <tr><td>75th percentile</td><td>{fmt_int(r['depth_p75'])}</td></tr>
                    <tr><td>90th percentile</td><td>{fmt_int(r['depth_p90'])}</td></tr>
                    <tr><td>Deepest on record</td><td>{fmt_int(r['depth_max'])}</td></tr>
                </tbody></table></div>
                <div><table class="mini"><thead><tr><th>Aquifer named on permits</th><th>Wells</th></tr></thead><tbody>{aq_rows or '<tr><td>No aquifer recorded</td><td>&mdash;</td></tr>'}</tbody></table></div>
            </div>
        </section>

        <section class="block">
            <h2>How much water do wells make near {esc(r['name'])}?</h2>
            {yield_bands or '<p>Too few permits here report a pump yield to chart.</p>'}
            <div class="two">
                <div><table class="mini"><thead><tr><th>Yield statistic</th><th>gpm</th></tr></thead><tbody>
                    <tr><td>25th percentile</td><td>{r['yield_p25'] if r['yield_p25'] is not None else 'n/a'}</td></tr>
                    <tr><td>Median</td><td>{r['yield_median'] if r['yield_median'] is not None else 'n/a'}</td></tr>
                    <tr><td>75th percentile</td><td>{r['yield_p75'] if r['yield_p75'] is not None else 'n/a'}</td></tr>
                    <tr><td>Median depth to water</td><td>{fmt_int(r['swl_median'])} ft</td></tr>
                </tbody></table></div>
                <div><table class="mini"><thead><tr><th>Median water level by decade</th><th>Depth to water</th></tr></thead><tbody>{swl_rows or '<tr><td>Not enough dated measurements</td><td>&mdash;</td></tr>'}</tbody></table></div>
            </div>
        </section>

        {sections_html}

        <section class="block">
            <h2>Well permits by decade near {esc(r['name'])}</h2>
            <div class="two">
                <div><table class="mini"><thead><tr><th>Decade permitted</th><th>Wells</th></tr></thead><tbody>{dec_rows}</tbody></table></div>
                <div><table class="mini"><thead><tr><th>Most active drillers on record</th><th>Wells</th></tr></thead><tbody>{dr_rows or '<tr><td>Driller not recorded</td><td>&mdash;</td></tr>'}</tbody></table></div>
            </div>
        </section>

        <section class="block">
            <h2>What a new well costs in {esc(disp)}</h2>
            {cost_html or '<p>Too few permits here report a depth to build a cost estimate.</p>'}
        </section>

        <section class="block">
            <h2>Environmental sites mapped within about 10 miles</h2>
            <div class="env">
                <div><b>{env_.get('pfas', 0)}</b><span>PFAS sampling / contamination sites</span></div>
                <div><b>{env_.get('mines', 0)}</b><span>historic mine features</span></div>
                <div><b>{env_.get('superfund', 0)}</b><span>EPA Superfund &amp; cleanup sites</span></div>
            </div>
            <p>Counts come from EPA, USGS and state datasets layered on the <a href="{search_link(r)}">Colorado Well Finder map</a>. A count is not a verdict on any one well; it tells you what to test for.</p>
        </section>

        <div class="lookup-cta">
            <h2>See every well near a specific address in {esc(r['name'])}</h2>
            <p>The numbers above are the area average. Your lot is one point on the map. Search the address to see the real depth, yield and aquifer of each permitted well around it.</p>
            <a class="lookup-btn" href="{search_link(r)}">
                <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
                Map wells near {esc(r['name'])}
            </a>
        </div>

        <div class="tl" id="tri-lakes">
            <div class="who">Building or improving land near {esc(r['name'])}?</div>
            <h2>Tri-Lakes Contracting</h2>
            <p>{tri_blurb}</p>
            <ul>
                <li>Access driveways &amp; road cuts</li>
                <li>Site development, grading &amp; drainage</li>
                <li>New-build septic systems (OWTS)</li>
                <li>Custom home construction</li>
            </ul>
            <p class="lic">Licensed Colorado custom home builder &middot; licensed driveway &amp; excavation contractor &middot; licensed septic installer</p>
            <div class="btns">
                <a class="b1" data-sheet="1" href="https://trilakes.co/get-a-quote/?utm_source=coloradowell&amp;utm_medium=well_page&amp;utm_campaign={esc(r['slug'])}">Get a quote from Tri-Lakes</a>
                <a class="b2" href="https://trilakes.co/?utm_source=coloradowell&amp;utm_medium=well_page&amp;utm_campaign={esc(r['slug'])}" target="_blank" rel="noopener">trilakes.co</a>
            </div>
        </div>

        <section class="block" id="feasibility">
            <h2>Buying land near {esc(r['name'])}? Know what it costs to build before you buy the dirt.</h2>
            <p>A listing photo can't tell you whether a lot is a dream or a money pit. Two parcels near {esc(r['name'])} can carry the same asking price while one needs an <strong>$80,000 driveway and a $45,000 engineered septic</strong> the other doesn't. The well numbers on this page are the start of that answer. A <strong>Tri-Lakes Site Feasibility Report</strong> is the rest of it: a licensed general contractor, excavator and septic installer reads the parcel the way we would if we were about to build on it, and puts the real cost in writing before your money is committed.</p>
            <div class="cost-breakdown">
                <div class="cost-breakdown-title">What the wrong lot can cost you &middot; the things a listing never mentions</div>
                <div class="cost-row"><div class="cost-row-label">Long or steep driveway &amp; access</div><div class="cost-row-value">$25k&ndash;$80k+</div></div>
                <div class="cost-row"><div class="cost-row-label">Rock excavation &amp; extra earthwork</div><div class="cost-row-value">$15k&ndash;$60k+</div></div>
                <div class="cost-row"><div class="cost-row-label">Engineered / advanced septic instead of conventional</div><div class="cost-row-value">+$20k&ndash;$40k</div></div>
                <div class="cost-row"><div class="cost-row-label">Water &amp; long utility runs</div><div class="cost-row-value">$10k&ndash;$50k+</div></div>
                <div class="cost-row total"><div class="cost-row-label">A feasibility report</div><div class="cost-row-value">from $99</div></div>
            </div>
            <p><strong>Spend a little to know. Or gamble a lot to guess.</strong> Realtors sell the view and inspectors look at houses that already exist. We're the people who'd build it, so we catch what quietly turns a "great deal" into a budget disaster: bad access, buried rock, steep grade, soil that won't pass for a normal septic, water that's a fortune to reach. And if the lot is good, you've already got a builder ready to do the work, driveway to drywall.</p>
            <ul>
                <li><strong>Instant Online Report, $99</strong> &mdash; type the address and get a buildability read on the spot: parcel, terrain and slope, soil and septic outlook. Cheap enough to run on every lot you're shopping.</li>
                <li><strong>Desktop Report, $500</strong> &mdash; a contractor's from-the-desk feasibility read: Regulation 43 and county code, GIS parcel data, terrain, flood and drainage, soil and septic viability, setbacks and buildable envelope, nearby well depths from our 591,000-well database, the likely driveway route, and a preliminary site-development cost breakdown.</li>
                <li><strong>On-Site Report, $2,000</strong> &mdash; everything above, then boots and a drone on the actual ground: house pad and septic zone located, access and winter conditions checked, rock and soil observed in person, a 15 to 20 page written opinion you can hand a lender or seller. <strong>Credited toward your project if you build with us.</strong></li>
            </ul>
            <p>Under contract, comparing parcels, or a realtor with a rural listing whose buyers keep asking "what will it cost to build here?" This is the due-diligence step almost everyone skips, and the one that saves the most.</p>
            <div class="tl-btns" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">
                <a class="lookup-btn" href="https://trilakes.co/build-feasibility?utm_source=coloradowell&amp;utm_medium=well_page&amp;utm_campaign={esc(r['slug'])}#instant" target="_blank" rel="noopener">
                    <svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                    Check my lot instantly &rarr;
                </a>
                <a class="lookup-btn" style="background:transparent;border:1px solid rgba(230,199,107,.5);color:#e6c76b;box-shadow:none" href="https://trilakes.co/build-feasibility?utm_source=coloradowell&amp;utm_medium=well_page&amp;utm_campaign={esc(r['slug'])}#request" target="_blank" rel="noopener">
                    <svg viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
                    Request a full report
                </a>
            </div>
        </section>

        <div class="lead" id="lead">
            <h2>Tell us about your project</h2>
            <p class="sub">Driveway, site work, septic or a build near {esc(r['name'])}. Licensed GC, excavator and septic installer.</p>
            <form id="tl-lead" novalidate>
                <input type="text" name="name" placeholder="Your name" required>
                <input type="tel" name="phone" placeholder="Phone" required>
                <input type="email" name="email" placeholder="Email" required>
                <input type="text" name="address" placeholder="Property address or area (optional)">
                <select name="project_type" class="full">
                    <option{' selected' if lead_default == 'Access driveway' else ''}>Access driveway</option>
                    <option{' selected' if lead_default == 'Site development' else ''}>Site development / grading</option>
                    <option>New-build septic system</option>
                    <option>Custom home build</option>
                    <option>Well + septic + driveway package</option>
                    <option>Something else</option>
                </select>
                <textarea name="message" placeholder="Short description of the work you're looking to get done, and the property location if you have it" required></textarea>
                <input type="text" name="company_website" class="hp" tabindex="-1" autocomplete="off">
                <button type="submit">Send to Tri-Lakes Contracting</button>
            </form>
            <p class="ok" id="tl-ok">Got it. Tri-Lakes will get back to you shortly.</p>
            <p class="err" id="tl-err">Something went wrong sending that. Email <a href="mailto:kyle@trilakes.co">kyle@trilakes.co</a> instead.</p>
        </div>

        <section class="block">
            <h2>Common questions about wells in {esc(r['name'])}</h2>
            {faq_html}
        </section>

        <section class="block">
            <h2>Keep researching</h2>
            {link_block}
            <div class="related" style="margin-top:14px">
                <a href="/wells/">Wells by county &amp; town<small>Every Colorado county and town with permit data</small></a>
                <a href="/well-drilling-cost-colorado/">Cost to drill a well in Colorado<small>Per-foot rates, line items, regional ranges</small></a>
                <a href="{search_link(r)}">Map wells near {esc(r['name'])}<small>Real permitted depths &amp; yields at any address</small></a>
                <a href="https://trilakes.co/build-feasibility?utm_source=coloradowell" target="_blank" rel="noopener">Site Feasibility Report<small>Desktop $500 &middot; on-site $2,000, credited if you build with Tri-Lakes</small></a>
            </div>
        </section>

        <footer class="pagefoot">
            <p>Colorado Well Finder &copy; 2025&ndash;2026 &nbsp;&#124;&nbsp; Colorado DWR well permit records, updated {TODAY}</p>
            <p class="disclaimer">Statistics are computed from state permit records for {esc(scope)} and describe the area, not any individual parcel. Depths, yields and water levels vary lot to lot, especially in fractured rock. Cost ranges are planning estimates, not bids. Verify permit requirements with the Colorado Division of Water Resources and get written quotes from licensed drillers.</p>
        </footer>
    </div>

    <!-- Floating lead bar: one tap opens the form from anywhere on the page -->
    <div class="fab" id="fab">
        <div class="txt"><b>Driveway, septic, site work or a build near {esc(r['name'])}?</b><small>Licensed GC, excavator &amp; septic installer. Tell us what you need &mdash; 30 seconds.</small></div>
        <button class="go" id="fab-go" type="button">Get a quote</button>
        <button class="x" id="fab-x" type="button" aria-label="Dismiss">&times;</button>
    </div>
    <div class="sheet-bg" id="sheet-bg"></div>
    <div class="sheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
        <button class="x" id="sheet-x" type="button" aria-label="Close">&times;</button>
        <h3 id="sheet-title">Tell Tri-Lakes what you need</h3>
        <p class="sub">Driveway, septic, site development, feasibility or a custom build near {esc(r['name'])}.</p>
        <form class="tl-form" id="tl-lead-2" novalidate>
            <input type="text" name="name" placeholder="Your name" required>
            <input type="tel" name="phone" placeholder="Phone" required>
            <input type="email" name="email" placeholder="Email" required class="full">
            <input type="text" name="address" placeholder="Property address or area (optional)" class="full">
            <select name="project_type" class="full">
                <option>Access driveway</option>
                <option>Site development / grading</option>
                <option>New-build septic system</option>
                <option>Site Feasibility Report (before I buy)</option>
                <option>Custom home build</option>
                <option>Well + septic + driveway package</option>
                <option>Something else</option>
            </select>
            <textarea name="message" placeholder="What are you looking to get done?" required></textarea>
            <input type="text" name="company_website" class="hp" tabindex="-1" autocomplete="off">
            <button type="submit" class="send">Send to Tri-Lakes Contracting</button>
            <p class="ok full">Got it. Tri-Lakes will get back to you shortly.</p>
            <p class="err full">Something went wrong. Email <a href="mailto:kyle@trilakes.co" style="color:#e6c76b">kyle@trilakes.co</a> or call <a href="tel:+17198883255" style="color:#e6c76b">(719) 888-3255</a>.</p>
        </form>
        <p class="alt">Shopping a lot? <a href="https://trilakes.co/build-feasibility?utm_source=coloradowell&amp;utm_medium=well_page_sheet&amp;utm_campaign={esc(r['slug'])}#instant" target="_blank" rel="noopener">Run the $99 instant buildability check &rarr;</a></p>
    </div>

    <script>
    (function(){{
      var PLACE='{esc(disp)}';
      function wire(f){{
        if(!f)return;
        var ok=f.querySelector('.ok')||document.getElementById('tl-ok'),er=f.querySelector('.err')||document.getElementById('tl-err');
        f.addEventListener('submit',function(ev){{
          ev.preventDefault();
          var d=f.elements,name=d.name.value.trim(),email=d.email.value.trim(),phone=d.phone.value.trim(),msg=d.message.value.trim();
          if(!name||!email||!phone||!msg){{er.textContent='Please fill in your name, phone, email and a short description.';er.style.display='block';return;}}
          var btn=f.querySelector('button[type=submit]');btn.disabled=true;btn.textContent='Sending…';er.style.display='none';
          if(d.company_website.value){{ok.style.display='block';return;}}
          var addr=(d.address.value||'').trim()||PLACE+' area';
          var payload={{name:name,email:email,phone:phone,address:addr,owns:'',services:d.project_type.value,
            notes:msg+' | from coloradowell.com well page: '+PLACE+' ('+location.pathname+')',source:'coloradowell-well-page'}};
          fetch('{LEAD_ENDPOINT}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}})
            .then(function(r){{if(!r.ok)throw new Error('http '+r.status);return r.json();}})
            .then(function(){{
              ok.style.display='block';
              Array.prototype.forEach.call(f.querySelectorAll('input,select,textarea,button'),function(el){{el.style.display='none';}});
              try{{navigator.sendBeacon('{HQ_LEAD_ENDPOINT}',new Blob([JSON.stringify({{email:email,address:PLACE+' (well page lead: '+d.project_type.value+')',source:'well_page_lead',app:'well-tool',page:location.pathname}})],{{type:'text/plain'}}));}}catch(e){{}}
              try{{if(window.HQ){{HQ.conversion('well_page_lead',email,{{}});}}}}catch(e){{}}
              try{{sessionStorage.setItem('tlLeadSent','1');}}catch(e){{}}
              var fab=document.getElementById('fab');if(fab)fab.classList.remove('on');
            }})
            .catch(function(){{er.style.display='block';btn.disabled=false;btn.textContent='Send to Tri-Lakes Contracting';}});
        }});
      }}
      wire(document.getElementById('tl-lead'));
      wire(document.getElementById('tl-lead-2'));

      var fab=document.getElementById('fab'),sheet=document.getElementById('sheet'),bg=document.getElementById('sheet-bg');
      function openSheet(){{sheet.classList.add('on');bg.classList.add('on');fab.classList.remove('on');
        try{{if(window.HQ){{HQ.event('lead_sheet_open',PLACE);}}}}catch(e){{}}
        setTimeout(function(){{var i=sheet.querySelector('input[name=name]');if(i)i.focus();}},250);}}
      function closeSheet(){{sheet.classList.remove('on');bg.classList.remove('on');showFab();}}
      function dismissed(){{try{{return sessionStorage.getItem('tlFabOff')==='1'||sessionStorage.getItem('tlLeadSent')==='1';}}catch(e){{return false;}}}}
      function showFab(){{if(!dismissed())fab.classList.add('on');}}
      document.getElementById('fab-go').addEventListener('click',openSheet);
      document.getElementById('fab-x').addEventListener('click',function(){{fab.classList.remove('on');try{{sessionStorage.setItem('tlFabOff','1');}}catch(e){{}}}});
      document.getElementById('sheet-x').addEventListener('click',closeSheet);
      bg.addEventListener('click',closeSheet);
      document.addEventListener('keydown',function(ev){{if(ev.key==='Escape'&&sheet.classList.contains('on'))closeSheet();}});
      // Bar rises after the reader has engaged: 350px of scroll or 8 seconds, whichever first.
      var shown=false;function maybe(){{if(shown)return;shown=true;showFab();}}
      window.addEventListener('scroll',function(){{if(window.scrollY>350)maybe();}},{{passive:true}});
      setTimeout(maybe,8000);
      // Any "Get a quote" style link on the page can open the sheet instead of leaving the site.
      Array.prototype.forEach.call(document.querySelectorAll('a[data-sheet]'),function(a){{a.addEventListener('click',function(ev){{ev.preventDefault();openSheet();}});}});
    }})();
    </script>
</body>
</html>
"""


def render_hub(pages: list[dict]) -> str:
    counties = sorted([p for p in pages if p["kind"] == "county"], key=lambda p: p["name"])
    by_county = {}
    for p in pages:
        if p["kind"] == "place":
            by_county.setdefault(p["county"], []).append(p)
    total = sum(p["wells_total"] for p in counties)
    body = ""
    for c in counties:
        kids = sorted(by_county.get(c["county"], []), key=lambda k: -k["wells_total"])
        kid_links = "".join(f"<a href='/wells/{k['slug']}/'>{esc(k['name'])}<small>{k['wells_total']:,}</small></a>" for k in kids)
        body += f"""<section class="block"><h2><a href="/wells/{c['slug']}/">{esc(c['name'])}</a></h2>
        <p>{c['wells_total']:,} permitted wells &middot; median depth {fmt_int(c['depth_median'])} ft &middot; median yield {c['yield_median'] if c['yield_median'] is not None else 'n/a'} gpm &middot; typical new well {money(c['cost']['total_low']) + '–' + money(c['cost']['total_high']) if c.get('cost') else 'n/a'}</p>
        {('<div class="linkgrid">' + kid_links + '</div>') if kid_links else ''}</section>\n"""
    ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "Colorado well depth, yield and drilling cost by county and town",
          "url": f"{BASE_URL}/wells/", "hasPart": [{"@type": "WebPage", "name": c["name"], "url": f"{BASE_URL}/wells/{c['slug']}/"} for c in counties]}
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="theme-color" content="#0f172a">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>Colorado Well Depth &amp; Drilling Cost by County and Town</title>
<meta name="description" content="How deep are wells in every Colorado county and town? Median depth, yield, water level and new-well cost for {len(counties)} counties and {len(pages) - len(counties)} towns, from {total:,} DWR permit records.">
<link rel="canonical" href="{BASE_URL}/wells/">
<meta property="og:title" content="Colorado Well Depth &amp; Drilling Cost by County and Town"><meta property="og:url" content="{BASE_URL}/wells/"><meta property="og:image" content="{BASE_URL}/og-image.png">
<script type="application/ld+json">{json.dumps(ld)}</script>
<style>{BASE_CSS}{EXTRA_CSS}</style>{TRACK_JS}
</head><body><div class="container">
<nav class="topnav"><a class="brand" href="/"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C12 2 6 8.5 6 13C6 16.5 8.5 20 12 20C15.5 20 18 16.5 18 13C18 8.5 12 2 12 2Z"/><path d="M12 16C13.5 16 15 14.5 15 13C15 11 12 8 12 8"/></svg>Colorado Well Finder</a><a class="back-link" href="/">&larr; Look up wells near you</a></nav>
<header class="hero"><span class="hero-eyebrow">Colorado well data by place</span><h1>Well Depth, Yield &amp; Drilling Cost <span>by County and Town</span></h1>
<p class="lede">Every page below is built from Colorado Division of Water Resources permit records: {total:,} wells across {len(counties)} counties. Pick a county for the county-wide picture, or a town for the wells within a few miles of it. Each page shows median depth, the depth spread, pump yields, depth to water, permit history, nearby environmental sites and what a new well should cost there.</p></header>
{body}
<div class="tl"><div class="who">Building on Colorado land?</div><h2>Tri-Lakes Contracting</h2><p>Licensed Colorado custom home builder, licensed driveway and excavation contractor, and licensed septic installer. Access driveways, site development, new-build septic systems and custom homes.</p><div class="btns"><a class="b1" href="https://trilakes.co/get-a-quote/?utm_source=coloradowell&amp;utm_medium=wells_hub" target="_blank" rel="noopener">Get a quote</a><a class="b2" href="https://trilakes.co/?utm_source=coloradowell&amp;utm_medium=wells_hub" target="_blank" rel="noopener">trilakes.co</a></div></div>
<footer class="pagefoot"><p>Colorado Well Finder &copy; 2025&ndash;2026 &nbsp;&#124;&nbsp; Colorado DWR well permit records, updated {TODAY}</p></footer>
</div></body></html>"""


def write_sitemap(pages: list[dict]) -> None:
    urls = [(f"{BASE_URL}/", "weekly", "1.0"), (f"{BASE_URL}/well-drilling-cost-colorado/", "monthly", "0.8"),
            (f"{BASE_URL}/wells/", "weekly", "0.9")]
    urls += [(f"{BASE_URL}/wells/{p['slug']}/", "monthly", "0.8" if p["kind"] == "county" else "0.7") for p in pages]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, cf, pr in urls:
        xml.append(f"  <url><loc>{u}</loc><lastmod>{TODAY}</lastmod><changefreq>{cf}</changefreq><priority>{pr}</priority></url>")
    xml.append("</urlset>")
    (SITE / "sitemap.xml").write_text("\n".join(xml) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="slugs to build")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-sitemap", action="store_true")
    a = ap.parse_args()

    allp = json.loads((DATA / "stats.json").read_text(encoding="utf-8"))
    for p in allp:  # JSON turns the decade keys into strings
        p["permits_by_decade"] = {int(k): v for k, v in p["permits_by_decade"].items()}
        p["swl_trend_by_decade"] = {int(k): v for k, v in p["swl_trend_by_decade"].items()}
    pages = [p for p in allp if p["kind"] == "county" or p["wells_total"] >= MIN_PLACE_WELLS]
    # drop county->place links that point at filtered-out places
    keep = {p["slug"] for p in pages}
    for p in pages:
        if p["kind"] == "county":
            p["places"] = [x for x in p.get("places", []) if x["slug"] in keep]
        p["neighbors"] = [n for n in p.get("neighbors", []) if n["slug"] in keep]
    todo = pages
    if a.only:
        todo = [p for p in pages if p["slug"] in set(a.only)]
    elif a.limit:
        todo = sorted(pages, key=lambda p: -p["wells_total"])[: a.limit]
    print(f"pages eligible: {len(pages)} (counties {sum(p['kind']=='county' for p in pages)}, places {sum(p['kind']=='place' for p in pages)}); building {len(todo)}")

    done = fail = 0
    tokens = 0
    t0 = time.time()

    def build(p):
        ai = write_copy(p)
        out = OUT_ROOT / p["slug"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text(render(p, ai), encoding="utf-8")
        return p["slug"], ai.get("_usage", {}).get("total_tokens", 0), ai.get("_model", "cache")

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(build, p): p for p in todo}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                slug, tk, model = fut.result()
                done += 1
                tokens += tk or 0
                with _print_lock:
                    print(f"  [{done + fail}/{len(todo)}] ok  {slug:<34} {model:<10} tokens={tk}  {time.time() - t0:.0f}s", flush=True)
            except Exception as e:  # noqa: BLE001
                fail += 1
                with _print_lock:
                    print(f"  [{done + fail}/{len(todo)}] FAIL {p['slug']}: {str(e)[:160]}", flush=True)

    built = [p for p in pages if (OUT_ROOT / p["slug"] / "index.html").exists()]
    if not a.no_sitemap:
        (OUT_ROOT / "index.html").write_text(render_hub(built), encoding="utf-8")
        write_sitemap(built)
    print(f"\ndone={done} fail={fail} tokens={tokens:,} pages_on_disk={len(built)} hub+sitemap={'skipped' if a.no_sitemap else 'written'}  {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
