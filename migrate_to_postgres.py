import json
import os
import psycopg2

DATA_DIR = 'data'

def get_connection():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise Exception('DATABASE_URL environment variable not set')
    return psycopg2.connect(db_url)

def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    salt TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    avatar TEXT DEFAULT '😀',
    bio TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS videos (
    id SERIAL PRIMARY KEY,
    title TEXT,
    filename TEXT,
    url TEXT,
    thumb TEXT,
    category TEXT,
    type TEXT DEFAULT 'video',
    episodes JSONB,
    views INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comments (
    id BIGINT PRIMARY KEY,
    video_url TEXT,
    username TEXT,
    avatar TEXT DEFAULT '😀',
    text TEXT,
    time TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    sender TEXT,
    recipient TEXT,
    content TEXT,
    type TEXT DEFAULT 'text',
    time TEXT,
    read BOOLEAN DEFAULT FALSE,
    edited BOOLEAN DEFAULT FALSE,
    reactions JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS likes (
    id SERIAL PRIMARY KEY,
    username TEXT,
    video_url TEXT,
    action TEXT
);

CREATE TABLE IF NOT EXISTS ratings (
    id SERIAL PRIMARY KEY,
    username TEXT,
    video_url TEXT,
    rating INTEGER
);

CREATE TABLE IF NOT EXISTS favorites (
    id SERIAL PRIMARY KEY,
    username TEXT,
    video_url TEXT
);

CREATE TABLE IF NOT EXISTS progress (
    id SERIAL PRIMARY KEY,
    username TEXT,
    video_url TEXT,
    current_time_sec REAL DEFAULT 0,
    duration REAL DEFAULT 0,
    watched BOOLEAN DEFAULT FALSE
);
"""

def main():
    conn = get_connection()
    cur = conn.cursor()

    print('Creating tables...')
    cur.execute(SCHEMA)
    conn.commit()
    print('Tables created.')

    # --- users ---
    users = load_json('users.json')
    for u in users:
        cur.execute(
            """INSERT INTO users (username, password, salt, role, avatar, bio)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (username) DO NOTHING""",
            (u.get('username'), u.get('password'), u.get('salt'),
             u.get('role', 'viewer'), u.get('avatar', '😀'), u.get('bio', ''))
        )
    print(f'Imported {len(users)} users.')

    # --- videos ---
    videos = load_json('videos.json')
    for v in videos:
        episodes = json.dumps(v.get('episodes')) if v.get('episodes') else None
        cur.execute(
            """INSERT INTO videos (title, filename, url, thumb, category, type, episodes, views)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (v.get('title'), v.get('filename'), v.get('url'), v.get('thumb'),
             v.get('category'), v.get('type', 'video'), episodes, v.get('views', 0))
        )
    print(f'Imported {len(videos)} videos.')

    # --- comments ---
    comments = load_json('comments.json')
    for c in comments:
        cur.execute(
            """INSERT INTO comments (id, video_url, username, avatar, text, time)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (int(c.get('id')), c.get('videoUrl'), c.get('username'),
             c.get('avatar', '😀'), c.get('text'), c.get('time'))
        )
    print(f'Imported {len(comments)} comments.')

    # --- messages ---
    messages = load_json('messages.json')
    for m in messages:
        reactions = json.dumps(m.get('reactions', {}))
        cur.execute(
            """INSERT INTO messages (sender, recipient, content, type, time, read, edited, reactions)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (m.get('from'), m.get('to'), m.get('content'), m.get('type', 'text'),
             m.get('time'), m.get('read', False), m.get('edited', False), reactions)
        )
    print(f'Imported {len(messages)} messages.')

    # --- likes ---
    likes = load_json('likes.json')
    for l in likes:
        cur.execute(
            """INSERT INTO likes (username, video_url, action)
               VALUES (%s, %s, %s)""",
            (l.get('username'), l.get('videoUrl'), l.get('action'))
        )
    print(f'Imported {len(likes)} likes.')

    # --- ratings ---
    ratings = load_json('ratings.json')
    for r in ratings:
        cur.execute(
            """INSERT INTO ratings (username, video_url, rating)
               VALUES (%s, %s, %s)""",
            (r.get('username'), r.get('videoUrl'), r.get('rating'))
        )
    print(f'Imported {len(ratings)} ratings.')

    # --- favorites ---
    favorites = load_json('favorites.json')
    for f in favorites:
        cur.execute(
            """INSERT INTO favorites (username, video_url)
               VALUES (%s, %s)""",
            (f.get('username'), f.get('videoUrl'))
        )
    print(f'Imported {len(favorites)} favorites.')

    # --- progress ---
    progress = load_json('progress.json')
    for p in progress:
        cur.execute(
            """INSERT INTO progress (username, video_url, current_time_sec, duration, watched)
               VALUES (%s, %s, %s, %s, %s)""",
            (p.get('username'), p.get('videoUrl'), p.get('current', 0),
             p.get('duration', 0), p.get('watched', False))
        )
    print(f'Imported {len(progress)} progress entries.')

    conn.commit()
    cur.close()
    conn.close()
    print('\nMigration complete.')

if __name__ == '__main__':
    main()
