import os
import psycopg2
import psycopg2.extras

ADULT_CATEGORIES = ['Anime corn', 'NTR Anime corn', 'Anime series corn', 'Corn']

def main():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise Exception('DATABASE_URL environment variable not set')
    conn = psycopg2.connect(db_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Find which videos will be deleted (main-site, non-adult)
    cur.execute(
        "SELECT id, title, filename, url, category, type FROM videos WHERE category NOT IN %s",
        (tuple(ADULT_CATEGORIES),)
    )
    targets = cur.fetchall()

    print(f"Found {len(targets)} main-site videos to delete:")
    for t in targets:
        print(f"  - [{t['id']}] {t['title']} ({t['category']})")

    if not targets:
        print("Nothing to delete.")
        cur.close()
        conn.close()
        return

    confirm = input("\nType DELETE to confirm permanent removal: ")
    if confirm != 'DELETE':
        print("Aborted, nothing deleted.")
        cur.close()
        conn.close()
        return

    video_keys = [(t['type'] == 'series' and t['filename']) or t['url'] for t in targets]

    cur.execute("DELETE FROM comments WHERE video_url = ANY(%s)", (video_keys,))
    print(f"Deleted {cur.rowcount} comments")

    cur.execute("DELETE FROM ratings WHERE video_url = ANY(%s)", (video_keys,))
    print(f"Deleted {cur.rowcount} ratings")

    cur.execute("DELETE FROM likes WHERE video_url = ANY(%s)", (video_keys,))
    print(f"Deleted {cur.rowcount} likes")

    cur.execute("DELETE FROM favorites WHERE video_url = ANY(%s)", (video_keys,))
    print(f"Deleted {cur.rowcount} favorites")

    cur.execute("DELETE FROM progress WHERE video_url = ANY(%s)", (video_keys,))
    print(f"Deleted {cur.rowcount} progress rows")

    cur.execute("DELETE FROM videos WHERE category NOT IN %s", (tuple(ADULT_CATEGORIES),))
    print(f"Deleted {cur.rowcount} videos")

    conn.commit()
    cur.close()
    conn.close()
    print("\nDone.")

if __name__ == '__main__':
    main()
