import os
import psycopg2

def main():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise Exception('DATABASE_URL environment variable not set')
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    cur.execute("""
        ALTER TABLE submissions ADD COLUMN IF NOT EXISTS episodes JSONB;
    """)
    conn.commit()
    cur.close()
    conn.close()
    print('episodes column added to submissions (or already existed).')

if __name__ == '__main__':
    main()
