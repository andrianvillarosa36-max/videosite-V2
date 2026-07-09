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

import urllib.request
import uuid
import tempfile
import requests
import threading

PENDING_UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pending_uploads')
os.makedirs(PENDING_UPLOADS_DIR, exist_ok=True)

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
    with db_cursor() as (conn, cur):
        cur.execute(
            'INSERT INTO sessions (token, username, role) VALUES (%s, %s, %s)',
            (token, username, role)
        )
    return token

def get_session(self):
    auth = self.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:]
    with db_cursor() as (conn, cur):
        cur.execute('SELECT * FROM sessions WHERE token = %s', (token,))
        row = cur.fetchone()
        if not row:
            return None
        if datetime.now() - row['created_at'] > SESSION_LIFETIME:
            cur.execute('DELETE FROM sessions WHERE token = %s', (token,))
            return None
        return {'username': row['username'], 'role': row['role'], 'created': row['created_at']}

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
    cutoff = datetime.now() - SESSION_LIFETIME
    with db_cursor() as (conn, cur):
        cur.execute('DELETE FROM sessions WHERE created_at < %s', (cutoff,))

def safe_join(base_dir, filename):
    filename = os.path.basename(filename)
    full_path = os.path.normpath(os.path.join(base_dir, filename))
    base_abs = os.path.abspath(base_dir)
    full_abs = os.path.abspath(full_path)
    if not full_abs.startswith(base_abs + os.sep) and full_abs != base_abs:
        return None
    return full_path

def parse_multipart_stream(rfile, content_type, content_length, max_total_size, max_file_size=200*1024*1024, read_chunk=65536):
    """
    Streams a multipart/form-data body, writing file parts to temp files on disk
    instead of buffering them in memory. Returns a dict of form fields plus a
    '_files' list of {'field', 'filename', 'path', 'size'} dicts, and a
    '_temp_paths' list for cleanup. Caller MUST call cleanup_temp_files() when done.
    """
    if content_length > max_total_size:
        raise ValueError('Upload exceeds the size limit.')

    boundary = content_type.split('boundary=')[1]
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = ('--' + boundary).encode()

    fields = {}
    files = []
    temp_paths = []

    bytes_remaining = content_length
    buffer = b''

    def read_more():
        nonlocal buffer, bytes_remaining
        if bytes_remaining <= 0:
            return False
        chunk = rfile.read(min(read_chunk, bytes_remaining))
        if not chunk:
            bytes_remaining = 0
            return False
        bytes_remaining -= len(chunk)
        buffer += chunk
        return True

    while boundary_bytes not in buffer:
        if not read_more():
            raise ValueError('Malformed upload (no initial boundary found).')
    idx = buffer.index(boundary_bytes) + len(boundary_bytes)
    buffer = buffer[idx:]
    while b'\r\n' not in buffer:
        if not read_more():
            raise ValueError('Malformed upload.')
    buffer = buffer[buffer.index(b'\r\n') + 2:]

    try:
        while True:
            while b'\r\n\r\n' not in buffer:
                if not read_more():
                    raise ValueError('Malformed upload (headers not terminated).')
            header_bytes, _, buffer = buffer.partition(b'\r\n\r\n')
            header_text = header_bytes.decode(errors='ignore')

            filename = None
            field_name = 'file'
            for line in header_text.split('\r\n'):
                if 'Content-Disposition' in line:
                    if 'filename="' in line:
                        filename = line.split('filename="')[1].split('"')[0]
                    if 'name="' in line:
                        field_name = line.split('name="')[1].split('"')[0]

            is_file_part = filename is not None

            if is_file_part:
                tmp = tempfile.NamedTemporaryFile(delete=False, prefix='upload_', suffix='.bin', dir=PENDING_UPLOADS_DIR)
                temp_paths.append(tmp.name)
                size_written = 0
                while True:
                    boundary_idx = buffer.find(b'\r\n' + boundary_bytes)
                    if boundary_idx != -1:
                        tmp.write(buffer[:boundary_idx])
                        size_written += boundary_idx
                        consumed_to = boundary_idx + 2 + len(boundary_bytes)
                        buffer = buffer[consumed_to:]
                        tmp.close()
                        break
                    else:
                        safe_write_len = max(0, len(buffer) - (len(boundary_bytes) + 4))
                        if safe_write_len > 0:
                            tmp.write(buffer[:safe_write_len])
                            size_written += safe_write_len
                            buffer = buffer[safe_write_len:]
                        if size_written > max_file_size:
                            tmp.close()
                            raise ValueError(f'"{filename}" exceeds the per-file size limit.')
                        if not read_more():
                            tmp.close()
                            raise ValueError('Malformed upload (file part not terminated).')
                if size_written > max_file_size:
                    raise ValueError(f'"{filename}" exceeds the per-file size limit.')
                files.append({'field': field_name, 'filename': filename, 'path': tmp.name, 'size': size_written})
            else:
                while True:
                    boundary_idx = buffer.find(b'\r\n' + boundary_bytes)
                    if boundary_idx != -1:
                        value = buffer[:boundary_idx]
                        consumed_to = boundary_idx + 2 + len(boundary_bytes)
                        buffer = buffer[consumed_to:]
                        fields[field_name] = value.decode(errors='ignore')
                        break
                    if not read_more():
                        raise ValueError('Malformed upload (field not terminated).')

            while len(buffer) < 2:
                if not read_more():
                    break
            if buffer[:2] == b'--':
                break
            elif buffer[:2] == b'\r\n':
                buffer = buffer[2:]

    except ValueError:
        for p in temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise

    fields['_files'] = files
    fields['_temp_paths'] = temp_paths
    return fields

def cleanup_temp_files(fields):
    for p in fields.get('_temp_paths', []):
        try:
            os.unlink(p)
        except OSError:
            pass

def upload_to_catbox(filename, file_path):
    """
    Streams a file from disk to Catbox, never loading the whole file into memory.
    `file_path` must be a path to a file on disk (e.g. from parse_multipart_stream).
    """
    with open(file_path, 'rb') as fh:
        files = {'fileToUpload': (filename, fh, 'application/octet-stream')}
        data = {'reqtype': 'fileupload'}
        resp = requests.post('https://catbox.moe/user/api.php', data=data, files=files, timeout=300)
    result = resp.text.strip()
    if not result.startswith('https://files.catbox.moe/'):
        raise Exception(f'Catbox upload failed: {result}')
    return result

def background_relay_upload(submission_id, vtype, files_meta, thumb_path, thumb_filename):
    """
    Runs in a background thread. Relays already-saved local files to Catbox,
    then publishes the video directly to the videos table (no admin approval
    needed) and flips the submission status to 'approved'. Cleans up local
    temp files when done.
    """
    db_url = os.environ.get('DATABASE_URL')
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute('SELECT title, category FROM submissions WHERE id = %s', (submission_id,))
        sub_row = cur.fetchone()
        cur.close()
        conn.close()
        title = sub_row[0] if sub_row else ''
        category = sub_row[1] if sub_row else ''

        thumb_url = ''
        if thumb_path:
            try:
                thumb_url = upload_to_catbox(thumb_filename, thumb_path)
            except Exception as e:
                print(f'[background upload {submission_id}] thumbnail relay failed: {e}')

        if vtype == 'series':
            episodes = []
            for item in files_meta:
                try:
                    url = upload_to_catbox(item['filename'], item['path'])
                    episodes.append({'title': item['ep_title'], 'url': url})
                except Exception as e:
                    print(f'[background upload {submission_id}] episode relay failed: {e}')
                    conn = psycopg2.connect(db_url)
                    cur = conn.cursor()
                    cur.execute("UPDATE submissions SET status = 'failed', admin_note = %s WHERE id = %s",
                                (f'Upload failed: {e}', submission_id))
                    conn.commit()
                    cur.close()
                    conn.close()
                    return
            conn = psycopg2.connect(db_url)
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO videos (title, filename, thumb, category, type, episodes)
                   VALUES (%s, %s, %s, %s, 'series', %s)''',
                (title, title, thumb_url, category, json.dumps(episodes))
            )
            cur.execute(
                "UPDATE submissions SET episodes = %s, thumb = %s, status = 'approved', pending_files = NULL WHERE id = %s",
                (json.dumps(episodes), thumb_url, submission_id)
            )
            conn.commit()
            cur.close()
            conn.close()
        else:
            item = files_meta[0]
            try:
                catbox_url = upload_to_catbox(item['filename'], item['path'])
            except Exception as e:
                print(f'[background upload {submission_id}] video relay failed: {e}')
                conn = psycopg2.connect(db_url)
                cur = conn.cursor()
                cur.execute("UPDATE submissions SET status = 'failed', admin_note = %s WHERE id = %s",
                            (f'Upload failed: {e}', submission_id))
                conn.commit()
                cur.close()
                conn.close()
                return
            filename = catbox_url.split('/')[-1]
            conn = psycopg2.connect(db_url)
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO videos (title, filename, url, thumb, category, type)
                   VALUES (%s, %s, %s, %s, %s, %s)''',
                (title, filename, catbox_url, thumb_url, category, vtype)
            )
            cur.execute(
                "UPDATE submissions SET catbox_url = %s, thumb = %s, status = 'approved', pending_files = NULL WHERE id = %s",
                (catbox_url, thumb_url, submission_id)
            )
            conn.commit()
            cur.close()
            conn.close()
    finally:
        # Clean up local files regardless of success/failure
        all_paths = [f['path'] for f in files_meta]
        if thumb_path:
            all_paths.append(thumb_path)
        for p in all_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

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
        elif path == '/adult.html':
            self.serve_file(os.path.join(STATIC_DIR, 'adult.html'), 'text/html')
        elif path == '/adult-top.html':
            self.serve_file(os.path.join(STATIC_DIR, 'adult-top.html'), 'text/html')
        elif path == '/signup.html':
            self.serve_file(os.path.join(STATIC_DIR, 'signup.html'), 'text/html')
        elif path == '/submit.html':
            self.serve_file(os.path.join(STATIC_DIR, 'submit.html'), 'text/html')
        elif path == '/manifest.json':
            self.serve_file(os.path.join(STATIC_DIR, 'manifest.json'), 'application/json')
        elif path == '/icon-512.png':
            self.serve_file(os.path.join(STATIC_DIR, 'icon-512.png'), 'image/png')
        elif path == '/sw.js':
            self.serve_file(os.path.join(STATIC_DIR, 'sw.js'), 'application/javascript')

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

        elif path.startswith('/api/uploadstatus'):
            session = require_auth(self)
            if not session: return
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sub_id = qs.get('id', [''])[0]
            with db_cursor() as (conn, cur):
                cur.execute('SELECT id, status, admin_note FROM submissions WHERE id = %s', (sub_id,))
                row = cur.fetchone()
            if not row:
                self.send_json({'success': False, 'message': 'Not found'})
                return
            self.send_json({'success': True, 'status': row['status'], 'admin_note': row['admin_note']})

        elif path == '/api/mysubmissions':
            session = require_auth(self)
            if not session: return
            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT id, kind, title, description, category, type, thumb, status, admin_note, created_at
                       FROM submissions WHERE username = %s ORDER BY created_at DESC''',
                    (session['username'],)
                )
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path.startswith('/api/useruploads'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            target_user = qs.get('username', [''])[0]
            if not target_user:
                self.send_json([])
                return
            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT id, kind, title, description, category, type, thumb, created_at
                       FROM submissions WHERE username = %s AND status = %s ORDER BY created_at DESC''',
                    (target_user, 'approved')
                )
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

        elif path == '/api/submissions':
            session = require_admin(self)
            if not session: return
            with db_cursor() as (conn, cur):
                cur.execute(
                    '''SELECT * FROM submissions ORDER BY
                       CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC'''
                )
                rows = cur.fetchall()
            self.send_json([dict(r) for r in rows])

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
                with db_cursor() as (conn, cur):
                    cur.execute('DELETE FROM sessions WHERE token = %s', (token,))
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

        elif path == '/api/uploadchunk':
            session = require_auth(self)
            if not session: return

            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            upload_id = qs.get('upload_id', [''])[0]
            chunk_index = qs.get('chunk_index', ['0'])[0]

            if not upload_id or not all(c.isalnum() or c == '-' for c in upload_id):
                self.send_json({'success': False, 'message': 'Invalid upload_id.'})
                return

            chunk_dir = os.path.join(PENDING_UPLOADS_DIR, 'chunks_' + upload_id)
            os.makedirs(chunk_dir, exist_ok=True)
            chunk_path = os.path.join(chunk_dir, f'chunk_{int(chunk_index):06d}')

            CHUNK_SIZE_LIMIT = 8 * 1024 * 1024  # 8MB per chunk, generous over our 5MB client target
            if length > CHUNK_SIZE_LIMIT:
                self.send_json({'success': False, 'message': 'Chunk too large.'})
                return

            with open(chunk_path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    data = self.rfile.read(min(65536, remaining))
                    if not data:
                        break
                    f.write(data)
                    remaining -= len(data)

            self.send_json({'success': True})

        elif path == '/api/submitupload':
            session = require_auth(self)
            if not session: return

            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_json({'success': False, 'message': 'Invalid upload format.'})
                return

            MAX_UPLOAD_SIZE = 500 * 1024 * 1024
            PER_FILE_LIMIT = 300 * 1024 * 1024

            try:
                fields = parse_multipart_stream(self.rfile, content_type, length, MAX_UPLOAD_SIZE, max_file_size=PER_FILE_LIMIT)
            except ValueError:
                self.send_json({'success': False, 'message': 'Upload exceeds the size limit.'})
                return

            files = fields.get('_files', [])
            if not files:
                self.send_json({'success': False, 'message': 'No file provided.'})
                cleanup_temp_files(fields)
                return

            title = fields.get('title', '').strip()
            description = fields.get('description', '').strip()
            category = fields.get('category', '')
            vtype = fields.get('type', 'video')

            if not title:
                self.send_json({'success': False, 'message': 'Title is required.'})
                cleanup_temp_files(fields)
                return

            thumb_uploads = [f for f in files if f['field'] == 'thumb_file']
            thumb_path = thumb_uploads[0]['path'] if thumb_uploads else None
            thumb_filename = thumb_uploads[0]['filename'] if thumb_uploads else None

            video_files = [f for f in files if f['field'] != 'thumb_file']
            if not video_files:
                self.send_json({'success': False, 'message': 'No video file provided.'})
                cleanup_temp_files(fields)
                return

            files_meta = []
            for i, f in enumerate(video_files):
                ep_title = fields.get(f"ep_title_{f['field']}", f"Episode {i + 1}")
                files_meta.append({'filename': f['filename'], 'path': f['path'], 'ep_title': ep_title})

            with db_cursor() as (conn, cur):
                pending_json = json.dumps([{'path': fm['path'], 'filename': fm['filename']} for fm in files_meta])
                cur.execute(
                    '''INSERT INTO submissions (username, kind, title, description, category, type, status, pending_files)
                       VALUES (%s, 'upload', %s, %s, %s, %s, 'uploading', %s) RETURNING id''',
                    (session['username'], title, description, category, vtype, pending_json)
                )
                submission_id = cur.fetchone()['id']

            self.send_json({'success': True, 'submission_id': submission_id, 'status': 'uploading'})

            thread = threading.Thread(
                target=background_relay_upload,
                args=(submission_id, vtype, files_meta, thumb_path, thumb_filename),
                daemon=True
            )
            thread.start()

        elif path == '/api/finalizeupload':
            session = require_auth(self)
            if not session: return

            body = json.loads(self.rfile.read(length))
            upload_id = body.get('upload_id', '')
            total_chunks = body.get('total_chunks', 0)
            filename = body.get('filename', 'video.mp4')
            field = body.get('field', 'video_file')
            title = body.get('title', '').strip()
            description = body.get('description', '').strip()
            category = body.get('category', '')
            vtype = body.get('type', 'video')
            ep_title = body.get('ep_title', '')
            thumb_upload_id = body.get('thumb_upload_id', '')
            thumb_filename = body.get('thumb_filename', '')
            is_last_file = body.get('is_last_file', True)
            existing_submission_id = body.get('submission_id')
            existing_files_meta = body.get('files_meta', [])

            if not upload_id or not all(c.isalnum() or c == '-' for c in upload_id):
                self.send_json({'success': False, 'message': 'Invalid upload_id.'})
                return

            chunk_dir = os.path.join(PENDING_UPLOADS_DIR, 'chunks_' + upload_id)
            final_path = os.path.join(PENDING_UPLOADS_DIR, f'reassembled_{upload_id}.bin')

            try:
                with open(final_path, 'wb') as out:
                    for i in range(total_chunks):
                        chunk_path = os.path.join(chunk_dir, f'chunk_{i:06d}')
                        if not os.path.exists(chunk_path):
                            raise Exception(f'Chunk {i} of {total_chunks} is missing (upload_id={upload_id}). The upload may have been interrupted.')
                        with open(chunk_path, 'rb') as cf:
                            while True:
                                buf = cf.read(65536)
                                if not buf:
                                    break
                                out.write(buf)
                for i in range(total_chunks):
                    try:
                        os.unlink(os.path.join(chunk_dir, f'chunk_{i:06d}'))
                    except OSError:
                        pass
                try:
                    os.rmdir(chunk_dir)
                except OSError:
                    pass
            except Exception as e:
                self.send_json({'success': False, 'message': str(e)})
                return

            thumb_path = None
            if thumb_upload_id:
                thumb_path = os.path.join(PENDING_UPLOADS_DIR, f'reassembled_{thumb_upload_id}.bin')

            new_file_meta = {'filename': filename, 'path': final_path, 'ep_title': ep_title or filename}
            files_meta = existing_files_meta + [new_file_meta]

            if not is_last_file:
                # More files coming (multi-episode batch) - just acknowledge, client will call finalize again
                self.send_json({'success': True, 'files_meta': files_meta})
                return

            if not title:
                self.send_json({'success': False, 'message': 'Title is required.'})
                return

            with db_cursor() as (conn, cur):
                pending_json = json.dumps([{'path': fm['path'], 'filename': fm['filename']} for fm in files_meta])
                cur.execute(
                    '''INSERT INTO submissions (username, kind, title, description, category, type, status, pending_files)
                       VALUES (%s, 'upload', %s, %s, %s, %s, 'uploading', %s) RETURNING id''',
                    (session['username'], title, description, category, vtype, pending_json)
                )
                submission_id = cur.fetchone()['id']

            self.send_json({'success': True, 'submission_id': submission_id, 'status': 'uploading', 'done': True})

            thread = threading.Thread(
                target=background_relay_upload,
                args=(submission_id, vtype, files_meta, thumb_path, thumb_filename),
                daemon=True
            )
            thread.start()

        elif path == '/api/reviewsubmission':
            session = require_admin(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            sub_id = body.get('id')
            action = body.get('action')  # 'approve' or 'reject'
            admin_note = body.get('admin_note', '')

            if action not in ('approve', 'reject'):
                self.send_json({'success': False, 'message': 'Invalid action.'})
                return

            with db_cursor() as (conn, cur):
                cur.execute('SELECT * FROM submissions WHERE id = %s', (sub_id,))
                sub = cur.fetchone()
                if not sub:
                    self.send_json({'success': False, 'message': 'Submission not found.'})
                    return

                if action == 'reject':
                    cur.execute(
                        'UPDATE submissions SET status = %s, admin_note = %s WHERE id = %s',
                        ('rejected', admin_note, sub_id)
                    )
                    self.send_json({'success': True})
                    return

                # approve
                if sub['kind'] == 'upload' and sub['type'] == 'series' and sub['episodes']:
                    cur.execute(
                        '''INSERT INTO videos (title, filename, thumb, category, type, episodes)
                           VALUES (%s, %s, %s, %s, 'series', %s)''',
                        (sub['title'], sub['title'], sub.get('thumb', ''), sub['category'], json.dumps(sub['episodes']))
                    )
                    cur.execute(
                        'UPDATE submissions SET status = %s, admin_note = %s WHERE id = %s',
                        ('approved', admin_note, sub_id)
                    )
                    self.send_json({'success': True})
                elif sub['kind'] == 'upload' and sub['catbox_url']:
                    filename = sub['catbox_url'].split('/')[-1]
                    cur.execute(
                        '''INSERT INTO videos (title, filename, url, thumb, category, type)
                           VALUES (%s, %s, %s, %s, %s, %s)''',
                        (sub['title'], filename, sub['catbox_url'], sub.get('thumb', ''), sub['category'], sub['type'])
                    )
                    cur.execute(
                        'UPDATE submissions SET status = %s, admin_note = %s WHERE id = %s',
                        ('approved', admin_note, sub_id)
                    )
                    self.send_json({'success': True})
                else:
                    # suggestion-only: mark approved but admin must add the actual video manually
                    cur.execute(
                        'UPDATE submissions SET status = %s, admin_note = %s WHERE id = %s',
                        ('approved', admin_note, sub_id)
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
        def _json_default(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            raise TypeError(f'Object of type {type(obj)} is not JSON serializable')
        body = json.dumps(data, default=_json_default).encode()	
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
            log_path = 'data/access_log.txt'
            MAX_LOG_SIZE = 2 * 1024 * 1024  # 2MB cap
            if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_SIZE:
                with open(log_path, 'r') as f:
                    lines = f.readlines()
                with open(log_path, 'w') as f:
                    f.writelines(lines[-2000:])  # keep last 2000 lines
            with open(log_path, 'a') as f:
                f.write(f'{time} | {ip} | {path}\n')
        except Exception:
            pass


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ensure_schema()
    cleanup_sessions()
    port = int(os.environ.get('PORT', 8080))
    print(f'Server running on port {port}')
    http.server.HTTPServer(('0.0.0.0', port), Handler).serve_forever()
