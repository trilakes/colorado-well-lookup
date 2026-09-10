"""Step 1 of the programmatic SEO build: compute real well statistics for every
Colorado county and every town/CDP with enough wells nearby.

Reads  seo_pages/data/co_wells.parquet  (cached from the wells DB)
       seo_pages/data/co_places.csv      (Census gazetteer, CO places)
Writes seo_pages/data/stats.json         (one record per page)

Nothing here touches the live site.
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

CO_COUNTIES = [
    "ADAMS", "ALAMOSA", "ARAPAHOE", "ARCHULETA", "BACA", "BENT", "BOULDER", "BROOMFIELD", "CHAFFEE", "CHEYENNE",
    "CLEAR CREEK", "CONEJOS", "COSTILLA", "CROWLEY", "CUSTER", "DELTA", "DENVER", "DOLORES", "DOUGLAS", "EAGLE",
    "EL PASO", "ELBERT", "FREMONT", "GARFIELD", "GILPIN", "GRAND", "GUNNISON", "HINSDALE", "HUERFANO", "JACKSON",
    "JEFFERSON", "KIOWA", "KIT CARSON", "LA PLATA", "LAKE", "LARIMER", "LAS ANIMAS", "LINCOLN", "LOGAN", "MESA",
    "MINERAL", "MOFFAT", "MONTEZUMA", "MONTROSE", "MORGAN", "OTERO", "OURAY", "PARK", "PHILLIPS", "PITKIN",
    "PROWERS", "PUEBLO", "RIO BLANCO", "RIO GRANDE", "ROUTT", "SAGUACHE", "SAN JUAN", "SAN MIGUEL", "SEDGWICK",
    "SUMMIT", "TELLER", "WASHINGTON", "WELD", "YUMA",
]
PLAINS = {"BACA", "BENT", "CHEYENNE", "CROWLEY", "KIOWA", "KIT CARSON", "LINCOLN", "LOGAN", "MORGAN", "OTERO",
          "PHILLIPS", "PROWERS", "SEDGWICK", "WASHINGTON", "YUMA", "WELD", "ADAMS", "ARAPAHOE", "PUEBLO", "LAS ANIMAS"}
MOUNTAIN = {"PARK", "TELLER", "CHAFFEE", "CLEAR CREEK", "GILPIN", "SUMMIT", "LAKE", "GRAND", "JACKSON", "EAGLE",
            "PITKIN", "GUNNISON", "HINSDALE", "MINERAL", "SAN JUAN", "OURAY", "SAN MIGUEL", "DOLORES", "CUSTER",
            "FREMONT", "HUERFANO", "ARCHULETA", "ROUTT", "LARIMER", "BOULDER", "JEFFERSON", "DOUGLAS"}


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s


def titlecase_county(c: str) -> str:
    return " ".join(w.capitalize() if w not in ("LA", "DE") else w.capitalize() for w in c.split())


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def q(series, p):
    s = series.dropna()
    return None if len(s) == 0 else float(np.percentile(s, p))


def r0(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else int(round(x))


def decade_of(dates: pd.Series) -> pd.Series:
    yrs = pd.to_datetime(dates, errors="coerce").dt.year
    return (yrs // 10 * 10).where(yrs.between(1900, 2026))


def aquifer_label(a: str) -> str:
    a = (a or "").strip()
    if not a:
        return ""
    if a.upper() == "ALL UNNAMED AQUIFERS":
        return "unnamed / fractured-bedrock aquifer"
    return a.title().replace("Fox Hills", "Fox Hills")


def load_contamination() -> dict[str, np.ndarray]:
    dsn = re.search(r"postgresql://[^'\"]+", (HERE.parent / "_inventory_pg.py").read_text()).group(0)
    c = psycopg2.connect(dsn)
    out = {}
    for name, sql in {
        "pfas": "SELECT latitude, longitude FROM pfas_sites WHERE state='CO' AND latitude IS NOT NULL",
        "mines": "SELECT latitude, longitude FROM mine_sites WHERE state='CO' AND latitude IS NOT NULL",
        "superfund": "SELECT latitude, longitude FROM epa_sites WHERE state='CO' AND latitude IS NOT NULL",
    }.items():
        try:
            out[name] = pd.read_sql(sql, c).to_numpy(dtype=float)
        except Exception as e:  # noqa: BLE001
            c.rollback()
            print(f"  contamination table {name} skipped: {str(e)[:80]}")
            out[name] = np.zeros((0, 2))
    c.close()
    return out


def stats_for(df: pd.DataFrame, county_hint: str | None) -> dict:
    """df = wells for one page. Returns the numbers the page is built from."""
    constructed = df[df.status.fillna("").str.startswith(("Well Constructed", "Well Replaced"))]
    dep = df.depth_total.where(df.depth_total.between(10, 5000))
    yld = df.pump_yield_gpm.where(df.pump_yield_gpm.between(0, 3000))
    swl = df.static_water_level.where(df.static_water_level.between(0, 3000))
    uses = df.uses.fillna("").str.lower()
    dom = uses.str.contains("domestic|household").sum()
    stock = uses.str.contains("stock").sum()
    irr = uses.str.contains("irrigation").sum()
    mon = uses.str.contains("monitoring").sum()
    n = len(df)

    # aquifers
    aq = df.aquifers.fillna("").str.split(",").explode().str.strip()
    aq = aq[aq != ""]
    aq_top = [(aquifer_label(k), int(v)) for k, v in aq.value_counts().head(3).items()]
    denver_share = float((df.denver_basin_aquifer.fillna("").str.upper() == "YES").mean()) if n else 0.0

    # drilling by decade (permit issued)
    dec = decade_of(df.date_permit_issued)
    by_decade = {int(k): int(v) for k, v in dec.value_counts().sort_index().items() if k >= 1950}
    newest_year = pd.to_datetime(df.date_permit_issued, errors="coerce").dt.year.max()

    # water level by decade (measurement date)
    swl_dec = decade_of(df.static_water_level_date)
    trend = {}
    for d, grp in swl.groupby(swl_dec):
        g = grp.dropna()
        if len(g) >= 8 and d >= 1950:
            trend[int(d)] = int(round(float(np.median(g))))

    # depth distribution
    d_valid = dep.dropna()
    depth_bands = {}
    if len(d_valid):
        depth_bands = {
            "under_100": float((d_valid < 100).mean()),
            "100_300": float(d_valid.between(100, 300, inclusive="left").mean()),
            "300_600": float(d_valid.between(300, 600, inclusive="left").mean()),
            "over_600": float((d_valid >= 600).mean()),
        }
    y_valid = yld.dropna()
    yield_bands = {}
    if len(y_valid):
        yield_bands = {
            "under_1gpm": float((y_valid < 1).mean()),
            "1_5gpm": float(y_valid.between(1, 5, inclusive="left").mean()),
            "5_15gpm": float(y_valid.between(5, 15, inclusive="left").mean()),
            "over_15gpm": float((y_valid >= 15).mean()),
        }

    drillers = [(k.title(), int(v)) for k, v in df.driller_name.dropna().replace("", np.nan).dropna()
                .value_counts().head(3).items()]

    # cost model: $/ft by geology
    if denver_share > 0.35:
        rate, geo = (50, 70), "Denver Basin sedimentary bedrock"
    elif county_hint in MOUNTAIN:
        rate, geo = (65, 100), "fractured mountain bedrock (granite / gneiss)"
    elif county_hint in PLAINS:
        rate, geo = (35, 55), "alluvium and plains sedimentary rock"
    else:
        rate, geo = (50, 75), "mixed sedimentary and alluvial ground"
    med = q(dep, 50)
    cost = None
    if med:
        cost = {"hole_low": int(round(med * rate[0], -2)), "hole_high": int(round(med * rate[1], -2)),
                "total_low": int(round(med * rate[0] + 6000, -2)), "total_high": int(round(med * rate[1] + 12000, -2)),
                "rate_low": rate[0], "rate_high": rate[1], "geology": geo}

    return {
        "wells_total": int(n), "wells_constructed": int(len(constructed)),
        "wells_with_depth": int(d_valid.shape[0]), "wells_with_yield": int(y_valid.shape[0]),
        "wells_with_swl": int(swl.dropna().shape[0]),
        "depth_median": r0(med), "depth_p25": r0(q(dep, 25)), "depth_p75": r0(q(dep, 75)), "depth_p90": r0(q(dep, 90)),
        "depth_max": r0(q(dep, 100)), "depth_bands": depth_bands,
        "yield_median": (round(q(yld, 50), 1) if len(y_valid) else None), "yield_p25": (round(q(yld, 25), 1) if len(y_valid) else None),
        "yield_p75": (round(q(yld, 75), 1) if len(y_valid) else None), "yield_bands": yield_bands,
        "swl_median": r0(q(swl, 50)), "swl_p25": r0(q(swl, 25)), "swl_p75": r0(q(swl, 75)), "swl_trend_by_decade": trend,
        "use_domestic_share": float(dom / n) if n else 0, "use_stock_share": float(stock / n) if n else 0,
        "use_irrigation_share": float(irr / n) if n else 0, "use_monitoring_share": float(mon / n) if n else 0,
        "aquifers_top": aq_top, "denver_basin_share": denver_share,
        "permits_by_decade": by_decade, "newest_permit_year": (int(newest_year) if pd.notna(newest_year) else None),
        "top_drillers": drillers, "cost": cost,
    }


def main() -> None:
    wells = pd.read_parquet(DATA / "co_wells.parquet")
    wells["county"] = wells.county.fillna("").str.upper().str.strip()
    print(f"wells loaded: {len(wells):,}")
    lat = wells.latitude.to_numpy(float)
    lon = wells.longitude.to_numpy(float)
    contam = load_contamination()

    def contam_within(clat, clon, miles=10):
        out = {}
        for k, arr in contam.items():
            if len(arr) == 0:
                out[k] = 0
                continue
            d = haversine_miles(clat, clon, arr[:, 0], arr[:, 1])
            out[k] = int((d <= miles).sum())
        return out

    pages: list[dict] = []

    # ---- counties
    for cty in CO_COUNTIES:
        sub = wells[wells.county == cty]
        if len(sub) < 30:
            print(f"  skip county {cty}: {len(sub)} wells")
            continue
        name = titlecase_county(cty) + " County"
        clat, clon = float(sub.latitude.median()), float(sub.longitude.median())
        rec = {"kind": "county", "name": name, "county": cty, "slug": slugify(name),
               "lat": round(clat, 4), "lon": round(clon, 4), "radius_miles": None,
               "region": ("plains" if cty in PLAINS else "mountain" if cty in MOUNTAIN else "other"),
               "contamination_10mi": contam_within(clat, clon, 15)}
        rec.update(stats_for(sub, cty))
        pages.append(rec)
    print(f"county pages: {len(pages)}")

    # ---- places: gazetteer + supplemental from wells.city
    places = pd.read_csv(DATA / "co_places.csv")
    places["name"] = places.name.str.replace(r"\s+(city|town|CDP)$", "", regex=True).str.strip()
    gaz_names = set(places.name.str.upper())
    city_counts = wells.city.fillna("").str.upper().str.strip().replace("", np.nan).dropna().value_counts()
    extra = []
    for cname, cnt in city_counts.items():
        if cnt >= 120 and cname not in gaz_names and re.fullmatch(r"[A-Z .'-]+", cname):
            sub = wells[wells.city.fillna("").str.upper().str.strip() == cname]
            extra.append({"name": cname.title(), "lsad": "unincorporated", "lat": float(sub.latitude.median()),
                          "lon": float(sub.longitude.median()), "aland_sqmi": np.nan})
    if extra:
        places = pd.concat([places, pd.DataFrame(extra)], ignore_index=True)
    print(f"candidate places: {len(places)} (incl. {len(extra)} unincorporated from well records)")

    place_pages = []
    for _, p in places.iterrows():
        d = haversine_miles(p.lat, p.lon, lat, lon)
        radius = 5
        for radius in (5, 8, 12):
            m = d <= radius
            if m.sum() >= 80:
                break
        if m.sum() < 40:
            continue
        sub = wells[m]
        cty = sub.county.replace("", np.nan).mode()
        cty = str(cty.iloc[0]) if len(cty) else ""
        if cty not in CO_COUNTIES:
            continue
        name = f"{p['name']}, CO"
        rec = {"kind": "place", "name": p["name"], "display": name, "county": cty,
               "county_name": titlecase_county(cty) + " County", "slug": slugify(f"{p['name']}-co"),
               "lat": round(float(p.lat), 4), "lon": round(float(p.lon), 4), "radius_miles": int(radius),
               "place_type": str(p.lsad), "region": ("plains" if cty in PLAINS else "mountain" if cty in MOUNTAIN else "other"),
               "contamination_10mi": contam_within(p.lat, p.lon, 10)}
        rec.update(stats_for(sub, cty))
        place_pages.append(rec)
    # de-dupe slugs (e.g. same name city+CDP)
    seen = set()
    uniq = []
    for r in sorted(place_pages, key=lambda r: -r["wells_total"]):
        if r["slug"] in seen:
            continue
        seen.add(r["slug"])
        uniq.append(r)
    place_pages = uniq
    print(f"place pages: {len(place_pages)}")

    # ---- neighbors for internal linking
    pl_lat = np.array([r["lat"] for r in place_pages])
    pl_lon = np.array([r["lon"] for r in place_pages])
    for r in place_pages:
        d = haversine_miles(r["lat"], r["lon"], pl_lat, pl_lon)
        idx = np.argsort(d)[1:7]
        r["neighbors"] = [{"name": place_pages[i]["display"], "slug": place_pages[i]["slug"], "miles": int(round(d[i]))} for i in idx]
    for c in pages:
        kids = [r for r in place_pages if r["county"] == c["county"]]
        c["places"] = [{"name": k["display"], "slug": k["slug"], "wells": k["wells_total"]} for k in sorted(kids, key=lambda k: -k["wells_total"])]
        # neighboring counties by centroid
        d = haversine_miles(c["lat"], c["lon"], np.array([x["lat"] for x in pages]), np.array([x["lon"] for x in pages]))
        idx = np.argsort(d)[1:6]
        c["neighbors"] = [{"name": pages[i]["name"], "slug": pages[i]["slug"], "miles": int(round(d[i]))} for i in idx]

    all_pages = pages + place_pages
    (DATA / "stats.json").write_text(json.dumps(all_pages, indent=1, default=lambda o: None), encoding="utf-8")
    print(f"\nTOTAL pages: {len(all_pages)} -> {DATA / 'stats.json'}")
    for r in all_pages:
        if r["county"] in ("TELLER", "PARK") and (r["kind"] == "county" or r["name"] in ("Florissant", "Divide", "Woodland Park", "Cripple Creek", "Guffey", "Lake George", "Hartsel")):
            print(f"  {r['kind']:<6} {r.get('display', r['name']):<24} wells={r['wells_total']:>6} depth_med={r['depth_median']} yield_med={r['yield_median']} swl_med={r['swl_median']} r={r['radius_miles']} cost={r['cost'] and r['cost']['total_low']}-{r['cost'] and r['cost']['total_high']}")


if __name__ == "__main__":
    main()
