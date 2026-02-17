import psycopg2, os
os.environ['DATABASE_URL'] = 'postgresql://wells_admin:03khu1t6f2bw3SSsfxLeFXEC8ilvc8UA@dpg-d67la5gboq4c7381t6eg-a.oregon-postgres.render.com/wells_db_yxxi'
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='wells' AND column_name='well_state'")
has_col = cur.fetchone()
print(f'well_state column exists: {bool(has_col)}')
if has_col:
    cur.execute('SELECT well_state, COUNT(*) FROM wells GROUP BY well_state ORDER BY well_state')
    for row in cur.fetchall():
        print(f'  {row[0]}: {row[1]:,} wells')
cur.execute('SELECT COUNT(*) FROM wells')
print(f'TOTAL: {cur.fetchone()[0]:,} wells')
conn.close()
