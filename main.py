# ============================================================
# VuliStudy backend — v2.0 (simplified)
# Focus-timer study buddy. Username + password accounts, a world
# leaderboard, study-time sync and an admin panel. Everything that
# didn't directly help a student focus (shop/coins, premium, the
# AI coach, friends & group chats) has been removed.
# ============================================================
import os
import sqlite3
import time
import hashlib
import binascii
from flask import Flask, render_template, request, jsonify, send_from_directory
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')


def get_db():
    conn = sqlite3.connect('leaderboard.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL DEFAULT '',
        total_minutes INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        reborns INTEGER DEFAULT 0,
        active_background TEXT DEFAULT 'default',
        character_width INTEGER DEFAULT 140,
        happiness INTEGER DEFAULT 100,
        last_active INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS sync_ratelimit (
        username TEXT PRIMARY KEY,
        last_sync INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_study (
        username TEXT NOT NULL,
        week_start INTEGER NOT NULL,
        minutes INTEGER DEFAULT 0,
        PRIMARY KEY (username, week_start)
    )''')
    # Forward-compatible: add the password column if upgrading an old DB.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


init_db()

SYNC_COOLDOWN = 5
MAX_MINUTES = 50000
MAX_STREAK = 5000
MAX_REBORNS = 500
VALID_BACKGROUNDS = ['default', 'ocean', 'sunset', 'lavender', 'mint', 'rose', 'midnight', 'forest']
BLOCKED_NAMES = ['admin', 'system', 'null', 'undefined', 'test', 'mod', 'owner']


# === PASSWORD HASHING (stdlib pbkdf2) ===
def hash_password(password):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return binascii.hexlify(salt).decode() + '$' + binascii.hexlify(dk).decode()


def verify_password(password, stored):
    try:
        salt_hex, hash_hex = stored.split('$')
        salt = binascii.unhexlify(salt_hex)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return binascii.hexlify(dk).decode() == hash_hex
    except Exception:
        return False


def week_start_ts():
    now = datetime.now(timezone.utc)
    ws = now - timedelta(days=now.weekday())
    ws = ws.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(ws.timestamp())


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/')
def home():
    return render_template('index.html')


# === ACCOUNTS ===
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or len(username) < 2 or len(username) > 20:
        return jsonify({'success': False, 'error': 'Username must be 2–20 characters'})
    if username.lower() in BLOCKED_NAMES:
        return jsonify({'success': False, 'error': 'Username not allowed'})
    if len(password) < 4 or len(password) > 64:
        return jsonify({'success': False, 'error': 'Password must be 4–64 characters'})
    conn = get_db()
    try:
        existing = conn.execute('SELECT username FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            return jsonify({'success': False, 'error': 'Username already taken'})
        now_ts = int(time.time())
        conn.execute('''INSERT INTO users (username, password_hash, last_active, is_active, created_at)
                        VALUES (?, ?, ?, 1, ?)''',
                     (username, hash_password(password), now_ts, now_ts))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify({'success': False, 'error': 'Enter your username and password'})
    conn = get_db()
    try:
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'No account with that username'})
        if not verify_password(password, user['password_hash']):
            return jsonify({'success': False, 'error': 'Incorrect password'})
        conn.execute('UPDATE users SET last_active=?, is_active=1 WHERE username=?',
                     (int(time.time()), username))
        conn.commit()
        return jsonify({'success': True, 'user': {
            'total_minutes': user['total_minutes'],
            'streak': user['streak'],
            'reborns': user['reborns'],
            'happiness': user['happiness'],
            'active_background': user['active_background'],
            'character_width': user['character_width'],
        }})
    finally:
        conn.close()


# === SCORE SYNC ===
@app.route('/sync-score', methods=['POST'])
def sync_score():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    try:
        user = conn.execute('SELECT total_minutes FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        # Rate limit
        rl = conn.execute('SELECT last_sync FROM sync_ratelimit WHERE username = ?', (username,)).fetchone()
        if rl and (int(time.time()) - rl['last_sync']) < SYNC_COOLDOWN:
            return jsonify({'success': False, 'error': 'Rate limited'})
        conn.execute('INSERT OR REPLACE INTO sync_ratelimit (username, last_sync) VALUES (?, ?)',
                     (username, int(time.time())))
        old_minutes = user['total_minutes'] or 0

        total_minutes = min(int(data.get('totalMinutes', 0)), MAX_MINUTES)
        streak = min(int(data.get('streak', 0)), MAX_STREAK)
        reborns = min(int(data.get('reborns', 0)), MAX_REBORNS)

        # Anti-tamper: minutes can never go down, and max gain per sync is 480 min (8 hrs)
        if total_minutes < old_minutes:
            total_minutes = old_minutes
        elif total_minutes - old_minutes > 480:
            total_minutes = old_minutes + 480

        active_background = data.get('activeBackground', 'default')
        character_width = min(max(int(data.get('characterWidth', 140)), 140), 420)
        happiness = min(max(int(data.get('happiness', 100)), 0), 100)
        if active_background not in VALID_BACKGROUNDS:
            active_background = 'default'

        conn.execute('''UPDATE users SET
                        total_minutes=?, streak=?, reborns=?,
                        active_background=?, character_width=?, happiness=?,
                        last_active=?, is_active=1
                        WHERE username=?''',
                     (total_minutes, streak, reborns, active_background,
                      character_width, happiness, int(time.time()), username))

        gained = max(0, total_minutes - old_minutes)
        if gained > 0:
            conn.execute(
                '''INSERT INTO weekly_study (username, week_start, minutes)
                   VALUES (?, ?, ?)
                   ON CONFLICT(username, week_start)
                   DO UPDATE SET minutes = minutes + excluded.minutes''',
                (username, week_start_ts(), gained)
            )
        conn.commit()
        return jsonify({'success': True, 'correctedMinutes': total_minutes})
    finally:
        conn.close()


@app.route('/leaderboard')
def leaderboard():
    conn = get_db()
    three_days_ago = int(time.time()) - (3 * 24 * 60 * 60)
    conn.execute('UPDATE users SET is_active=0 WHERE last_active < ? AND last_active > 0',
                 (three_days_ago,))
    conn.commit()
    period = request.args.get('period', 'all')
    if period == 'weekly':
        users = conn.execute(
            '''SELECT u.username, u.total_minutes, u.streak, u.reborns,
               u.active_background, u.character_width, u.happiness,
               COALESCE(w.minutes, 0) AS weekly_minutes
               FROM users u
               LEFT JOIN weekly_study w ON u.username = w.username AND w.week_start = ?
               WHERE u.is_active=1
               ORDER BY weekly_minutes DESC, u.total_minutes DESC LIMIT 20''',
            (week_start_ts(),)
        ).fetchall()
    else:
        users = conn.execute(
            '''SELECT username, total_minutes, streak, reborns,
               active_background, character_width, happiness
               FROM users WHERE is_active=1
               ORDER BY total_minutes DESC LIMIT 20'''
        ).fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])


@app.route('/check-active', methods=['POST'])
def check_active():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'active': False})
    conn = get_db()
    user = conn.execute('SELECT is_active FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'active': False, 'exists': False})
    return jsonify({'active': bool(user['is_active']), 'exists': True})


@app.route('/rejoin', methods=['POST'])
def rejoin():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    conn.execute('UPDATE users SET is_active=1, last_active=? WHERE username=?',
                 (int(time.time()), username))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/delete-user', methods=['POST'])
def delete_user():
    data = request.get_json()
    username = data.get('username')
    if not username:
        return jsonify({'success': False})
    conn = get_db()
    conn.execute('DELETE FROM users WHERE username = ?', (username,))
    conn.execute('DELETE FROM sync_ratelimit WHERE username = ?', (username,))
    conn.execute('DELETE FROM weekly_study WHERE username = ?', (username,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# === ADMIN ===
@app.route('/check-password', methods=['POST'])
def check_password():
    data = request.get_json()
    if ADMIN_PASSWORD and data.get('password') == ADMIN_PASSWORD:
        return jsonify({'correct': True})
    return jsonify({'correct': False})


@app.route('/admin-get-user', methods=['POST'])
def admin_get_user():
    data = request.get_json()
    if not ADMIN_PASSWORD or data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'No username'})
    conn = get_db()
    try:
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if not user:
            return jsonify({'success': False, 'error': 'User not found on server'})
        u = dict(user)
        u.pop('password_hash', None)  # never expose the hash
        return jsonify({'success': True, 'user': u})
    finally:
        conn.close()


@app.route('/admin-export-db', methods=['POST'])
def admin_export_db():
    data = request.get_json()
    if not ADMIN_PASSWORD or data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    conn = get_db()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        dump = {}
        for t in tables:
            rows = conn.execute(f'SELECT * FROM {t}').fetchall()
            dump[t] = [dict(r) for r in rows]
        return jsonify({'success': True, 'dump': dump, 'exported_at': int(time.time())})
    finally:
        conn.close()


@app.route('/admin-import-db', methods=['POST'])
def admin_import_db():
    data = request.get_json()
    if not ADMIN_PASSWORD or data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'error': 'Unauthorized'})
    dump = data.get('dump')
    if not isinstance(dump, dict):
        return jsonify({'success': False, 'error': 'Invalid dump'})
    conn = get_db()
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
        for table, rows in dump.items():
            if not isinstance(rows, list):
                continue
            conn.execute(f'DELETE FROM {table}')
            for row in rows:
                if not isinstance(row, dict) or not row:
                    continue
                cols = list(row.keys())
                vals = [row[c] for c in cols]
                q = ','.join('?' for _ in cols)
                conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({q})", vals)
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()


if __name__ == '__main__':
    app.run()
