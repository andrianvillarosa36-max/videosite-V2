import http.server
import json
import os
import hashlib
import secrets

from datetime import datetime, timedelta
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

online_users = {}
sessions = {}  # token -> { 'username': ..., 'role': ..., 'created': ... }

SESSION_LIFETIME = timedelta(hours=12)

VIDEO_DIR  = 'videos'
STATIC_DIR = 'static'


# ---------- Database helpers ----------

_db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            raise Exception('DATABASE_URL environment variable not set')
        _db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, db_url)
    return _db_pool

def ensure_schema():
    with db_cursor() as (conn, cur):
        cur.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS position INTEGER")
        cur.execute("UPDATE videos SET position = id WHERE position IS NULL")

def get_db():
    conn = get_pool().getconn()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def put_db(conn):
    get_pool().putconn(conn)

@contextmanager
def db_cursor():
    """
    Safe DB access: always returns the connection to the pool,
    even if an exception happens. Commits on success, rolls back on error.
    Usage: with db_cursor() as (conn, cur): ...
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        put_db(conn)


# ---------- Auth helpers ----------

def create_session(username, role):
    token = secrets.token_hex(32)
    sessions[token] = {'username': username, 'role': role, 'created': datetime.now()}
    return token

def get_session(self):
    auth = self.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:]
    session = sessions.get(token)
    if not session:
        return None
    if datetime.now() - session.get('created', datetime.now()) > SESSION_LIFETIME:
        del sessions[token]
        return None
    return session

def require_auth(self):
    session = get_session(self)
    if not session:
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'success': False, 'message': 'Not logged in'}).encode())
        return None
    return session

def require_admin(self):
    session = require_auth(self)
    if not session:
        return None
    if session['role'] != 'admin':
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'success': False, 'message': 'Admin only'}).encode())
        return None
    return session

def cleanup_sessions():
    now = datetime.now()
    expired = [t for t, s in sessions.items() if now - s.get('created', now) > SESSION_LIFETIME]
    for t in expired:
        del sessions[t]

def safe_join(base_dir, filename):
    filename = os.path.basename(filename)
    full_path = os.path.normpath(os.path.join(base_dir, filename))
    base_abs = os.path.abspath(base_dir)
    full_abs = os.path.abspath(full_path)
    if not full_abs.startswith(base_abs + os.sep) and full_abs != base_abs:
        return None
    return full_path

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt, h

def verify_password(password, salt, stored_hash):
    _, h = hash_password(password, salt)
    return secrets.compare_digest(h, stored_hash)


# ---------- Row -> JSON shape helpers ----------
# These translate DB column names back to the JSON field names the frontend expects,
# so no HTML/JS files need to change.

def video_row_to_json(v):
    out = {
        'title': v['title'],
        'filename': v['filename'],
        'thumb': v['thumb'],
        'category': v['category'],
        'type': v['type'],
        'views': v.get('views', 0) or 0,
    }
    if v['type'] == 'series':
        out['episodes'] = v['episodes'] if v['episodes'] else []
    else:
        out['url'] = v['url']
    return out

def message_row_to_json(m):
    return {
        'id': m['id'],
        'from': m['sender'],
        'to': m['recipient'],
        'content': m['content'],
        'type': m['type'],
        'time': m['time'],
        'read': m['read'],
        'edited': m['edited'],
        'reactions': m['reactions'] if m['reactions'] else {},
    }

def comment_row_to_json(c):
    return {
        'id': str(c['id']),
        'videoUrl': c['video_url'],
        'username': c['username'],
        'avatar': c['avatar'],
        'text': c['text'],
        'time': c['time'],
    }

def progress_row_to_json(p):
    return {
        'username': p['username'],
        'videoUrl': p['video_url'],
        'current': p['current_time_sec'],
        'duration': p['duration'],
        'watched': p['watched'],
    }


class Handler(http.server.BaseHTTPRequestHandler):

    # ---------------- GET ----------------

    def do_GET(self):
        try:
            self._do_GET_inner()
        except Exception as e:
            print(f'GET error: {e}')
            try:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': 'Bad request'}).encode())
            except Exception:
                pass

    def _do_GET_inner(self):
        path = self.path.split('?')[0]

        if path == '/' or path == '/index.html':
            self.serve_file(os.path.join(STATIC_DIR, 'index.html'), 'text/html')
        elif path == '/login.html':
            self.serve_file(os.path.join(STATIC_DIR, 'login.html'), 'text/html')
        elif path == '/admin.html':
            self.serve_file(os.path.join(STATIC_DIR, 'admin.html'), 'text/html')
        elif path == '/style.css':
            self.serve_file(os.path.join(STATIC_DIR, 'style.css'), 'text/css')
        elif path == '/chat.html':
            self.serve_file(os.path.join(STATIC_DIR, 'chat.html'), 'text/html')
        elif path == '/users.html':
            self.serve_file(os.path.join(STATIC_DIR, 'users.html'), 'text/html')
        elif path == '/top.html':
            self.serve_file(os.path.join(STATIC_DIR, 'top.html'), 'text/html')
        elif path == '/settings.html':
            self.serve_file(os.path.join(STATIC_DIR, 'settings.html'), 'text/html')
        elif path == '/profile.html':
            self.serve_file(os.path.join(STATIC_DIR, 'profile.html'), 'text/html')
        elif path == '/reorder.html':
            self.serve_file(os.path.join(STATIC_DIR, 'reorder.html'), 'text/html')
        elif path == '/signup.html':
            self.serve_file(os.path.join(STATIC_DIR, 'signup.html'), 'text/html')

        elif path == '/api/videos':
            with db_cursor() as (conn, cur):
                cur.execute('SELECT * FROM videos ORDER BY position ASC, id ASC')
                rows = cur.fetchall()
            self.send_json([video_row_to_json(v) for v in rows])

        elif path.startswith('/videos/'):
            filename = path[8:]
            filepath = safe_join(VIDEO_DIR, filename)
            if filepath and os.path.exists(filepath):
                self.serve_file(filepath, 'video/mp4')
            else:
                self.send_error(404)

        elif path == '/api/users':
            with db_cursor() as (conn, cur):
                cur.execute('SELECT username, role, avatar, bio FROM users ORDER BY username')
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path.startswith('/api/messages'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            user1 = qs.get('user1', [''])[0]
            user2 = qs.get('user2', [''])[0]
            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT * FROM messages
                       WHERE (sender = %s AND recipient = %s) OR (sender = %s AND recipient = %s)
                       ORDER BY id''',
                    (user1, user2, user2, user1)
                )
                rows = cur.fetchall()
            self.send_json([message_row_to_json(m) for m in rows])

        elif path == '/api/ratings':
            with db_cursor() as (conn, cur):
                cur.execute('SELECT username, video_url AS "videoUrl", rating FROM ratings ORDER BY id')
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path == '/api/online':
            now = datetime.now()
            active = {u: t.isoformat() for u, t in online_users.items()
                      if now - t < timedelta(minutes=1)}
            self.send_json(active)

        elif path == '/api/progress':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            username = qs.get('username', [''])[0]
            with db_cursor() as (conn, cur):
                cur.execute('SELECT * FROM progress WHERE username = %s ORDER BY id', (username,))
                rows = cur.fetchall()
            self.send_json([progress_row_to_json(p) for p in rows])

        elif path == '/api/favorites':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            username = qs.get('username', [''])[0]
            with db_cursor() as (conn, cur):
                cur.execute('SELECT username, video_url AS "videoUrl" FROM favorites WHERE username = %s ORDER BY id', (username,))
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path == '/api/likes':
            with db_cursor() as (conn, cur):
                cur.execute('SELECT username, video_url AS "videoUrl", action FROM likes ORDER BY id')
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path.startswith('/api/comments'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            videoUrl = qs.get('videoUrl', [''])[0]
            with db_cursor() as (conn, cur):
                cur.execute('SELECT * FROM comments WHERE video_url = %s ORDER BY id', (videoUrl,))
                rows = cur.fetchall()
            self.send_json([comment_row_to_json(c) for c in rows])

        else:
            self.send_error(404)

    # ---------------- POST ----------------

    def do_POST(self):
        try:
            self._do_POST_inner()
        except Exception as e:
            print(f'POST error: {e}')
            try:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': 'Bad request'}).encode())
            except Exception:
                pass

    def _do_POST_inner(self):
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length', 0))

        if path == '/api/login':
            body = json.loads(self.rfile.read(length))
            username = body.get('username', '')
            password = body.get('password', '')

            with db_cursor() as (conn, cur):
                cur.execute('SELECT * FROM users WHERE username = %s', (username,))
                matched = cur.fetchone()

                if not matched:
                    self.send_json({'success': False, 'message': 'Wrong username or password'})
                    return

                if not matched.get('salt'):
                    # legacy plaintext password, migrate on first login
                    if matched['password'] != password:
                        self.send_json({'success': False, 'message': 'Wrong username or password'})
                        return
                    salt, hashed = hash_password(password)
                    cur.execute('UPDATE users SET password = %s, salt = %s WHERE username = %s',
                                (hashed, salt, username))
                else:
                    if not verify_password(password, matched['salt'], matched['password']):
                        self.send_json({'success': False, 'message': 'Wrong username or password'})
                        return

            token = create_session(matched['username'], matched['role'])
            self.send_json({'success': True, 'role': matched['role'], 'username': matched['username'], 'token': token})

        elif path == '/api/addvideo':
            session = require_admin(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            title = body.get('title', 'Untitled')
            thumb = body.get('thumb', '')
            category = body.get('category', '')
            vtype = body.get('type', 'video')

            if vtype == 'series':
                episodes = body.get('episodes', [])
                with db_cursor() as (conn, cur):
                    cur.execute(
                        '''INSERT INTO videos (title, filename, thumb, category, type, episodes)
                           VALUES (%s, %s, %s, %s, 'series', %s)''',
                        (title, title, thumb, category, json.dumps(episodes))
                    )
                self.send_json({'success': True})
            else:
                url = body.get('url', '')
                if url:
                    filename = url.split('/')[-1]
                    with db_cursor() as (conn, cur):
                        cur.execute(
                            '''INSERT INTO videos (title, filename, url, thumb, category, type)
                               VALUES (%s, %s, %s, %s, %s, 'video')''',
                            (title, filename, url, thumb, category)
                        )
                    self.send_json({'success': True})
                else:
                    self.send_json({'success': False, 'message': 'No URL provided'})

        elif path == '/api/reordervideos':
            session = require_admin(self)
            if not session: return
            body  = json.loads(self.rfile.read(length))
            order = body.get('order', [])  # list of filenames in new order

            with db_cursor() as (conn, cur):
                for idx, filename in enumerate(order):
                    cur.execute('UPDATE videos SET position = %s WHERE filename = %s', (idx, filename))
            self.send_json({'success': True})

        elif path == '/api/delete':
            session = require_admin(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            filename = body.get('filename', '')

            with db_cursor() as (conn, cur):
                cur.execute('DELETE FROM videos WHERE filename = %s', (filename,))

            filepath = safe_join(VIDEO_DIR, filename)
            if filepath and os.path.exists(filepath) and os.path.isfile(filepath):
                os.remove(filepath)
            self.send_json({'success': True})

        elif path == '/api/sendmessage':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''INSERT INTO messages (sender, recipient, content, type, time, read)
                       VALUES (%s, %s, %s, %s, %s, FALSE)''',
                    (session['username'], body.get('to'), body.get('content'),
                     body.get('type', 'text'), datetime.now().strftime('%H:%M'))
                )
            self.send_json({'success': True})

        elif path == '/api/deletemessage':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            index = body.get('index')
            user1 = session['username']
            user2 = body.get('user2')

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT id FROM messages
                       WHERE (sender = %s AND recipient = %s) OR (sender = %s AND recipient = %s)
                       ORDER BY id''',
                    (user1, user2, user2, user1)
                )
                conv = cur.fetchall()
                if index is not None and 0 <= index < len(conv):
                    cur.execute('DELETE FROM messages WHERE id = %s', (conv[index]['id'],))
            self.send_json({'success': True})

        elif path == '/api/editmessage':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            index = body.get('index')
            content = body.get('content')
            user1 = session['username']
            user2 = body.get('user2')

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT id FROM messages
                       WHERE (sender = %s AND recipient = %s) OR (sender = %s AND recipient = %s)
                       ORDER BY id''',
                    (user1, user2, user2, user1)
                )
                conv = cur.fetchall()
                if index is not None and 0 <= index < len(conv):
                    cur.execute('UPDATE messages SET content = %s, edited = TRUE WHERE id = %s',
                                (content, conv[index]['id']))
            self.send_json({'success': True})

        elif path == '/api/markread':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))

            with db_cursor() as (conn, cur):
                cur.execute(
                    'UPDATE messages SET read = TRUE WHERE sender = %s AND recipient = %s',
                    (body.get('from'), session['username'])
                )
            self.send_json({'success': True})

        elif path == '/api/updateprofile':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            avatar = body.get('avatar')
            bio = body.get('bio')

            with db_cursor() as (conn, cur):
                cur.execute('UPDATE users SET avatar = %s, bio = %s WHERE username = %s',
                            (avatar, bio, session['username']))
            self.send_json({'success': True})

        elif path == '/api/unratevideo':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')

            with db_cursor() as (conn, cur):
                cur.execute('DELETE FROM ratings WHERE username = %s AND video_url = %s',
                            (session['username'], videoUrl))
            self.send_json({'success': True})

        elif path == '/api/ratevideo':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')
            rating = body.get('rating')

            with db_cursor() as (conn, cur):
                cur.execute('SELECT id FROM ratings WHERE username = %s AND video_url = %s',
                            (session['username'], videoUrl))
                existing = cur.fetchone()
                if existing:
                    cur.execute('UPDATE ratings SET rating = %s WHERE id = %s', (rating, existing['id']))
                else:
                    cur.execute('INSERT INTO ratings (username, video_url, rating) VALUES (%s, %s, %s)',
                                (session['username'], videoUrl, rating))
            self.send_json({'success': True})

        elif path == '/api/ping':
            session = require_auth(self)
            if not session: return
            online_users[session['username']] = datetime.now()
            self.send_json({'success': True})

        elif path == '/api/logout':
            auth = self.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
                if token in sessions:
                    del sessions[token]
            self.send_json({'success': True})

        elif path == '/api/addreaction':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            user1 = body.get('user1')
            user2 = body.get('user2')
            index = body.get('index')
            emoji = body.get('emoji')
            username = session['username']

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT id, reactions FROM messages
                       WHERE (sender = %s AND recipient = %s) OR (sender = %s AND recipient = %s)
                       ORDER BY id''',
                    (user1, user2, user2, user1)
                )
                conv = cur.fetchall()
                if index is not None and 0 <= index < len(conv):
                    msg_id = conv[index]['id']
                    reactions = conv[index]['reactions'] if conv[index]['reactions'] else {}
                    if emoji not in reactions:
                        reactions[emoji] = []
                    if username in reactions[emoji]:
                        reactions[emoji].remove(username)
                        if not reactions[emoji]:
                            del reactions[emoji]
                    else:
                        reactions[emoji].append(username)
                    cur.execute('UPDATE messages SET reactions = %s WHERE id = %s',
                                (json.dumps(reactions), msg_id))
            self.send_json({'success': True})

        elif path == '/api/saveprogress':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')
            current = body.get('current', 0)
            duration = body.get('duration', 0)
            watched = body.get('watched', False)

            with db_cursor() as (conn, cur):
                cur.execute('SELECT id FROM progress WHERE username = %s AND video_url = %s',
                            (session['username'], videoUrl))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        'UPDATE progress SET current_time_sec = %s, duration = %s, watched = %s WHERE id = %s',
                        (current, duration, watched, existing['id'])
                    )
                else:
                    cur.execute(
                        '''INSERT INTO progress (username, video_url, current_time_sec, duration, watched)
                           VALUES (%s, %s, %s, %s, %s)''',
                        (session['username'], videoUrl, current, duration, watched)
                    )
            self.send_json({'success': True})

        elif path == '/api/togglefavorite':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')

            with db_cursor() as (conn, cur):
                cur.execute('SELECT id FROM favorites WHERE username = %s AND video_url = %s',
                            (session['username'], videoUrl))
                existing = cur.fetchone()
                if existing:
                    cur.execute('DELETE FROM favorites WHERE id = %s', (existing['id'],))
                    favorited = False
                else:
                    cur.execute('INSERT INTO favorites (username, video_url) VALUES (%s, %s)',
                                (session['username'], videoUrl))
                    favorited = True
            self.send_json({'success': True, 'favorited': favorited})

        elif path == '/api/togglelike':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')
            action = body.get('action')

            with db_cursor() as (conn, cur):
                cur.execute('SELECT id, action FROM likes WHERE username = %s AND video_url = %s',
                            (session['username'], videoUrl))
                existing = cur.fetchone()
                if existing:
                    if existing['action'] == action:
                        cur.execute('DELETE FROM likes WHERE id = %s', (existing['id'],))
                    else:
                        cur.execute('UPDATE likes SET action = %s WHERE id = %s', (action, existing['id']))
                else:
                    cur.execute('INSERT INTO likes (username, video_url, action) VALUES (%s, %s, %s)',
                                (session['username'], videoUrl, action))
            self.send_json({'success': True})

        elif path == '/api/addcomment':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            new_id = int(datetime.now().timestamp() * 1000)

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''INSERT INTO comments (id, video_url, username, avatar, text, time)
                       VALUES (%s, %s, %s, %s, %s, %s)''',
                    (new_id, body.get('videoUrl'), session['username'], body.get('avatar', '😀'),
                     body.get('text'), datetime.now().strftime('%b %d, %H:%M'))
                )
            self.send_json({'success': True})

        elif path == '/api/deletecomment':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            comment_id = body.get('id')

            with db_cursor() as (conn, cur):
                cur.execute('SELECT username FROM comments WHERE id = %s', (comment_id,))
                target = cur.fetchone()
                if target and target['username'] != session['username']:
                    self.send_json({'success': False, 'message': 'Not your comment'})
                    return
                cur.execute('DELETE FROM comments WHERE id = %s', (comment_id,))
            self.send_json({'success': True})

        elif path == '/api/signup':
            body = json.loads(self.rfile.read(length))
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()

            if not username or not password:
                self.send_json({'success': False, 'message': 'Please fill in all fields.'})
                return

            with db_cursor() as (conn, cur):
                cur.execute('SELECT id FROM users WHERE LOWER(username) = LOWER(%s)', (username,))
                existing = cur.fetchone()
                if existing:
                    self.send_json({'success': False, 'message': 'Username already taken.'})
                    return

                salt, hashed = hash_password(password)
                cur.execute(
                    '''INSERT INTO users (username, password, salt, role, avatar, bio)
                       VALUES (%s, %s, %s, 'viewer', '😀', '')''',
                    (username, hashed, salt)
                )
            self.send_json({'success': True})

        elif path == '/api/addview':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')

            with db_cursor() as (conn, cur):
                cur.execute(
                    '''UPDATE videos SET views = COALESCE(views, 0) + 1
                       WHERE (type = 'series' AND filename = %s) OR (type != 'series' AND url = %s)''',
                    (videoUrl, videoUrl)
                )
            self.send_json({'success': True})

        else:
            self.send_error(404)

    # ---------------- File serving ----------------

    def serve_file(self, filepath, content_type):
        if not os.path.exists(filepath):
            self.send_error(404)
            return

        file_size = os.path.getsize(filepath)
        range_header = self.headers.get('Range')

        if range_header:
            range_val = range_header.strip().replace('bytes=', '')
            start_str, end_str = range_val.split('-')
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
            length = end - start + 1

            self.send_response(206)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', length)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', file_size)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()

            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        ip = self.client_address[0]
        path = self.path
        time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with open('data/access_log.txt', 'a') as f:
                f.write(f'{time} | {ip} | {path}\n')
        except Exception:
            pass


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ensure_schema()
    port = int(os.environ.get('PORT', 8080))
    print(f'Server running on port {port}')
    http.server.HTTPServer(('0.0.0.0', port), Handler).serve_forever()
