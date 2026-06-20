import http.server
import json
import os
import shutil

from datetime import datetime, timedelta
online_users = {}

import hashlib
import secrets

sessions = {}  # token -> { 'username': ..., 'role': ... }

def create_session(username, role):
    token = secrets.token_hex(32)
    sessions[token] = { 'username': username, 'role': role, 'created': datetime.now() }
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
        self.wfile.write(json.dumps({ 'success': False, 'message': 'Not logged in' }).encode())
        return None
    return session

SESSION_LIFETIME = timedelta(hours=12)

def cleanup_sessions():
    now = datetime.now()
    expired = [t for t, s in sessions.items() if now - s.get('created', now) > SESSION_LIFETIME]
    for t in expired:
        del sessions[t]

def require_admin(self):
    session = require_auth(self)
    if not session:
        return None
    if session['role'] != 'admin':
        self.send_response(403)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({ 'success': False, 'message': 'Admin only' }).encode())
        return None
    return session

def safe_join(base_dir, filename):
    # Strip any path components, keep just the filename itself
    filename = os.path.basename(filename)
    full_path = os.path.normpath(os.path.join(base_dir, filename))
    base_abs = os.path.abspath(base_dir)
    full_abs = os.path.abspath(full_path)
    if not full_abs.startswith(base_abs + os.sep) and full_abs != base_abs:
        return None  # attempted path traversal
    return full_path

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return salt, h

def verify_password(password, salt, stored_hash):
    _, h = hash_password(password, salt)
    return secrets.compare_digest(h, stored_hash)

DATA_DIR   = 'data'
VIDEO_DIR  = 'videos'
STATIC_DIR = 'static'

def load_json(filename):
    with open(os.path.join(DATA_DIR, filename)) as f:
        return json.load(f)

def save_json(filename, data):
    with open(os.path.join(DATA_DIR, filename), 'w') as f:
        json.dump(data, f, indent=2)

def parse_multipart(rfile, content_type, content_length):
    boundary = content_type.split('boundary=')[1].encode()
    data = rfile.read(content_length)
    parts = data.split(b'--' + boundary)
    fields = {}
    for part in parts[1:-1]:
        header, _, body = part[2:].partition(b'\r\n\r\n')
        header = header.decode()
        body = body.rstrip(b'\r\n')
        if 'filename="' in header:
            filename = header.split('filename="')[1].split('"')[0]
            fields['file'] = (filename, body)
        elif 'name="' in header:
            name = header.split('name="')[1].split('"')[0]
            fields[name] = body.decode()
    return fields

class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            self._do_GET_inner()
        except Exception as e:
            try:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({ 'success': False, 'message': 'Bad request' }).encode())
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
        elif path == '/api/videos':
            self.send_json(load_json('videos.json'))
        elif path.startswith('/videos/'):
            filename = path[8:]
            filepath = safe_join(VIDEO_DIR, filename)
            if filepath and os.path.exists(filepath):
                self.serve_file(filepath, 'video/mp4')
            else:
                self.send_error(404)

        elif path == '/chat.html':
            self.serve_file(os.path.join(STATIC_DIR, 'chat.html'), 'text/html')
        elif path == '/api/users':
            users = load_json('users.json')
            safe  = [{
                'username': u['username'],
                'role': u['role'],
                'avatar': u.get('avatar', '😀'),
                'bio': u.get('bio', '')
            } for u in users]
            self.send_json(safe)
        elif path.startswith('/api/messages'):
            from urllib.parse import urlparse, parse_qs
            qs    = parse_qs(urlparse(self.path).query)
            user1 = qs.get('user1', [''])[0]
            user2 = qs.get('user2', [''])[0]
            msgs  = load_json('messages.json')
            conv  = [m for m in msgs if
                     (m['from'] == user1 and m['to'] == user2) or
                     (m['from'] == user2 and m['to'] == user1)]
            self.send_json(conv)
        elif path == '/users.html':
            self.serve_file(os.path.join(STATIC_DIR, 'users.html'), 'text/html')
        elif path == '/top.html':
            self.serve_file(os.path.join(STATIC_DIR, 'top.html'), 'text/html')
        elif path == '/settings.html':
            self.serve_file(os.path.join(STATIC_DIR, 'settings.html'), 'text/html')
        elif path == '/profile.html':
            self.serve_file(os.path.join(STATIC_DIR, 'profile.html'), 'text/html')
        elif path == '/api/ratings':
            self.send_json(load_json('ratings.json'))
        elif path == '/api/online':
            now    = datetime.now()
            active = { u: t.isoformat() for u, t in online_users.items()
                      if now - t < timedelta(minutes=1) }
            self.send_json(active)
        elif path == '/api/progress':
            from urllib.parse import urlparse, parse_qs
            qs       = parse_qs(urlparse(self.path).query)
            username = qs.get('username', [''])[0]
            progress = load_json('progress.json')
            mine     = [p for p in progress if p['username'] == username]
            self.send_json(mine)
        elif path == '/api/favorites':
            from urllib.parse import urlparse, parse_qs
            qs       = parse_qs(urlparse(self.path).query)
            username = qs.get('username', [''])[0]
            favs     = load_json('favorites.json')
            mine     = [f for f in favs if f['username'] == username]
            self.send_json(mine)
        elif path == '/api/likes':
            self.send_json(load_json('likes.json'))
        elif path.startswith('/api/comments'):
            from urllib.parse import urlparse, parse_qs
            qs       = parse_qs(urlparse(self.path).query)
            videoUrl = qs.get('videoUrl', [''])[0]
            comments = load_json('comments.json')
            mine     = [c for c in comments if c['videoUrl'] == videoUrl]
            self.send_json(mine)
        elif path == '/signup.html':
            self.serve_file(os.path.join(STATIC_DIR, 'signup.html'), 'text/html')

        else:
            self.send_error(404)

    def do_POST(self):
        try:
            self._do_POST_inner()
        except Exception as e:
            try:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({ 'success': False, 'message': 'Bad request' }).encode())
            except Exception:
                pass

    def _do_POST_inner(self):
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length', 0))

        if path == '/api/login':
            body     = json.loads(self.rfile.read(length))
            username = body.get('username', '')
            password = body.get('password', '')
            users    = load_json('users.json')
            matched  = next((u for u in users if u['username'] == username), None)

            if not matched:
                self.send_json({ 'success': False, 'message': 'Wrong username or password' })
                return

            # Migrate plaintext password to hashed on first successful login
            if 'salt' not in matched:
                if matched.get('password') != password:
                    self.send_json({ 'success': False, 'message': 'Wrong username or password' })
                    return
                salt, hashed = hash_password(password)
                matched['salt']     = salt
                matched['password'] = hashed
                save_json('users.json', users)
            else:
                if not verify_password(password, matched['salt'], matched['password']):
                    self.send_json({ 'success': False, 'message': 'Wrong username or password' })
                    return

            token = create_session(matched['username'], matched['role'])
            self.send_json({ 'success': True, 'role': matched['role'], 'username': matched['username'], 'token': token })

        elif path == '/api/addvideo':
            session = require_admin(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            title    = body.get('title', 'Untitled')
            thumb    = body.get('thumb', '')
            category = body.get('category', '')
            vtype    = body.get('type', 'video')
            videos   = load_json('videos.json')
            if vtype == 'series':
                episodes = body.get('episodes', [])
                videos.append({ 'title': title, 'thumb': thumb, 'category': category, 'type': 'series', 'filename': title, 'episodes': episodes })
                save_json('videos.json', videos)
                self.send_json({ 'success': True })
            else:
                url = body.get('url', '')
                if url:
                    filename = url.split('/')[-1]
                    videos.append({ 'title': title, 'filename': filename, 'url': url, 'thumb': thumb, 'category': category, 'type': 'video' })
                    save_json('videos.json', videos)
                    self.send_json({ 'success': True })
                else:
                    self.send_json({ 'success': False, 'message': 'No URL provided' })

        elif path == '/api/delete':
            session = require_admin(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            filename = body.get('filename', '')
            videos   = load_json('videos.json')
            videos   = [v for v in videos if v['filename'] != filename]
            save_json('videos.json', videos)
            filepath = safe_join(VIDEO_DIR, filename)
            if filepath and os.path.exists(filepath) and os.path.isfile(filepath):
                os.remove(filepath)
            self.send_json({ 'success': True })

        elif path == '/api/sendmessage':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            msgs = load_json('messages.json')
            msgs.append({
                'from':    session['username'],
                'to':      body.get('to'),
                'content': body.get('content'),
                'type':    body.get('type', 'text'),
                'time':    datetime.now().strftime('%H:%M'),
                'read':    False
            })
            save_json('messages.json', msgs)
            self.send_json({ 'success': True })

        elif path == '/api/deletemessage':
            session = require_auth(self)
            if not session: return
            body  = json.loads(self.rfile.read(length))
            index = body.get('index')
            user1 = session['username']
            user2 = body.get('user2')
            msgs  = load_json('messages.json')
            conv  = [m for m in msgs if
                     (m['from'] == user1 and m['to'] == user2) or
                     (m['from'] == user2 and m['to'] == user1)]
            if 0 <= index < len(conv):
                msgs.remove(conv[index])
                save_json('messages.json', msgs)
            self.send_json({ 'success': True })

        elif path == '/api/editmessage':
            session = require_auth(self)
            if not session: return
            body    = json.loads(self.rfile.read(length))
            index   = body.get('index')
            content = body.get('content')
            user1   = session['username']
            user2   = body.get('user2')
            msgs    = load_json('messages.json')
            conv    = [m for m in msgs if
                       (m['from'] == user1 and m['to'] == user2) or
                       (m['from'] == user2 and m['to'] == user1)]
            if 0 <= index < len(conv):
                conv[index]['content'] = content
                conv[index]['edited']  = True
                save_json('messages.json', msgs)
            self.send_json({ 'success': True })

        elif path == '/api/markread':
            session = require_auth(self)
            if not session: return
            body = json.loads(self.rfile.read(length))
            msgs = load_json('messages.json')
            for m in msgs:
                if m['from'] == body.get('from') and m['to'] == session['username']:
                    m['read'] = True
            save_json('messages.json', msgs)
            self.send_json({ 'success': True })

        elif path == '/api/updateprofile':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            avatar   = body.get('avatar')
            bio      = body.get('bio')
            users    = load_json('users.json')
            for u in users:
                if u['username'] == username:
                    u['avatar'] = avatar
                    u['bio']    = bio
            save_json('users.json', users)
            self.send_json({ 'success': True })

        elif path == '/api/unratevideo':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            videoUrl = body.get('videoUrl')
            ratings  = load_json('ratings.json')
            ratings  = [r for r in ratings if not (r['username'] == username and r['videoUrl'] == videoUrl)]
            save_json('ratings.json', ratings)
            self.send_json({ 'success': True })

        elif path == '/api/ratevideo':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            videoUrl = body.get('videoUrl')
            rating   = body.get('rating')
            ratings  = load_json('ratings.json')
            existing = next((r for r in ratings if r['username'] == username and r['videoUrl'] == videoUrl), None)
            if existing:
                existing['rating'] = rating
            else:
                ratings.append({ 'username': username, 'videoUrl': videoUrl, 'rating': rating })
            save_json('ratings.json', ratings)
            self.send_json({ 'success': True })

        elif path == '/api/ping':
            session = require_auth(self)
            if not session: return
            online_users[session['username']] = datetime.now()
            self.send_json({ 'success': True })

        elif path == '/api/logout':
            auth = self.headers.get('Authorization', '')
            if auth.startswith('Bearer '):
                token = auth[7:]
                if token in sessions:
                    del sessions[token]
            self.send_json({ 'success': True })

        elif path == '/api/addreaction':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            user1    = body.get('user1')
            user2    = body.get('user2')
            index    = body.get('index')
            emoji    = body.get('emoji')
            username = session['username']
            msgs    = load_json('messages.json')
            conv    = [m for m in msgs if
                       (m['from'] == user1 and m['to'] == user2) or
                       (m['from'] == user2 and m['to'] == user1)]
            if 0 <= index < len(conv):
                if 'reactions' not in conv[index]:
                    conv[index]['reactions'] = {}
                if emoji not in conv[index]['reactions']:
                    conv[index]['reactions'][emoji] = []
                if username in conv[index]['reactions'][emoji]:
                    conv[index]['reactions'][emoji].remove(username)
                    if not conv[index]['reactions'][emoji]:
                        del conv[index]['reactions'][emoji]
                else:
                    conv[index]['reactions'][emoji].append(username)
                save_json('messages.json', msgs)
            self.send_json({ 'success': True })

        elif path == '/api/saveprogress':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            videoUrl = body.get('videoUrl')
            current  = body.get('current', 0)
            duration = body.get('duration', 0)
            watched  = body.get('watched', False)
            progress = load_json('progress.json')
            existing = next((p for p in progress if p['username'] == username and p['videoUrl'] == videoUrl), None)
            if existing:
                existing['current']  = current
                existing['duration'] = duration
                existing['watched']  = watched
            else:
                progress.append({ 'username': username, 'videoUrl': videoUrl, 'current': current, 'duration': duration, 'watched': watched })
            save_json('progress.json', progress)
            self.send_json({ 'success': True })

        elif path == '/api/togglefavorite':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            videoUrl = body.get('videoUrl')
            favs     = load_json('favorites.json')
            existing = next((f for f in favs if f['username'] == username and f['videoUrl'] == videoUrl), None)
            if existing:
                favs.remove(existing)
                save_json('favorites.json', favs)
                self.send_json({ 'success': True, 'favorited': False })
            else:
                favs.append({ 'username': username, 'videoUrl': videoUrl })
                save_json('favorites.json', favs)
                self.send_json({ 'success': True, 'favorited': True })

        elif path == '/api/togglelike':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            username = session['username']
            videoUrl = body.get('videoUrl')
            action   = body.get('action')
            likes    = load_json('likes.json')
            existing = next((l for l in likes if l['username'] == username and l['videoUrl'] == videoUrl), None)
            if existing:
                if existing['action'] == action:
                    likes.remove(existing)
                else:
                    existing['action'] = action
            else:
                likes.append({ 'username': username, 'videoUrl': videoUrl, 'action': action })
            save_json('likes.json', likes)
            self.send_json({ 'success': True })

        elif path == '/api/addcomment':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            comments = load_json('comments.json')
            comments.append({
                'id':       str(int(datetime.now().timestamp() * 1000)),
                'videoUrl': body.get('videoUrl'),
                'username': session['username'],
                'avatar':   body.get('avatar', '😀'),
                'text':     body.get('text'),
                'time':     datetime.now().strftime('%b %d, %H:%M')
            })
            save_json('comments.json', comments)
            self.send_json({ 'success': True })

        elif path == '/api/deletecomment':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            comments = load_json('comments.json')
            target   = next((c for c in comments if c['id'] == body.get('id')), None)
            if target and target['username'] != session['username']:
                self.send_json({ 'success': False, 'message': 'Not your comment' })
                return
            comments = [c for c in comments if c['id'] != body.get('id')]
            save_json('comments.json', comments)
            self.send_json({ 'success': True })

        elif path == '/api/signup':
            body     = json.loads(self.rfile.read(length))
            username = body.get('username', '').strip()
            password = body.get('password', '').strip()
            users    = load_json('users.json')

            if not username or not password:
                self.send_json({ 'success': False, 'message': 'Please fill in all fields.' })
                return

            existing = next((u for u in users if u['username'].lower() == username.lower()), None)
            if existing:
                self.send_json({ 'success': False, 'message': 'Username already taken.' })
                return

            salt, hashed = hash_password(password)
            users.append({
                'username': username,
                'password': hashed,
                'salt': salt,
                'role': 'viewer',
                'avatar': '😀',
                'bio': ''
            })
            save_json('users.json', users)
            self.send_json({ 'success': True })

        elif path == '/api/addview':
            session  = require_auth(self)
            if not session: return
            body     = json.loads(self.rfile.read(length))
            videoUrl = body.get('videoUrl')
            videos   = load_json('videos.json')
            for v in videos:
                vid = v.get('filename') if v.get('type') == 'series' else v.get('url')
                if vid == videoUrl:
                    v['views'] = v.get('views', 0) + 1
            save_json('videos.json', videos)
            self.send_json({ 'success': True })

        else:
            self.send_error(404)

    def serve_file(self, filepath, content_type):
        if not os.path.exists(filepath):
            self.send_error(404)
            return

        file_size = os.path.getsize(filepath)
        range_header = self.headers.get('Range')

        if range_header:
            # Parse range request
            range_val = range_header.strip().replace('bytes=', '')
            start_str, end_str = range_val.split('-')
            start = int(start_str)
            end   = int(end_str) if end_str else file_size - 1
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
            # Full file
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
        ip   = self.client_address[0]
        path = self.path
        time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open('data/access_log.txt', 'a') as f:
            f.write(f'{time} | {ip} | {path}\n')

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = int(os.environ.get('PORT', 8080))
    print(f'Server running on port {port}')
    http.server.HTTPServer(('0.0.0.0', port), Handler).serve_forever()
