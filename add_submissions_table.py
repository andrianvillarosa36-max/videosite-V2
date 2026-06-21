import os
import psycopg2

def main():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise Exception('DATABASE_URL environment variable not set')
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            category TEXT DEFAULT '',
            type TEXT DEFAULT 'video',
            catbox_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    print('submissions table created (or already existed).')

if __name__ == '__main__':
    main()
