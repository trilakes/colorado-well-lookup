"""
download_wells.py — Download Arizona + New Mexico wells into SQLite
Uses parallel workers for maximum speed.

Usage:
  python download_wells.py          # download both states
  python download_wells.py az       # Arizona only
  python download_wells.py nm       # New Mexico only
"""
import os
import sys
import time
import sqlite3
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── ArcGIS Endpoints ─────────────────────────────────────────────────────────

AZ_URL = "https://services.arcgis.com/C34zQ7veRS0V1t04/arcgis/rest/services/Well_Registry_2024/FeatureServer/0/query"
NM_URL = "https://services2.arcgis.com/qXZbWTdPDbTjl7Dy/arcgis/rest/services/OSE_Points_of_Diversion/FeatureServer/0/query"

AZ_FIELDS = ",".join([
    "REGISTRY_ID", "OWNER_NAME", "WELLTYPE", "WELL_TYPE_GROUP",
    "WELL_DEPTH", "WATER_LEVEL", "CASING_DEPTH", "CASING_DIAMETER",
    "PUMPRATE", "TESTEDRATE", "DRAW_DOWN", "COUNTY",
    "BASIN_NAME", "SUBBASIN_NAME", "AMA", "WATERSHED",
    "WATER_USE", "SITE_USE", "CITY", "STATE", "ZIP",
    "APPLICATION_DATE", "APPROVED", "INSTALLED",
    "DLIC_NUM", "PUMP_TYPE", "PUMP_POWER", "CADASTRAL",
    "SECTION", "QUARTER_160_ACRE", "QUARTER_40_ACRE", "QUARTER_10_ACRE",
    "WHOLE_TOWNSHIP", "HALF_TOWNSHIP", "NORTHSOUTH",
    "WHOLE_RANGE", "HALF_RANGE", "EASTWEST",
    "ADDRESS1", "ADDRESS2", "WELL_CANCELLED",
    "UTM_X_METERS", "UTM_Y_METERS", "PROGRAM",
    "CASING_TYPE", "COMPLETION_REPORT_STATUS", "DRILL_LOG",
])

NM_FIELDS = ",".join([
    "pod_nbr", "pod_suffix", "pod_name", "pod_basin", "pod_status",
    "county", "tws", "rng", "sec", "qtr_4th", "qtr_16th", "qtr_64th",
    "elevation", "depth_well", "depth_wate", "static_lev",
    "aquifer", "use_of_wel", "pump_type", "discharge", "casing_siz",
    "start_date", "finish_dat", "plug_date",
    "license_nb", "own_lname", "own_fname",
    "addr1", "addr2", "city", "state", "zip",
    "contact_ln", "contact_fn",
    "basin", "sub_basin", "status", "use_", "pod_file",
    "subdiv_nam", "easting", "northing", "utm_zone", "datum",
    "nmwrrs_wrs", "total_div", "metered", "ref",
])

PAGE_SIZE = 2000
MAX_WORKERS = 10
MAX_RETRIES = 3
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Same column schema as Colorado wells ─────────────────────────────────────

COLUMNS = [
    ("receipt", "TEXT"), ("permit", "TEXT"), ("wdid", "TEXT"),
    ("status", "TEXT"), ("category", "TEXT"),
    ("latitude", "DOUBLE PRECISION"), ("longitude", "DOUBLE PRECISION"),
    ("utm_x", "DOUBLE PRECISION"), ("utm_y", "DOUBLE PRECISION"),
    ("elevation", "DOUBLE PRECISION"), ("location_accuracy", "TEXT"),
    ("county", "TEXT"), ("division", "INTEGER"), ("water_district", "INTEGER"),
    ("pm", "TEXT"), ("township", "TEXT"), ("range", "TEXT"),
    ("section", "TEXT"), ("q160", "TEXT"), ("q40", "TEXT"), ("q10", "TEXT"),
    ("address", "TEXT"), ("city", "TEXT"), ("state", "TEXT"),
    ("zip_code", "TEXT"), ("parcel_name", "TEXT"),
    ("depth_total", "DOUBLE PRECISION"), ("top_perforated", "DOUBLE PRECISION"),
    ("bottom_perforated", "DOUBLE PRECISION"), ("pump_yield_gpm", "DOUBLE PRECISION"),
    ("static_water_level", "DOUBLE PRECISION"), ("static_water_level_date", "TEXT"),
    ("aquifers", "TEXT"), ("as_built_aquifers", "TEXT"), ("uses", "TEXT"),
    ("designated_basin", "TEXT"), ("management_district", "TEXT"),
    ("denver_basin_aquifer", "TEXT"),
    ("date_application", "TEXT"), ("date_permit_issued", "TEXT"),
    ("date_first_use", "TEXT"), ("date_expires", "TEXT"),
    ("date_completed", "TEXT"), ("date_pump_installed", "TEXT"),
    ("date_plugged", "TEXT"), ("date_modified", "TEXT"),
    ("driller_license", "TEXT"), ("driller_name", "TEXT"),
    ("pump_license", "TEXT"), ("pump_installer", "TEXT"),
    ("owner_name", "TEXT"), ("owner_address", "TEXT"),
    ("owner_city", "TEXT"), ("owner_state", "TEXT"), ("owner_zip", "TEXT"),
    ("contact_type", "TEXT"), ("location_type", "TEXT"),
    ("case_numbers", "TEXT"), ("more_info_url", "TEXT"),
    ("downloaded_at", "TEXT"),
]

COL_NAMES = [c[0] for c in COLUMNS]


# ── Helpers ──────────────────────────────────────────────────────────────────

def epoch_to_date(ms):
    """Convert ArcGIS epoch milliseconds to ISO date string."""
    if ms is None or ms == 0:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%d')
    except (OSError, ValueError):
        return None


def safe_float(val):
    """Convert to float, return None if invalid or zero."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


def safe_int(val):
    """Convert to int, return None if invalid or zero."""
    if val is None:
        return None
    try:
        i = int(val)
        return i if i != 0 else None
    except (ValueError, TypeError):
        return None


def clean_str(val):
    """Clean whitespace from string, return None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def parse_discharge(val):
    """Parse NM discharge field (text) to numeric GPM."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Try to extract a number
    import re
    m = re.search(r'[\d.]+', s)
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


# ── Field Mapping ────────────────────────────────────────────────────────────

def map_az_record(feat):
    """Map Arizona ArcGIS feature to CO schema dict."""
    a = feat.get('attributes', {})
    g = feat.get('geometry', {})
    now = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')

    # Build township string
    twp_parts = [str(a.get('WHOLE_TOWNSHIP') or ''), str(a.get('HALF_TOWNSHIP') or '')]
    twp = ''.join(p for p in twp_parts if p).strip()
    if twp and a.get('NORTHSOUTH'):
        twp += a['NORTHSOUTH']

    # Build range string
    rng_parts = [str(a.get('WHOLE_RANGE') or ''), str(a.get('HALF_RANGE') or '')]
    rng = ''.join(p for p in rng_parts if p).strip()
    if rng and a.get('EASTWEST'):
        rng += a['EASTWEST']

    # Status from WELL_CANCELLED
    cancelled = clean_str(a.get('WELL_CANCELLED'))
    status = 'Cancelled' if cancelled and cancelled.upper() == 'Y' else 'Active'

    return {
        'receipt': clean_str(a.get('REGISTRY_ID')),
        'permit': None,
        'wdid': None,
        'status': status,
        'category': clean_str(a.get('WELLTYPE')),
        'latitude': g.get('y'),
        'longitude': g.get('x'),
        'utm_x': safe_float(a.get('UTM_X_METERS')),
        'utm_y': safe_float(a.get('UTM_Y_METERS')),
        'elevation': None,
        'location_accuracy': None,
        'county': clean_str(a.get('COUNTY')),
        'division': None,
        'water_district': None,
        'pm': None,
        'township': clean_str(twp) if twp else None,
        'range': clean_str(rng) if rng else None,
        'section': clean_str(a.get('SECTION')),
        'q160': clean_str(a.get('QUARTER_160_ACRE')),
        'q40': clean_str(a.get('QUARTER_40_ACRE')),
        'q10': clean_str(a.get('QUARTER_10_ACRE')),
        'address': clean_str(a.get('ADDRESS1')),
        'city': clean_str(a.get('CITY')),
        'state': 'AZ',
        'zip_code': clean_str(a.get('ZIP')),
        'parcel_name': None,
        'depth_total': safe_float(a.get('WELL_DEPTH')),
        'top_perforated': safe_float(a.get('CASING_DEPTH')),
        'bottom_perforated': None,
        'pump_yield_gpm': safe_float(a.get('PUMPRATE')),
        'static_water_level': safe_float(a.get('WATER_LEVEL')),
        'static_water_level_date': None,
        'aquifers': clean_str(a.get('BASIN_NAME')),
        'as_built_aquifers': clean_str(a.get('SUBBASIN_NAME')),
        'uses': clean_str(a.get('WATER_USE')),
        'designated_basin': clean_str(a.get('AMA')),
        'management_district': clean_str(a.get('WATERSHED')),
        'denver_basin_aquifer': None,
        'date_application': epoch_to_date(a.get('APPLICATION_DATE')),
        'date_permit_issued': epoch_to_date(a.get('APPROVED')),
        'date_first_use': None,
        'date_expires': None,
        'date_completed': epoch_to_date(a.get('INSTALLED')),
        'date_pump_installed': None,
        'date_plugged': None,
        'date_modified': None,
        'driller_license': clean_str(a.get('DLIC_NUM')),
        'driller_name': None,
        'pump_license': None,
        'pump_installer': None,
        'owner_name': clean_str(a.get('OWNER_NAME')),
        'owner_address': clean_str(a.get('ADDRESS1')),
        'owner_city': clean_str(a.get('CITY')),
        'owner_state': clean_str(a.get('STATE')),
        'owner_zip': clean_str(a.get('ZIP')),
        'contact_type': clean_str(a.get('SITE_USE')),
        'location_type': clean_str(a.get('CADASTRAL')),
        'case_numbers': None,
        'more_info_url': None,
        'downloaded_at': now,
    }


def map_nm_record(feat):
    """Map New Mexico ArcGIS feature to CO schema dict."""
    a = feat.get('attributes', {})
    g = feat.get('geometry', {})
    now = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')

    # Combine owner name
    lname = clean_str(a.get('own_lname'))
    fname = clean_str(a.get('own_fname'))
    if lname and fname:
        owner = f"{lname}, {fname}"
    elif lname:
        owner = lname
    elif fname:
        owner = fname
    else:
        owner = None

    # Combine contact name
    cl = clean_str(a.get('contact_ln'))
    cf = clean_str(a.get('contact_fn'))
    contact = f"{cl}, {cf}" if cl and cf else (cl or cf)

    # Build receipt from pod_file or pod_nbr
    receipt = clean_str(a.get('pod_file')) or clean_str(a.get('pod_nbr'))

    # Status mapping
    raw_status = clean_str(a.get('pod_status')) or clean_str(a.get('status'))
    status_map = {'ACT': 'Active', 'PLG': 'Plugged', 'ABN': 'Abandoned',
                  'INJ': 'Injection', 'MON': 'Monitor', 'OTH': 'Other'}
    status = status_map.get(raw_status, raw_status)

    # Use field
    use_val = clean_str(a.get('use_of_wel')) or clean_str(a.get('use_'))

    # Water level — use depth_wate or static_lev
    wl = safe_float(a.get('depth_wate')) or safe_float(a.get('static_lev'))

    # NMWRRS link
    wrs_id = clean_str(a.get('nmwrrs_wrs'))
    more_info = f"https://nmwrrs.ose.nm.gov/nmwrrs/well-log-meter-info-form?wrs={wrs_id}" if wrs_id else None

    return {
        'receipt': receipt,
        'permit': clean_str(a.get('ref')),
        'wdid': None,
        'status': status,
        'category': use_val,
        'latitude': g.get('y') if g else None,
        'longitude': g.get('x') if g else None,
        'utm_x': safe_float(a.get('easting')),
        'utm_y': safe_float(a.get('northing')),
        'elevation': safe_float(a.get('elevation')),
        'location_accuracy': None,
        'county': clean_str(a.get('county')),
        'division': None,
        'water_district': None,
        'pm': None,
        'township': clean_str(a.get('tws')),
        'range': clean_str(a.get('rng')),
        'section': clean_str(a.get('sec')),
        'q160': clean_str(a.get('qtr_4th')),
        'q40': clean_str(a.get('qtr_16th')),
        'q10': clean_str(a.get('qtr_64th')),
        'address': clean_str(a.get('addr1')),
        'city': clean_str(a.get('city')),
        'state': 'NM',
        'zip_code': clean_str(a.get('zip')),
        'parcel_name': clean_str(a.get('subdiv_nam')),
        'depth_total': safe_float(a.get('depth_well')),
        'top_perforated': None,
        'bottom_perforated': None,
        'pump_yield_gpm': parse_discharge(a.get('discharge')),
        'static_water_level': wl,
        'static_water_level_date': None,
        'aquifers': clean_str(a.get('aquifer')),
        'as_built_aquifers': None,
        'uses': use_val,
        'designated_basin': clean_str(a.get('basin')),
        'management_district': clean_str(a.get('sub_basin')),
        'denver_basin_aquifer': None,
        'date_application': epoch_to_date(a.get('start_date')),
        'date_permit_issued': None,
        'date_first_use': None,
        'date_expires': None,
        'date_completed': epoch_to_date(a.get('finish_dat')),
        'date_pump_installed': None,
        'date_plugged': epoch_to_date(a.get('plug_date')),
        'date_modified': None,
        'driller_license': str(a.get('license_nb')) if a.get('license_nb') else None,
        'driller_name': None,
        'pump_license': None,
        'pump_installer': None,
        'owner_name': owner,
        'owner_address': clean_str(a.get('addr1')),
        'owner_city': clean_str(a.get('city')),
        'owner_state': clean_str(a.get('state')),
        'owner_zip': clean_str(a.get('zip')),
        'contact_type': contact,
        'location_type': None,
        'case_numbers': None,
        'more_info_url': more_info,
        'downloaded_at': now,
    }


# ── Download Engine ──────────────────────────────────────────────────────────

def fetch_page(url, fields, offset, page_size):
    """Fetch one page of records from ArcGIS FeatureServer. Returns list of features."""
    params = {
        'where': '1=1',
        'outFields': fields,
        'outSR': '4326',     # lat/lon coordinates
        'resultOffset': offset,
        'resultRecordCount': page_size,
        'f': 'json',
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=90)
            r.raise_for_status()
            data = r.json()
            if 'error' in data:
                raise Exception(f"ArcGIS error: {data['error']}")
            return data.get('features', [])
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)  # exponential backoff
            else:
                print(f"\n  FAILED page at offset {offset}: {e}", flush=True)
                return []


def get_total_count(url):
    """Get total record count from ArcGIS FeatureServer."""
    params = {'where': '1=1', 'returnCountOnly': 'true', 'f': 'json'}
    r = requests.get(url, params=params, timeout=30)
    return r.json().get('count', 0)


def download_state(state_code, url, fields, mapper):
    """
    Download all records for a state using parallel workers.
    Returns list of mapped record dicts.
    """
    print(f"", flush=True)
    print(f"  ┌─ {state_code} Download ─────────────────────────────────────", flush=True)
    print(f"  │", flush=True)

    # Get total count
    total = get_total_count(url)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  │  Records:  {total:,}", flush=True)
    print(f"  │  Pages:    {pages} × {PAGE_SIZE:,}", flush=True)
    print(f"  │  Workers:  {MAX_WORKERS}", flush=True)
    print(f"  │", flush=True)

    # Generate all page offsets
    offsets = list(range(0, total, PAGE_SIZE))

    # Parallel download
    all_features = []
    completed = 0
    failed = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_page, url, fields, offset, PAGE_SIZE): offset
            for offset in offsets
        }

        for future in as_completed(futures):
            offset = futures[future]
            try:
                features = future.result()
                all_features.extend(features)
                completed += 1
            except Exception as e:
                print(f"\n  │  ERROR offset={offset}: {e}", flush=True)
                failed += 1
                completed += 1

            elapsed = time.time() - t0
            pct = (completed / pages) * 100
            rate = len(all_features) / elapsed if elapsed > 0 else 0
            remaining = ((pages - completed) / (completed / elapsed)) if completed > 0 and elapsed > 0 else 0

            bar_len = 30
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"\r  │  [{bar}] {completed}/{pages}  {pct:5.1f}%  {len(all_features):,} wells  {rate:,.0f}/s  ETA {remaining:.0f}s  ", end="", flush=True)

    elapsed = time.time() - t0
    print(f"", flush=True)
    print(f"  │", flush=True)
    print(f"  │  Downloaded: {len(all_features):,} features in {elapsed:.1f}s", flush=True)
    if failed:
        print(f"  │  Failed pages: {failed}", flush=True)

    # Map to schema
    print(f"  │  Mapping fields...", flush=True)
    t1 = time.time()
    records = []
    for feat in all_features:
        try:
            records.append(mapper(feat))
        except Exception as e:
            pass  # skip bad records silently
    print(f"  │  Mapped: {len(records):,} records in {time.time()-t1:.1f}s", flush=True)
    print(f"  │", flush=True)

    return records


def save_to_sqlite(records, db_path, state_code):
    """Save records to SQLite database."""
    print(f"  │  Saving to {os.path.basename(db_path)}...", flush=True)
    t0 = time.time()

    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Create table
    col_defs = ", ".join(f"{name} {typ}" for name, typ in COLUMNS)
    conn.execute(f"CREATE TABLE wells (id INTEGER PRIMARY KEY AUTOINCREMENT, {col_defs})")

    # Bulk insert
    placeholders = ", ".join(["?"] * len(COL_NAMES))
    insert_sql = f"INSERT INTO wells ({', '.join(COL_NAMES)}) VALUES ({placeholders})"

    batch_size = 5000
    total_inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        rows = [tuple(rec.get(col) for col in COL_NAMES) for rec in batch]
        conn.executemany(insert_sql, rows)
        conn.commit()
        total_inserted += len(batch)

    # Create indexes
    conn.execute("CREATE INDEX idx_bbox ON wells (latitude, longitude) WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
    conn.execute("CREATE INDEX idx_county ON wells (county)")
    conn.execute("CREATE INDEX idx_receipt ON wells (receipt)")
    conn.execute("CREATE INDEX idx_depth ON wells (depth_total)")
    conn.execute("CREATE INDEX idx_status ON wells (status)")
    conn.commit()

    # Verify
    count = conn.execute("SELECT COUNT(*) FROM wells").fetchone()[0]
    with_depth = conn.execute("SELECT COUNT(*) FROM wells WHERE depth_total IS NOT NULL").fetchone()[0]
    with_coords = conn.execute("SELECT COUNT(*) FROM wells WHERE latitude IS NOT NULL AND longitude IS NOT NULL").fetchone()[0]
    conn.close()

    elapsed = time.time() - t0
    print(f"  │  Saved: {count:,} wells in {elapsed:.1f}s", flush=True)
    print(f"  │  With depth: {with_depth:,} ({with_depth/count*100:.0f}%)", flush=True)
    print(f"  │  With coords: {with_coords:,} ({with_coords/count*100:.0f}%)", flush=True)
    print(f"  └──────────────────────────────────────────────────────────", flush=True)
    return count


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    target = sys.argv[1].lower() if len(sys.argv) > 1 else 'both'

    print(f"", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)
    print(f"  Well Data Downloader — Parallel ArcGIS Fetcher", flush=True)
    print(f"  Workers: {MAX_WORKERS}  |  Page Size: {PAGE_SIZE:,}", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)

    results = {}
    t_start = time.time()

    if target in ('az', 'both'):
        db_path = os.path.join(SCRIPT_DIR, 'az_wells.db')
        records = download_state('AZ', AZ_URL, AZ_FIELDS, map_az_record)
        count = save_to_sqlite(records, db_path, 'AZ')
        results['AZ'] = count

    if target in ('nm', 'both'):
        db_path = os.path.join(SCRIPT_DIR, 'nm_wells.db')
        records = download_state('NM', NM_URL, NM_FIELDS, map_nm_record)
        count = save_to_sqlite(records, db_path, 'NM')
        results['NM'] = count

    total_time = time.time() - t_start
    print(f"", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)
    print(f"  COMPLETE!", flush=True)
    for st, cnt in results.items():
        print(f"    {st}: {cnt:,} wells", flush=True)
    total_wells = sum(results.values())
    print(f"    Total: {total_wells:,} wells downloaded", flush=True)
    print(f"    Time:  {total_time:.1f}s ({total_time/60:.1f} min)", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)


if __name__ == '__main__':
    main()
