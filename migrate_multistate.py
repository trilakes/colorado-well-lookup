"""
migrate_multistate.py — Add AZ and NM wells to existing PostgreSQL database

This script:
1. Adds a 'well_state' column to the existing wells table
2. Sets all existing rows to 'CO' 
3. Imports AZ and NM wells from their SQLite files
4. Creates an index on the new column

Usage:
  set DATABASE_URL=postgresql://...
  python migrate_multistate.py
"""
import os
import sys
import io
import time
import sqlite3
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', '')
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BATCH_SIZE = 10000

COLUMNS = [
    "receipt", "permit", "wdid", "status", "category",
    "latitude", "longitude", "utm_x", "utm_y",
    "elevation", "location_accuracy",
    "county", "division", "water_district",
    "pm", "township", "range",
    "section", "q160", "q40", "q10",
    "address", "city", "state",
    "zip_code", "parcel_name",
    "depth_total", "top_perforated",
    "bottom_perforated", "pump_yield_gpm",
    "static_water_level", "static_water_level_date",
    "aquifers", "as_built_aquifers", "uses",
    "designated_basin", "management_district",
    "denver_basin_aquifer",
    "date_application", "date_permit_issued",
    "date_first_use", "date_expires",
    "date_completed", "date_pump_installed",
    "date_plugged", "date_modified",
    "driller_license", "driller_name",
    "pump_license", "pump_installer",
    "owner_name", "owner_address",
    "owner_city", "owner_state", "owner_zip",
    "contact_type", "location_type",
    "case_numbers", "more_info_url",
    "downloaded_at",
]


def escape_copy(val):
    """Escape a value for PostgreSQL COPY format."""
    if val is None:
        return r'\N'
    s = str(val)
    s = s.replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')
    return s


def import_state(pg_conn, sqlite_path, state_code):
    """Import wells from a state SQLite file into PostgreSQL."""
    if not os.path.exists(sqlite_path):
        print(f"  SKIP: {sqlite_path} not found", flush=True)
        return 0

    print(f"", flush=True)
    print(f"  ┌─ {state_code} Migration ───────────────────────────────────", flush=True)
    print(f"  │", flush=True)

    # Read SQLite
    print(f"  │  [1/2] Reading {os.path.basename(sqlite_path)}...", flush=True)
    t0 = time.time()
    sqlite_conn = sqlite3.connect(sqlite_path)
    total = sqlite_conn.execute("SELECT COUNT(*) FROM wells").fetchone()[0]
    print(f"  │        {total:,} wells", flush=True)

    select_cols = ", ".join(COLUMNS)
    all_rows = sqlite_conn.execute(f"SELECT {select_cols} FROM wells").fetchall()
    sqlite_conn.close()
    print(f"  │        Read in {time.time()-t0:.1f}s", flush=True)

    # Upload via COPY with well_state column
    print(f"  │  [2/2] Uploading via COPY...", flush=True)
    upload_start = time.time()
    total_sent = 0
    copy_cols = COLUMNS + ['well_state']

    for batch_start in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[batch_start:batch_start + BATCH_SIZE]

        buf = io.StringIO()
        for row in batch:
            vals = [escape_copy(v) for v in row]
            vals.append(state_code)  # well_state
            buf.write("\t".join(vals) + "\n")
        buf.seek(0)

        with pg_conn.cursor() as cur:
            cur.copy_from(buf, 'wells', columns=copy_cols, null=r'\N')
        pg_conn.commit()

        total_sent += len(batch)
        elapsed = time.time() - upload_start
        rate = total_sent / elapsed if elapsed > 0 else 0
        pct = (total_sent / total) * 100

        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  │        [{bar}] {total_sent:>7,}/{total:,}  {pct:5.1f}%  {rate:,.0f}/s", end="", flush=True)

    upload_time = time.time() - upload_start
    print(f"", flush=True)
    print(f"  │        {total_sent:,} rows in {upload_time:.1f}s ({total_sent/upload_time:,.0f} rows/s)", flush=True)
    print(f"  └──────────────────────────────────────────────────────────", flush=True)
    return total_sent


def main():
    if not DATABASE_URL:
        print("ERROR: Set DATABASE_URL first", flush=True)
        sys.exit(1)

    print(f"", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)
    print(f"  Multi-State Wells Migration", flush=True)
    print(f"  Target: Render PostgreSQL", flush=True)
    print(f"  ═══════════════════════════════════════════════════════════", flush=True)

    # Connect
    print(f"", flush=True)
    print(f"  Connecting to PostgreSQL...", flush=True)
    for attempt in range(5):
        try:
            pg_conn = psycopg2.connect(DATABASE_URL, connect_timeout=60, sslmode='require')
            break
        except Exception as e:
            wait = (attempt + 1) * 15
            print(f"  Attempt {attempt+1}/5 failed: {e}", flush=True)
            print(f"  Retrying in {wait}s...", flush=True)
            time.sleep(wait)
    else:
        print("  FATAL: Could not connect after 5 attempts.", flush=True)
        sys.exit(1)

    pg_conn.autocommit = False

    # Step 1: Check current state
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wells")
        existing = cur.fetchone()[0]
        print(f"  Existing wells: {existing:,}", flush=True)

        # Check if well_state column exists
        cur.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'wells' AND column_name = 'well_state'
        """)
        has_state_col = cur.fetchone() is not None

    # Step 2: Add well_state column if needed
    if not has_state_col:
        print(f"  Adding 'well_state' column...", flush=True)
        with pg_conn.cursor() as cur:
            # DEFAULT 'CO' automatically sets value for all existing rows (PG 11+)
            cur.execute("ALTER TABLE wells ADD COLUMN well_state TEXT DEFAULT 'CO'")
        pg_conn.commit()
        print(f"  Column added — {existing:,} existing wells defaulted to 'CO'", flush=True)
    else:
        print(f"  'well_state' column already exists", flush=True)
        # Check if we already have AZ/NM data — avoid duplicates
        with pg_conn.cursor() as cur:
            cur.execute("SELECT well_state, COUNT(*) FROM wells GROUP BY well_state ORDER BY well_state")
            for row in cur.fetchall():
                print(f"    {row[0]}: {row[1]:,}", flush=True)

    # Step 3: Remove existing AZ/NM data if re-running
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM wells WHERE well_state = 'AZ'")
        az_deleted = cur.rowcount
        cur.execute("DELETE FROM wells WHERE well_state = 'NM'")
        nm_deleted = cur.rowcount
    pg_conn.commit()
    if az_deleted or nm_deleted:
        print(f"  Cleaned up: {az_deleted:,} AZ + {nm_deleted:,} NM old rows", flush=True)

    # Step 4: Import states
    t_start = time.time()
    totals = {}

    az_db = os.path.join(SCRIPT_DIR, 'az_wells.db')
    nm_db = os.path.join(SCRIPT_DIR, 'nm_wells.db')

    totals['AZ'] = import_state(pg_conn, az_db, 'AZ')
    totals['NM'] = import_state(pg_conn, nm_db, 'NM')

    # Step 5: Create index on well_state
    print(f"", flush=True)
    print(f"  Creating well_state index...", flush=True)
    with pg_conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_wells_state")
        cur.execute("CREATE INDEX idx_wells_state ON wells (well_state)")
    pg_conn.commit()
    print(f"  Done", flush=True)

    # Verify
    with pg_conn.cursor() as cur:
        cur.execute("SELECT well_state, COUNT(*) FROM wells GROUP BY well_state ORDER BY well_state")
        print(f"", flush=True)
        print(f"  ═══════════════════════════════════════════════════════════", flush=True)
        print(f"  MIGRATION COMPLETE!", flush=True)
        grand = 0
        for row in cur.fetchall():
            print(f"    {row[0]}: {row[1]:,} wells", flush=True)
            grand += row[1]
        print(f"    ──────────────────", flush=True)
        print(f"    TOTAL: {grand:,} wells", flush=True)
        print(f"    Time:  {time.time()-t_start:.1f}s", flush=True)
        print(f"  ═══════════════════════════════════════════════════════════", flush=True)

    pg_conn.close()


if __name__ == '__main__':
    main()
