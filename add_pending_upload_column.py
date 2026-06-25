import os
import psycopg2

def main():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise Exception('DATABASE_URL environment variable not set')
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    cur.execute("""
        ALTER TABLE submissions ADD COLUMN IF NOT EXISTS pending_files JSONB;
    """)
    conn.commit()
    cur.close()
    conn.close()
    print('pending_files column added (or already existed).')

if __name__ == '__main__':
    main()
