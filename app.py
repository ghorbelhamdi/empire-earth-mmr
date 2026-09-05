import os, json, sqlite3, hashlib, secrets, time, logging, html, sys
from contextlib import contextmanager
from datetime import datetime
from itertools import combinations
from functools import wraps
from flask import Flask, request, redirect, url_for, session, jsonify, g, render_template
from ratings import (rate_match, predict_win, ordinal_to_mmr, OS_DEFAULT_MU,
                     OS_DEFAULT_SIGMA, RATING_SCALE, RATING_OFFSET)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DATABASE_URL = os.environ.get('DATABASE_URL', '')
SQLITE_PATH = os.environ.get('SQLITE_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'empire.db'))
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD', '')
REQUIRE_MATCH_APPROVAL = os.environ.get('REQUIRE_MATCH_APPROVAL', 'true').lower() == 'true'
DEFAULT_MMR = 1000
ADMIN_SESSION_TIMEOUT = 1800  # 30 minutes

use_postgres = DATABASE_URL.startswith('postgres')

# ---------- SECURITY HELPERS ----------
def esc(s):
    """Escape a string for safe HTML insertion."""
    return html.escape(str(s))

def csrf_token():
    """Get or create a CSRF token for the current session."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

def csrf_field():
    """Return a hidden input HTML string with the CSRF token."""
    return f'<input type="hidden" name="csrf_token" value="{csrf_token()}">'

def check_csrf():
    """Validate the CSRF token from the submitted form."""
    token = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
    expected_token = session.get('csrf_token', None)
    return expected_token is not None and secrets.compare_digest(token, expected_token)


def get_db():
    if 'db' not in g:
        if use_postgres:
            import psycopg
            from psycopg.rows import dict_row
            url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
            g.db = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            g.db = sqlite3.connect(SQLITE_PATH, timeout=30)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA journal_mode=WAL')
            g.db.execute('PRAGMA busy_timeout=30000')
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query(sql, args=(), one=False, commit=False):
    db = get_db()
    if use_postgres:
        cur = db.cursor()
        sql = sql.replace('?', '%s').replace('AUTOINCREMENT', '').replace('INTEGER PRIMARY KEY', 'SERIAL PRIMARY KEY')
    else:
        cur = db.cursor()
    cur.execute(sql, args)
    if commit:
        lastrowid = cur.lastrowid if not use_postgres else None
        if not use_postgres and not g.get('rating_transaction_depth', 0):
            db.commit()
        cur.close()
        return lastrowid
    rows = cur.fetchall()
    cur.close()
    if use_postgres:
        return rows[0] if one and rows else rows if not one else None
    else:
        results = [dict(r) for r in rows]
        return results[0] if one and results else results if not one else None


@contextmanager
def rating_transaction():
    """Serialize rating writes across workers and commit each whole operation atomically."""
    depth = g.get('rating_transaction_depth', 0)
    if depth:
        g.rating_transaction_depth = depth + 1
        try:
            yield
        finally:
            g.rating_transaction_depth = depth
        return
    db = get_db()
    db.execute('BEGIN' if use_postgres else 'BEGIN IMMEDIATE')
    g.rating_transaction_depth = 1
    try:
        if use_postgres:
            # Transaction-scoped global ladder lock, also shared by replay and admin mutations.
            db.execute('SELECT pg_advisory_xact_lock(1162169677)')
        yield
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    finally:
        g.rating_transaction_depth = 0


def _migrate_companion_columns():
    columns = {
        'rating_version': "TEXT DEFAULT 'openskill-v1'",
        'military_stats': 'TEXT', 'evidence': 'TEXT', 'rating_details': 'TEXT',
        'source': "TEXT DEFAULT 'web'", 'rated_order': 'INTEGER',
    }
    with rating_transaction():
        if use_postgres:
            for name, definition in columns.items():
                query(f'ALTER TABLE matches ADD COLUMN IF NOT EXISTS {name} {definition}', commit=True)
        else:
            existing = {row['name'] for row in query('PRAGMA table_info(matches)')}
            for name, definition in columns.items():
                if name not in existing:
                    query(f'ALTER TABLE matches ADD COLUMN {name} {definition}', commit=True)
        query('''CREATE TABLE IF NOT EXISTS companion_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used_at TIMESTAMP, revoked_at TIMESTAMP)''', commit=True)
        query('''CREATE TABLE IF NOT EXISTS companion_submissions (
            submission_id TEXT PRIMARY KEY, token_id INTEGER NOT NULL, payload_hash TEXT NOT NULL,
            match_id INTEGER, screenshot_sha256 TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''', commit=True)
        # Existing values stay untouched; IDs describe the original replay ordering.
        query("UPDATE matches SET rated_order=id WHERE status='approved' AND rated_order IS NULL", commit=True)


def _migrate_openskill_columns():
    """Idempotent: ensure mu/sigma columns exist on players table."""
    try:
        db = get_db()
        cur = db.cursor()
        if use_postgres:
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS mu REAL DEFAULT 25.0")
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS sigma REAL DEFAULT 8.333333333333334")
        else:
            cur.execute("PRAGMA table_info(players)")
            cols = {row[1] if not isinstance(row, dict) else row['name'] for row in cur.fetchall()}
            if 'mu' not in cols:
                cur.execute("ALTER TABLE players ADD COLUMN mu REAL DEFAULT 25.0")
            if 'sigma' not in cols:
                cur.execute("ALTER TABLE players ADD COLUMN sigma REAL DEFAULT 8.333333333333334")
            db.commit()
        cur.close()
    except Exception as e:
        logger.warning(f'OpenSkill column migration skipped: {e}')


def init_db():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            db = get_db()
            if use_postgres:
                cur = db.cursor()
                cur.execute('''CREATE TABLE IF NOT EXISTS players (
                    id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, mmr INTEGER DEFAULT 1000,
                    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                    mu REAL DEFAULT 25.0, sigma REAL DEFAULT 8.333333333333334,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                cur.execute('''CREATE TABLE IF NOT EXISTS matches (
                    id SERIAL PRIMARY KEY, team1 TEXT NOT NULL, team2 TEXT NOT NULL,
                    winner TEXT NOT NULL, mmr_changes TEXT, status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                cur.close()
            else:
                cur = db.cursor()
                cur.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, mmr INTEGER DEFAULT 1000,
                    wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                    mu REAL DEFAULT 25.0, sigma REAL DEFAULT 8.333333333333334,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                cur.execute('''CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, team1 TEXT NOT NULL, team2 TEXT NOT NULL,
                    winner TEXT NOT NULL, mmr_changes TEXT, status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                db.commit()
                cur.close()
            _migrate_openskill_columns()
            _migrate_companion_columns()
            logger.info('Database initialized successfully.')
            return
        except Exception as e:
            logger.warning(f'init_db attempt {attempt + 1}/{max_retries} failed: {e}')
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                raise

with app.app_context():
    init_db()

@app.route('/health')
def health_check():
    backend = 'postgres' if use_postgres else 'sqlite'
    try:
        if use_postgres:
            query('SELECT 1')
        else:
            if not os.path.exists(SQLITE_PATH):
                return jsonify({'status': 'error', 'backend': backend, 'message': 'sqlite db not found'}), 500
        players = query('SELECT COUNT(*) as cnt FROM players')
        count = players[0]['cnt'] if players else 0
        return jsonify({'status': 'ok', 'backend': backend, 'player_count': count}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'backend': backend, 'message': 'Internal server error'}), 500

def rename_player_in_matches(old_name, new_name):
    matches = query('SELECT * FROM matches')
    for m in matches:
        changed = False
        t1 = json.loads(m['team1'])
        t2 = json.loads(m['team2'])
        changes = json.loads(m['mmr_changes']) if m['mmr_changes'] else {}
        if old_name in t1:
            t1 = [new_name if n == old_name else n for n in t1]
            changed = True
        if old_name in t2:
            t2 = [new_name if n == old_name else n for n in t2]
            changed = True
        if old_name in changes:
            changes[new_name] = changes.pop(old_name)
            changed = True
        if changed:
            details = json.loads(m['rating_details']) if m.get('rating_details') else {}
            if old_name in details.get('players', {}):
                details['players'][new_name] = details['players'].pop(old_name)
            query('UPDATE matches SET team1=?, team2=?, mmr_changes=?, rating_details=? WHERE id=?',
                  (json.dumps(t1), json.dumps(t2), json.dumps(changes), json.dumps(details), m['id']), commit=True)

def _fmt_delta(n):
    return f'+{n}' if n >= 0 else f'{n}'


def _stats_by_name(players, stats):
    if not stats:
        return None
    by_id = {p['id']: p['name'] for p in players}
    if len(stats) != len(players) or {row['player_id'] for row in stats} != set(by_id):
        raise ValueError('Military stats no longer match the complete roster.')
    return {by_id[row['player_id']]: {'units_killed': row['units_killed'], 'units_lost': row['units_lost']}
            for row in stats}


def calculate_match(w_players, l_players, stats=None, version='military-v2'):
    new_w, new_l, details = rate_match(w_players, l_players,
                                      _stats_by_name(w_players + l_players, stats), version=version)
    changes = {}
    for p, r in zip(w_players + l_players, new_w + new_l):
        old_mmr = p['mmr'] if p.get('mmr') is not None else DEFAULT_MMR
        new_mmr = ordinal_to_mmr(r.mu, r.sigma)
        changes[p['name']] = _fmt_delta(new_mmr - old_mmr)
    return changes, details


def preview_openskill_deltas(w_players, l_players):
    """Compute expected per-player MMR deltas without touching the DB."""
    return calculate_match(w_players, l_players)[0]


def apply_openskill_match(w_players, l_players, update_counts=True, stats=None, version='military-v2'):
    """Rate a match and persist mu/sigma/mmr (and optionally wins/losses). Returns changes dict."""
    new_w, new_l, details = rate_match(w_players, l_players,
                                      _stats_by_name(w_players + l_players, stats), version=version)
    changes = {}
    for p, r in zip(w_players, new_w):
        old_mmr = p['mmr'] if p.get('mmr') is not None else DEFAULT_MMR
        new_mmr = ordinal_to_mmr(r.mu, r.sigma)
        changes[p['name']] = _fmt_delta(new_mmr - old_mmr)
        if update_counts:
            query('UPDATE players SET mu=?, sigma=?, mmr=?, wins=wins+1 WHERE id=?',
                  (r.mu, r.sigma, new_mmr, p['id']), commit=True)
        else:
            query('UPDATE players SET mu=?, sigma=?, mmr=? WHERE id=?',
                  (r.mu, r.sigma, new_mmr, p['id']), commit=True)
    for p, r in zip(l_players, new_l):
        old_mmr = p['mmr'] if p.get('mmr') is not None else DEFAULT_MMR
        new_mmr = ordinal_to_mmr(r.mu, r.sigma)
        changes[p['name']] = _fmt_delta(new_mmr - old_mmr)
        if update_counts:
            query('UPDATE players SET mu=?, sigma=?, mmr=?, losses=losses+1 WHERE id=?',
                  (r.mu, r.sigma, new_mmr, p['id']), commit=True)
        else:
            query('UPDATE players SET mu=?, sigma=?, mmr=? WHERE id=?',
                  (r.mu, r.sigma, new_mmr, p['id']), commit=True)
    return changes, details


def recalc_all_openskill():
    """Replay the recorded formula and approval order, under the same lock as approval."""
    with rating_transaction():
        query('UPDATE players SET mu=?, sigma=?, mmr=?, wins=0, losses=0',
              (OS_DEFAULT_MU, OS_DEFAULT_SIGMA, DEFAULT_MMR), commit=True)
        matches = query("SELECT * FROM matches WHERE status='approved' ORDER BY rated_order ASC, id ASC")
        replayed = 0
        for m in matches:
            w_players, l_players = match_players(m)
            if not w_players or not l_players:
                # Never silently turn an old game into a match with an incomplete team.
                query('UPDATE matches SET mmr_changes=?,rating_details=? WHERE id=?',
                      ('{}', json.dumps({'replay_skipped': 'A player was deleted.'}), m['id']), commit=True)
                continue
            changes, details = apply_openskill_match(w_players, l_players, update_counts=True,
                stats=json.loads(m['military_stats']) if m.get('military_stats') else None,
                version=m.get('rating_version') or 'openskill-v1')
            query('UPDATE matches SET mmr_changes=?,rating_details=? WHERE id=?',
                  (json.dumps(changes), json.dumps(details), m['id']), commit=True)
            replayed += 1
    return replayed


def match_players(match):
    names1, names2 = json.loads(match['team1']), json.loads(match['team2'])
    if not names1 or not names2 or len(set(names1 + names2)) != len(names1 + names2):
        return [], []
    t1 = [query('SELECT * FROM players WHERE name=?', (name,), one=True) for name in names1]
    t2 = [query('SELECT * FROM players WHERE name=?', (name,), one=True) for name in names2]
    if any(p is None for p in t1 + t2):
        return [], []
    return (t1, t2) if match['winner'] == 'team1' else (t2, t1)


def next_rated_order():
    row = query('SELECT MAX(rated_order) AS n FROM matches', one=True)
    return (row['n'] or 0) + 1


def validated_web_rosters(ids1, ids2, winner, players):
    if not ids1 or not ids2:
        raise ValueError('Both teams need at least one player.')
    ids = ids1 + ids2
    if not 2 <= len(ids) <= 10:
        raise ValueError('Select 2 to 10 players.')
    if len(set(ids)) != len(ids):
        raise ValueError('Each player may appear only once in a match.')
    by_id = {str(p['id']): p for p in players}
    if not set(ids).issubset(by_id):
        raise ValueError('Unknown player. Refresh the roster and try again.')
    if winner not in ('team1', 'team2'):
        raise ValueError('Select a winner.')
    return [by_id[pid] for pid in ids1], [by_id[pid] for pid in ids2]

def team_avg_mmr(players_list):
    if not players_list:
        return 0
    return sum(p['mmr'] for p in players_list) / len(players_list)

def balance_teams(player_ids):
    players = []
    for pid in player_ids:
        p = query('SELECT * FROM players WHERE id = ?', (pid,), one=True)
        if p:
            players.append(p)
    n = len(players)
    if n < 2:
        return None, None, None, False
    best_score = float('inf')
    best_t1 = best_t2 = None
    half = n // 2
    sizes = [half] if n % 2 == 0 else [half, half + 1]
    unequal = (n % 2 != 0)
    for sz in sizes:
        for combo in combinations(range(n), sz):
            t1 = [players[i] for i in combo]
            t2 = [players[i] for i in range(n) if i not in combo]
            # Predictions account for team size and each player's uncertainty.
            score = abs(predict_win(t1, t2)[0] - 0.5)
            if score < best_score:
                best_score = score
                best_t1, best_t2 = t1, t2
    # For unequal teams, ensure t1 is the smaller team (for display clarity)
    if unequal and best_t1 and best_t2 and len(best_t1) > len(best_t2):
        best_t1, best_t2 = best_t2, best_t1
    return best_t1, best_t2, best_score, unequal

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not admin_authenticated():
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def admin_authenticated():
    if not session.get('is_admin'):
        return False
    login_time = session.get('admin_login_time')
    if login_time is None or time.time() - login_time > ADMIN_SESSION_TIMEOUT:
        session.clear()
        return False
    return True


def remove_match(match_id):
    # Retain receipts even after deletion, so a retried companion report cannot resurrect a game.
    query('UPDATE companion_submissions SET match_id=NULL WHERE match_id=?', (match_id,), commit=True)
    query('DELETE FROM matches WHERE id=?', (match_id,), commit=True)


def remove_player(player):
    # Match history uses names; preserving referenced players prevents accidentally reattaching
    # old matches to a new person who later registers the same name.
    for match in query('SELECT team1,team2 FROM matches'):
        if player['name'] in json.loads(match['team1']) + json.loads(match['team2']):
            raise ValueError('This player has match history. Delete their matches before deleting the player.')
    query('DELETE FROM players WHERE id=?', (player['id'],), commit=True)


# The stylesheet now lives in static/style.css (linked from page()).

RENAME_JS = '''
function adminRenamePlayer(id, currentName, csrfToken) {
    var newName = prompt("Rename player '" + currentName + "' to:", currentName);
    if (newName && newName.trim() !== "" && newName.trim() !== currentName) {
        var f = document.createElement("form");
        f.method = "POST"; f.action = "/rename_player";
        var i1 = document.createElement("input"); i1.type = "hidden"; i1.name = "player_id"; i1.value = id;
        var i2 = document.createElement("input"); i2.type = "hidden"; i2.name = "new_name"; i2.value = newName.trim();
        var i3 = document.createElement("input"); i3.type = "hidden"; i3.name = "redirect"; i3.value = "/admin/panel";
        var i4 = document.createElement("input"); i4.type = "hidden"; i4.name = "csrf_token"; i4.value = csrfToken;
        f.appendChild(i1); f.appendChild(i2); f.appendChild(i3); f.appendChild(i4); document.body.appendChild(f); f.submit();
    }
}
function adminDeletePlayer(id, name, csrfToken) {
    if (confirm("Delete player '" + name + "'? This cannot be undone.")) {
        var f = document.createElement("form");
        f.method = "POST"; f.action = "/delete_player";
        var i1 = document.createElement("input"); i1.type = "hidden"; i1.name = "player_id"; i1.value = id;
        var i2 = document.createElement("input"); i2.type = "hidden"; i2.name = "redirect"; i2.value = "/admin/panel";
        var i3 = document.createElement("input"); i3.type = "hidden"; i3.name = "csrf_token"; i3.value = csrfToken;
        f.appendChild(i1); f.appendChild(i2); f.appendChild(i3); document.body.appendChild(f); f.submit();
    }
}
'''

# ---------- VIEW LAYER (redesign handoff) ----------

FONTS = (
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Archivo:wght@400;500;600;700;800&'
    'family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">'
)

NAV_ITEMS = [
    ('/', 'Ladder', 'leaderboard'),
    ('/submit_match', 'Report', 'match'),
    ('/balance', 'Balance', 'balance'),
    ('/history', 'History', 'history'),
    ('/companion', 'Companion', 'companion'),
]


def page(title, content, nav_active=''):
    is_admin = bool(session.get('is_admin'))
    links = ''.join(
        f'<a href="{href}" class="nav-link{" active" if key == nav_active else ""}">{label}</a>'
        for href, label, key in NAV_ITEMS
    )
    if is_admin:
        right = ('<span class="admin-pill">Admin mode</span>'
                 '<a href="/admin/panel" class="btn btn-ghost btn-sm">Panel</a>'
                 '<a href="/admin/logout" class="btn btn-ghost btn-sm">Log out</a>')
    else:
        right = ('<span class="session-note">Signed out</span>'
                 '<a href="/admin" class="btn btn-ghost btn-sm">Admin</a>')
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} · Empire Earth MMR</title>
{FONTS}
<link rel="stylesheet" href="/static/style.css">
</head><body>
<script>{RENAME_JS}</script>
<nav class="navbar">
  <span class="brand">
    <span class="brand-mark">E</span>
    <span class="brand-text">
      <span class="brand-name">Empire Earth</span>
      <span class="brand-sub">MMR Ladder</span>
    </span>
  </span>
  <span class="nav-links">{links}</span>
  <span class="nav-right">{right}</span>
</nav>
<div class="container">{content}</div>
</body></html>'''


def flash_html(msg, type='success'):
    tag = 'Done' if type == 'success' else 'Error'
    return (f'<div class="flash flash-{type}"><span class="tag">{tag}</span>'
            f'<span>{esc(msg)}</span></div>')


def companion_download_url():
    configured = os.environ.get('COMPANION_DOWNLOAD_URL')
    if configured:
        return configured
    filename = 'downloads/Empire-Earth-Companion-0.1.0.exe'
    if os.path.isfile(os.path.join(app.static_folder, filename)):
        return url_for('static', filename=filename)
    return None


def match_evidence_content(match):
    stats = json.loads(match['military_stats']) if match.get('military_stats') else []
    rows = []
    for stat in stats:
        player = query('SELECT name FROM players WHERE id=?', (stat['player_id'],), one=True)
        rows.append(dict(stat, name=player['name'] if player else f'Deleted player #{stat["player_id"]}'))
    return render_template('match_evidence.html', stats=rows,
        evidence=json.loads(match['evidence']) if match.get('evidence') else None,
        rating_details=json.loads(match['rating_details']) if match.get('rating_details') else {},
        rating_version=match.get('rating_version') or 'openskill-v1', source=match.get('source') or 'web')


def _delta_span(chg):
    # '+14' / '-9' -> coloured mono span. Uses a true minus sign for legibility.
    if not chg:
        return '<span class="delta">—</span>'
    cls = 'up' if str(chg).startswith('+') else 'down'
    text = str(chg).replace('-', '−')
    return f'<span class="delta {cls}">{esc(text)}</span>'


def _short_date(raw):
    if not raw:
        return '—'
    dt = raw
    if isinstance(dt, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                dt = datetime.strptime(raw, fmt); break
            except Exception:
                continue
    return dt.strftime('%b %d') if isinstance(dt, datetime) else str(raw)[:10]


def _long_date(raw):
    if not raw:
        return ''
    dt = raw
    if isinstance(dt, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                dt = datetime.strptime(raw, fmt); break
            except Exception:
                continue
    return dt.strftime('%b %d, %Y · %H:%M') if isinstance(dt, datetime) else str(raw)[:16]


def _chip(field, p, checked=False):
    return (f'<label class="chip">'
            f'<input type="checkbox" name="{field}" value="{p["id"]}"{" checked" if checked else ""}>'
            f'<span class="box">&#10003;</span>'
            f'<span class="cname">{esc(p["name"])}</span>'
            f'<span class="cmmr">{p["mmr"]}</span></label>')


def leaderboard_content():
    all_players = query('SELECT * FROM players ORDER BY mmr DESC')
    active = sorted([p for p in all_players if p['wins'] + p['losses'] > 0],
                    key=lambda p: p['mmr'], reverse=True)
    inactive = sorted([p for p in all_players if p['wins'] + p['losses'] == 0],
                      key=lambda p: p['name'].lower())

    # stat strip — two cheap extra queries
    match_count = query('SELECT COUNT(*) as cnt FROM matches WHERE status=?', ('approved',), one=True)
    match_count = match_count['cnt'] if match_count else 0
    last = query("SELECT created_at FROM matches WHERE status='approved' ORDER BY id DESC LIMIT 1", one=True)
    last_str = _short_date(last['created_at']) if last else '—'

    stats = (f'<div class="stats">'
             f'<div class="stat"><div class="stat-k">Players</div><div class="stat-v">{len(all_players)}</div></div>'
             f'<div class="stat"><div class="stat-k">Matches</div><div class="stat-v">{match_count}</div></div>'
             f'<div class="stat" style="min-width:130px"><div class="stat-k">Last match</div>'
             f'<div class="stat-v">{esc(last_str)}</div></div>'
             f'</div>')

    rows = ''
    for i, p in enumerate(active, 1):
        total = p['wins'] + p['losses']
        pct = round(p['wins'] / total * 100)
        rank_cls = {1: 'r1', 2: 'r2', 3: 'r3'}.get(i, '')
        fill_cls = '' if pct >= 50 else ('mid' if pct >= 35 else 'low')
        tag = '<div class="player-tag">Champion</div>' if i == 1 else ''
        rows += (
            f'<div class="ladder-row{" top" if i == 1 else ""}">'
            f'<div class="rank {rank_cls}">{i:02d}</div>'
            f'<div class="player">'
            f'<span class="avatar{" gold" if i == 1 else ""}">{esc(p["name"][:1].upper())}</span>'
            f'<span><span class="player-name">{esc(p["name"])}</span>{tag}</span></div>'
            f'<div class="mmr">{p["mmr"]}</div>'
            f'<div class="record"><span class="w">{p["wins"]}</span> — <span class="l">{p["losses"]}</span></div>'
            f'<div class="wr"><span class="wr-bar"><span class="wr-fill {fill_cls}" style="width:{pct}%"></span></span>'
            f'<span class="wr-val">{pct}%</span></div>'
            f'</div>'
        )

    if inactive:
        rows += '<div class="group-label">Unranked · no games played</div>'
        for p in inactive:
            rows += (f'<div class="ladder-row unranked"><div class="rank" style="font-size:13px">—</div>'
                     f'<div class="player"><span class="player-name" style="font-weight:500;font-size:14px">'
                     f'{esc(p["name"])}</span></div>'
                     f'<div class="dash">—</div><div class="dash">—</div><div class="dash">—</div></div>')

    if not all_players:
        rows = ('<div class="empty"><div class="big">Empty ladder</div>'
                '<div>Add players from the admin panel to get started.</div></div>')

    return f'''
<div class="page-head">
  <div><h1>Ladder</h1>
  <div class="page-sub">OpenSkill (Plackett–Luce) rating · fresh players start at 1000</div></div>
  {stats}
</div>
<div class="panel ladder">
  <div class="ladder-head"><div>Rank</div><div>Player</div><div class="num-right">MMR</div>
  <div class="num-right">W — L</div><div class="num-right">Win rate</div></div>
  {rows}
</div>'''


def submit_match_content(msg, players):
    chips1 = ''.join(_chip('team1', p) for p in players)
    chips2 = ''.join(_chip('team2', p) for p in players)
    return f'''
<h1>Report a match</h1>
<div class="page-sub" style="margin-bottom:24px">Pick both rosters, then mark the winner.
Submissions wait for admin approval.</div>
{msg}
<form method="post">{csrf_field()}
<div class="versus">
  <div class="panel">
    <div class="panel-head accent"><span class="panel-title accent">Team 1</span></div>
    <div class="panel-body"><div class="chips">{chips1}</div></div>
    <div style="padding:0 16px 16px">
      <button type="submit" name="winner" value="team1" class="btn btn-pick on">Team 1 won</button>
    </div>
  </div>
  <div class="versus-divider"><span class="rule"></span><span class="vs">VS</span><span class="rule down"></span></div>
  <div class="panel">
    <div class="panel-head"><span class="panel-title">Team 2</span></div>
    <div class="panel-body"><div class="chips team2">{chips2}</div></div>
    <div style="padding:0 16px 16px">
      <button type="submit" name="winner" value="team2" class="btn btn-pick">Team 2 won</button>
    </div>
  </div>
</div>
<div class="form-footer">
  <span class="hint">A player can't sit on both rosters. Pick the winning side to submit.</span>
</div>
</form>'''


def balance_form(players):
    chips = ''.join(_chip('players', p) for p in players)
    return f'''
<h1>Team balancer</h1>
<div class="page-sub" style="margin-bottom:24px">Select who's playing.
The split targets a 50% win probability using skill, confidence and team size.</div>
<form method="post">{csrf_field()}
<div class="panel" style="margin-bottom:24px">
  <div class="panel-head"><span class="panel-title">Pool</span></div>
  <div class="panel-body"><div class="chips">{chips}</div></div>
  <div style="padding:0 16px 16px"><button type="submit" class="btn">Balance teams</button></div>
</div>
</form>'''


def balance_result(t1, t2, diff, handicapped, deltas_t1_wins, deltas_t2_wins):
    def team_block(team, label, accent):
        rows = ''.join(
            f'<div class="prow"><span class="n">{esc(p["name"])}</span>'
            f'<span class="delta" style="color:var(--text-2)">{p["mmr"]}</span></div>'
            for p in team)
        return (f'<div class="split-team"><div class="split-head">'
                f'<span class="panel-title{" accent" if accent else ""}">{label}</span>'
                f'<span class="split-avg">avg <b>{team_avg_mmr(team):.0f}</b></span></div>'
                f'<div class="rows">{rows}</div></div>')

    def stake(label, deltas, accent):
        r1 = ''.join(f'<div class="prow"><span class="n" style="font-weight:500;color:var(--text-2)">'
                     f'{esc(p["name"])}</span>{_delta_span(deltas.get(p["name"], ""))}</div>' for p in t1)
        r2 = ''.join(f'<div class="prow"><span class="n" style="font-weight:500;color:var(--text-2)">'
                     f'{esc(p["name"])}</span>{_delta_span(deltas.get(p["name"], ""))}</div>' for p in t2)
        return (f'<div><div class="stake-title{" accent" if accent else ""}">{label}</div>'
                f'<div class="rows">{r1}<div class="sep"></div>{r2}</div></div>')

    p1, p2 = predict_win(t1, t2)
    meta = f'{len(t1)}v{len(t2)} · win chance {p1:.0%} / {p2:.0%}'
    note = ('<div class="hint" style="padding:0 20px 18px">Uneven teams can remain uneven in strength. '
            'These probabilities are model estimates, not guarantees.</div>') if handicapped else ''
    return f'''
<div class="panel" style="margin-bottom:24px">
  <div class="panel-head"><span class="panel-title">Suggested split</span>
  <span class="panel-meta" style="color:var(--win)">{meta}</span></div>
  <div class="split">
    {team_block(t1, 'Team 1', True)}
    <div class="versus-divider"><span class="vs">VS</span></div>
    {team_block(t2, 'Team 2', False)}
  </div>
  {note}
</div>
<div class="panel">
  <div class="panel-head"><span class="panel-title">What's at stake</span></div>
  <div class="stake">
    {stake('If Team 1 wins', deltas_t1_wins, True)}
    {stake('If Team 2 wins', deltas_t2_wins, False)}
  </div>
</div>'''


def history_content():
    status = request.args.get('status', '')
    if status in ('pending', 'approved', 'denied'):
        matches = query('SELECT * FROM matches WHERE status=? ORDER BY id DESC', (status,))
    else:
        matches = query('SELECT * FROM matches ORDER BY id DESC')
    is_admin = bool(session.get('is_admin'))
    most_recent = query("SELECT id FROM matches WHERE status='approved' ORDER BY rated_order DESC,id DESC LIMIT 1", one=True)
    most_recent_id = most_recent['id'] if most_recent else None

    def filt(key, label):
        on = (status == key) or (key == '' and status not in ('pending', 'approved', 'denied'))
        href = '/history' if key == '' else f'/history?status={key}'
        cls = 'btn btn-solid-2 btn-sm' if on else 'btn btn-ghost btn-sm'
        return f'<a href="{href}" class="{cls}">{label}</a>'

    cards = ''
    for m in matches:
        t1, t2 = json.loads(m['team1']), json.loads(m['team2'])
        changes = json.loads(m['mmr_changes']) if m['mmr_changes'] else {}
        t1_won = m['winner'] == 'team1'

        def side(names, won, label):
            rows = ''.join(f'<div class="prow"><span class="n">{esc(n)}</span>'
                           f'{_delta_span(changes.get(n, ""))}</div>' for n in names)
            suffix = ' · won' if won else ''
            return (f'<div class="side{" won" if won else ""}">'
                    f'<div class="side-label">{label}{suffix}</div>'
                    f'<div class="rows">{rows}</div></div>')

        foot = ''
        if is_admin and m['status'] == 'pending':
            foot = (f'<div class="match-foot"><span class="who">Awaiting approval</span>'
                    f'<form method="post" action="/admin/approve/{m["id"]}" style="margin:0">{csrf_field()}'
                    f'<button class="btn btn-win btn-sm">Approve</button></form>'
                    f'<form method="post" action="/admin/deny/{m["id"]}" style="margin:0">{csrf_field()}'
                    f'<button class="btn btn-danger btn-sm">Deny</button></form></div>')
        elif is_admin and m['status'] == 'approved':
            if m['id'] == most_recent_id:
                actions = (f'<a href="/admin/edit_last_match" class="btn btn-solid-2 btn-sm">Edit</a>'
                           f'<form method="post" action="/admin/delete_last_match" style="margin:0">{csrf_field()}'
                           f'<button class="btn btn-danger btn-sm" onclick="return confirm('
                           f"'Delete this match? MMR will be reversed.'"
                           f')">Delete</button></form>')
            else:
                actions = (f'<form method="post" action="/admin/delete_match/{m["id"]}" style="margin:0">'
                           f'{csrf_field()}<button class="btn btn-danger btn-sm" onclick="return confirm('
                           f"'Delete match #" + str(m['id']) + " ? All MMR will be recalculated.'"
                           f')">Delete</button></form>')
            foot = f'<div class="match-foot"><span class="who">Admin actions</span>{actions}</div>'
        elif is_admin and m['status'] == 'denied':
            foot = (f'<div class="match-foot"><span class="who">Denied</span>'
                    f'<form method="post" action="/admin/delete_denied_match/{m["id"]}" style="margin:0">'
                    f'{csrf_field()}<button class="btn btn-danger btn-sm">Delete</button></form></div>')

        cards += f'''<div class="match{" pending" if m["status"] == "pending" else ""}">
  <div class="match-head">
    <span style="display:flex;align-items:center;gap:12px">
      <span class="match-id">#{m["id"]}</span>
      <span class="badge badge-{esc(m["status"])}">{esc(m["status"])}</span>
      <span class="match-meta">{len(t1)}v{len(t2)}</span>
    </span>
    <span class="match-meta">{esc(_long_date(m.get("created_at")))}</span>
  </div>
  <div class="match-body">
    {side(t1, t1_won, 'Team 1')}
    <div class="versus-divider"><span class="vs" style="color:var(--line-strong)">VS</span></div>
    {side(t2, not t1_won, 'Team 2')}
  </div>
  {match_evidence_content(m)}
  {foot}
</div>'''

    if not matches:
        cards = ('<div class="empty"><div class="big">No matches</div>'
                 '<div>Reported results will appear here.</div></div>')

    return f'''
<div class="page-head">
  <div><h1>Match history</h1>
  <div class="page-sub">{len(matches)} matches · newest first</div></div>
  <div class="filters">{filt('', 'All')}{filt('pending', 'Pending')}{filt('approved', 'Approved')}</div>
</div>
{cards}'''


def admin_login_content(msg):
    return f'''
<div class="login-wrap"><div style="width:380px">
  {msg}
  <div class="login-card">
    <div class="brand-mark">E</div>
    <h2>Admin access</h2>
    <p>Sessions expire after 30 minutes of inactivity.</p>
    <form method="post">{csrf_field()}
      <label class="field">Password</label>
      <input type="password" name="password" placeholder="••••••••" required
             style="margin-bottom:16px">
      <button type="submit" class="btn" style="width:100%">Unlock</button>
    </form>
  </div>
</div></div>'''


def admin_panel_content(pending, players):
    queue = ''
    for m in pending:
        t1, t2 = json.loads(m['team1']), json.loads(m['team2'])
        changes = json.loads(m['mmr_changes']) if m['mmr_changes'] else {}
        winner_label = 'Team 1 won' if m['winner'] == 'team1' else 'Team 2 won'
        deltas = ' · '.join(
            f'{esc(k)} <span class="{"up" if str(v).startswith("+") else "down"}" '
            f'style="color:var(--{"win" if str(v).startswith("+") else "loss"})">'
            f'{esc(str(v).replace("-", chr(0x2212)))}</span>'
            for k, v in changes.items())
        queue += f'''<div class="queue">
  <div class="lines">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="match-id">#{m["id"]}</span>
      <span class="badge badge-pending">pending</span>
      <span class="match-meta">{esc(_long_date(m.get("created_at")))}</span>
    </div>
    <div class="who">{esc(", ".join(t1))} <span class="vs-lite">vs</span> {esc(", ".join(t2))}
      <span class="match-meta" style="color:var(--win);margin-left:6px">{winner_label}</span></div>
    <div class="deltas">{deltas}</div>
    {match_evidence_content(m)}
  </div>
  <div class="btn-row">
    <form method="post" action="/admin/approve/{m["id"]}" style="margin:0">{csrf_field()}
      <button class="btn btn-win">Approve</button></form>
    <form method="post" action="/admin/deny/{m["id"]}" style="margin:0">{csrf_field()}
      <button class="btn btn-danger">Deny</button></form>
  </div>
</div>'''
    if not pending:
        queue = ('<div class="empty" style="padding:36px 20px;margin-bottom:32px">'
                 '<div class="big">Queue clear</div><div>No matches awaiting review.</div></div>')

    rows = ''
    for p in players:
        js_name = esc(json.dumps(p['name']))
        unranked = ('<span class="tag-unranked">unranked</span>'
                    if p['wins'] + p['losses'] == 0 else '')
        rows += f'''<div class="prow-grid">
  <div><span class="player-name" style="font-size:14px">{esc(p["name"])}</span>{unranked}</div>
  <div class="mmr" style="font-size:14px">{p["mmr"]}</div>
  <div class="record" style="font-size:12px">{p["wins"]} — {p["losses"]}</div>
  <div class="acts">
    <form method="post" action="/admin/set_mmr" style="display:flex;gap:6px;margin:0">{csrf_field()}
      <input type="hidden" name="player_id" value="{p["id"]}">
      <input name="mmr" type="number" value="{p["mmr"]}" class="mmr-input">
      <button class="btn btn-solid-2 btn-sm">Set</button></form>
    <button class="btn btn-ghost btn-sm"
      onclick="adminRenamePlayer({p["id"]}, {js_name}, '{csrf_token()}')">Rename</button>
    <form method="post" action="/admin/reset/{p["id"]}" style="margin:0">{csrf_field()}
      <button class="btn btn-ghost btn-sm">Reset</button></form>
    <button class="btn btn-danger btn-sm"
      onclick="adminDeletePlayer({p["id"]}, {js_name}, '{csrf_token()}')">Del</button>
  </div>
</div>'''

    return f'''
<div class="page-head">
  <div><h1>Admin</h1>
  <div class="page-sub">{len(pending)} awaiting review · {len(players)} players</div></div>
  <div class="btn-row">
    <a href="/admin/companion" class="btn btn-ghost btn-sm">Companion devices</a>
    <a href="/add_player" class="btn btn-sm" style="padding:11px 16px">+ Add player</a>
    <form method="post" action="/admin/recalculate_mmr" style="margin:0">{csrf_field()}
      <button class="btn btn-ghost btn-sm" style="padding:11px 16px"
        onclick="return confirm('Recalculate all MMR from scratch?')">Recalculate MMR</button></form>
  </div>
</div>
<div class="section-label">Approval queue</div>
{queue}
<div class="section-label">Players</div>
<div class="panel ptable">
  <div class="ladder-head"><div>Player</div><div class="num-right">MMR</div>
  <div class="num-right">W — L</div><div class="num-right">Actions</div></div>
  {rows}
</div>'''


def add_player_content(msg):
    return f'''
<div class="crumb"><a href="/admin/panel">Admin</a> / Add player</div>
<h1 style="margin-bottom:24px">Add player</h1>
{msg}
<div class="card-form">
  <form method="post">{csrf_field()}
    <label class="field">Player name</label>
    <input name="name" placeholder="Discord handle" required
           style="font-family:var(--ui);margin-bottom:16px">
    <div class="btn-row">
      <button type="submit" class="btn">Add player</button>
      <a href="/admin/panel" class="btn btn-ghost">Back</a>
    </div>
  </form>
</div>'''


def edit_match_content(m, players, old_t1, old_t2, old_winner, msg):
    chips1 = ''.join(_chip('team1', p, p['name'] in old_t1) for p in players)
    chips2 = ''.join(_chip('team2', p, p['name'] in old_t2) for p in players)
    return f'''
<div class="crumb"><a href="/history">History</a> / Edit #{m["id"]}</div>
<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:6px">
  <h1>Edit match</h1>
  <span class="mmr" style="color:var(--amber);font-size:20px">#{m["id"]}</span>
</div>
<div class="page-sub" style="margin-bottom:24px">Saving replays every approved match, so all
ratings are recalculated from scratch.</div>
{msg}
<form method="post">{csrf_field()}
<input type="hidden" name="match_id" value="{m['id']}">
<div class="versus" style="margin-bottom:20px">
  <div class="panel">
    <div class="panel-head accent"><span class="panel-title accent">Team 1</span>
      {'<span class="panel-meta" style="color:var(--win)">Winner</span>' if old_winner == 'team1' else ''}</div>
    <div class="panel-body"><div class="chips">{chips1}</div></div>
    <div style="padding:0 16px 16px">
      <button type="submit" name="winner" value="team1"
        class="btn btn-pick{' on' if old_winner == 'team1' else ''}">Team 1 won</button></div>
  </div>
  <div class="versus-divider"><span class="vs">VS</span></div>
  <div class="panel">
    <div class="panel-head"><span class="panel-title">Team 2</span>
      {'<span class="panel-meta" style="color:var(--win)">Winner</span>' if old_winner == 'team2' else ''}</div>
    <div class="panel-body"><div class="chips team2">{chips2}</div></div>
    <div style="padding:0 16px 16px">
      <button type="submit" name="winner" value="team2"
        class="btn btn-pick{' on' if old_winner == 'team2' else ''}">Team 2 won</button></div>
  </div>
</div>
<div class="btn-row" style="border-top:1px solid var(--line-soft);padding-top:20px">
  <a href="/history" class="btn btn-ghost">Cancel</a>
</div>
</form>'''


# ---------- ROUTES ----------
@app.route('/')
def leaderboard():
    return page('Leaderboard', leaderboard_content(), 'leaderboard')

@app.route('/rename_player', methods=['POST'])
@admin_required
def rename_player_route():
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    pid = request.form.get('player_id')
    new_name = request.form.get('new_name', '').strip()
    redirect_to = '/admin/panel' if request.form.get('redirect') == '/admin/panel' else '/'
    if not pid or not pid.isdigit() or not new_name or len(new_name) > 80:
        return redirect(redirect_to)
    try:
        with rating_transaction():
            player = query('SELECT * FROM players WHERE id = ?', (int(pid),), one=True)
            if not player or player['name'] == new_name:
                return redirect(redirect_to)
            old_name = player['name']
            query('UPDATE players SET name=? WHERE id=?', (new_name, int(pid)), commit=True)
            rename_player_in_matches(old_name, new_name)
        logger.info(f'Player renamed: {old_name} -> {new_name}')
    except Exception as e:
        logger.error(f'Rename failed: {e}')
    return redirect(redirect_to)

@app.route('/delete_player', methods=['POST'])
@admin_required
def delete_player_route():
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    pid = request.form.get('player_id')
    redirect_to = '/admin/panel' if request.form.get('redirect') == '/admin/panel' else '/'
    if not pid or not pid.isdigit():
        return redirect(redirect_to)
    try:
        with rating_transaction():
            player = query('SELECT * FROM players WHERE id=?', (int(pid),), one=True)
            if player:
                remove_player(player)
        logger.info(f'Player deleted: id={pid}')
    except ValueError as error:
        return page('Player has match history', flash_html(str(error), 'error') +
                    '<a class="btn btn-ghost" href="/admin/panel">Back to admin</a>', 'admin'), 409
    except Exception as e:
        logger.error(f'Delete failed: {e}')
    return redirect(redirect_to)

@app.route('/api/players/rename', methods=['POST'])
def api_rename_player():
    if not admin_authenticated():
        return jsonify({'error': 'Admin authentication required'}), 403
    if not check_csrf():
        return jsonify(error='Invalid CSRF token.'), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='A JSON object is required.'), 400
    player_id = data.get('player_id')
    new_name = data.get('new_name', '')
    if type(player_id) is not int or not isinstance(new_name, str) or not 0 < len(new_name.strip()) <= 80:
        return jsonify({'error': 'player_id and new_name are required'}), 400
    new_name = new_name.strip()
    try:
        with rating_transaction():
            player = query('SELECT * FROM players WHERE id=?', (player_id,), one=True)
            if not player:
                return jsonify(error='Player not found'), 404
            old_name = player['name']
            query('UPDATE players SET name=? WHERE id=?', (new_name, player_id), commit=True)
            rename_player_in_matches(old_name, new_name)
        return jsonify({'success': True, 'old_name': old_name, 'new_name': new_name}), 200
    except Exception as e:
        logger.error(f'API error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/players/<name>', methods=['DELETE'])
def api_delete_player(name):
    if not admin_authenticated():
        return jsonify({'error': 'Admin authentication required'}), 403
    if not check_csrf():
        return jsonify(error='Invalid CSRF token.'), 403
    try:
        with rating_transaction():
            player = query('SELECT * FROM players WHERE name=?', (name,), one=True)
            if not player:
                return jsonify({'error': 'Player not found'}), 404
            remove_player(player)
        return jsonify({'success': True, 'deleted': name}), 200
    except ValueError as error:
        return jsonify(error=str(error)), 409
    except Exception as e:
        logger.error(f'API error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/add_player', methods=['GET','POST'])
@admin_required
def add_player():
    msg = ''
    if request.method == 'POST':
        if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
        elif not (name := request.form.get('name','').strip()) or len(name) > 80:
            msg = flash_html('Enter a name from 1 to 80 characters.', 'error')
        else:
            try:
                with rating_transaction():
                    query('INSERT INTO players (name, mmr) VALUES (?, ?)', (name, DEFAULT_MMR), commit=True)
                msg = flash_html(f'Player {name} added with {DEFAULT_MMR} MMR!')
            except:
                msg = flash_html(f'Player already exists.', 'error')
    content = add_player_content(msg)
    return page('Add Player', content, 'add')

@app.route('/submit_match', methods=['GET','POST'])
def submit_match():
    msg = ''
    players = query('SELECT * FROM players ORDER BY name')
    if request.method == 'POST':
      if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
      else:
        try:
            with rating_transaction():
                players = query('SELECT * FROM players ORDER BY name')
                winner = request.form.get('winner')
                t1_players, t2_players = validated_web_rosters(request.form.getlist('team1'),
                    request.form.getlist('team2'), winner, players)
                w_team, l_team = (t1_players, t2_players) if winner == 'team1' else (t2_players, t1_players)
                status = 'pending' if REQUIRE_MATCH_APPROVAL else 'approved'
                if status == 'approved':
                    changes, details = apply_openskill_match(w_team, l_team)
                    rated_order = next_rated_order()
                else:
                    changes, details = calculate_match(w_team, l_team)
                    rated_order = None
                query('''INSERT INTO matches
                    (team1,team2,winner,mmr_changes,status,rating_version,rating_details,rated_order)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (json.dumps([p['name'] for p in t1_players]), json.dumps([p['name'] for p in t2_players]),
                     winner, json.dumps(changes), status, 'military-v2', json.dumps(details), rated_order), commit=True)
            summary = ', '.join(f"{n} {d}" for n, d in changes.items())
            if status == 'approved':
                msg = flash_html(f'Match recorded! MMR changes: {summary}')
            else:
                msg = flash_html(f'Match submitted for admin approval. Estimated changes: {summary}')
        except ValueError as error:
            msg = flash_html(str(error), 'error')
    content = submit_match_content(msg, players)
    return page('Submit Match', content, 'match')


@app.route('/balance', methods=['GET','POST'])
def balance():
    players = query('SELECT * FROM players ORDER BY name')
    result = ''
    if request.method == 'POST':
      if not check_csrf():
            result = flash_html('Invalid request.', 'error')
      else:
        sel = request.form.getlist('players')
        if not 2 <= len(sel) <= 10 or len(set(sel)) != len(sel):
            result = flash_html('Select 2 to 10 different players.', 'error')
        elif not set(sel).issubset({str(p['id']) for p in players}):
            result = flash_html('Unknown player. Refresh the roster and try again.', 'error')
        else:
            t1, t2, diff, handicapped = balance_teams([int(x) for x in sel])
            if t1 and t2:
                deltas_t1_wins = preview_openskill_deltas(t1, t2)
                deltas_t2_wins = preview_openskill_deltas(t2, t1)
                result = balance_result(t1, t2, diff, handicapped, deltas_t1_wins, deltas_t2_wins)
    content = balance_form(players) + result
    return page('Team Balancer', content, 'balance')

@app.route('/history')
def history():
    return page('Match History', history_content(), 'history')


# ---------- ADMIN ----------
@app.route('/admin', methods=['GET','POST'])
def admin_login():
    if session.get('is_admin'):
        return redirect(url_for('admin_panel'))
    msg = ''
    if request.method == 'POST':
        if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
        elif hashlib.sha256(request.form.get('password', '').encode()).hexdigest() == ADMIN_PASSWORD_HASH:
            session['is_admin'] = True
            session['admin_login_time'] = time.time()
            return redirect(url_for('admin_panel'))
        else:
            msg = flash_html('Wrong password.', 'error')
    content = admin_login_content(msg)
    return page('Admin', content, 'admin')

@app.route('/admin/panel')
@admin_required
def admin_panel():
    pending = query("SELECT * FROM matches WHERE status='pending' ORDER BY id DESC")
    players = query('SELECT * FROM players ORDER BY name')
    content = admin_panel_content(pending, players)
    return page('Admin Panel', content, 'admin')

@app.route('/admin/approve/<int:match_id>', methods=['POST'])
@admin_required
def approve_match(match_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    with rating_transaction():
        m = query('SELECT * FROM matches WHERE id=? AND status=?', (match_id, 'pending'), one=True)
        if m:
            w_players, l_players = match_players(m)
            if not w_players or not l_players:
                return page('Approval failed', flash_html('The complete roster is no longer available. Deny this report and submit a corrected match.', 'error'), 'admin'), 409
            changes, details = apply_openskill_match(w_players, l_players,
                stats=json.loads(m['military_stats']) if m.get('military_stats') else None,
                version=m.get('rating_version') or 'openskill-v1')
            query('UPDATE matches SET status=?,mmr_changes=?,rating_details=?,rated_order=? WHERE id=?',
                  ('approved', json.dumps(changes), json.dumps(details), next_rated_order(), match_id), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/deny/<int:match_id>', methods=['POST'])
@admin_required
def deny_match(match_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    with rating_transaction():
        query('UPDATE matches SET status=? WHERE id=? AND status=?', ('denied', match_id, 'pending'), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete_last_match', methods=['POST'])
@admin_required
def delete_last_match():
    if not check_csrf():
        return redirect(url_for('history'))
    with rating_transaction():
        m = query("SELECT * FROM matches WHERE status='approved' ORDER BY rated_order DESC,id DESC LIMIT 1", one=True)
        if not m:
            return redirect(url_for('history'))
        remove_match(m['id'])
        replayed = recalc_all_openskill()
    logger.info(f'Deleted last match #{m["id"]} and replayed {replayed} approved matches')
    return redirect(url_for('history'))


@app.route('/admin/delete_match/<int:match_id>', methods=['POST'])
@admin_required
def delete_match(match_id):
    """Delete any match by ID and recalculate all MMR from scratch."""
    if not check_csrf():
        return redirect(url_for('history'))
    with rating_transaction():
        m = query('SELECT * FROM matches WHERE id=?', (match_id,), one=True)
        if not m:
            return redirect(url_for('history'))
        remove_match(match_id)
        replayed = recalc_all_openskill() if m['status'] == 'approved' else 0
    logger.info(f'MMR recalculated after deleting match #{match_id}, replayed {replayed} matches')
    return redirect(url_for('history'))

@app.route('/admin/delete_denied_match/<int:match_id>', methods=['POST'])
@admin_required
def delete_denied_match(match_id):
    """Delete a denied match. No MMR recalculation needed since denied matches don't affect ratings."""
    if not check_csrf():
        return redirect(url_for('history'))
    with rating_transaction():
        m = query('SELECT * FROM matches WHERE id=? AND status=?', (match_id, 'denied'), one=True)
        if not m:
            return redirect(url_for('history'))
        remove_match(match_id)
    logger.info(f'Deleted denied match #{match_id}')
    return redirect(url_for('history'))

@app.route('/admin/edit_last_match', methods=['GET','POST'])
@admin_required
def edit_last_match():
    posted_id = request.form.get('match_id', '')
    if request.method == 'POST' and not posted_id.isdigit():
        return redirect(url_for('history'))
    m = (query("SELECT * FROM matches WHERE id=? AND status='approved'", (int(posted_id),), one=True)
         if request.method == 'POST' else
         query("SELECT * FROM matches WHERE status='approved' ORDER BY rated_order DESC,id DESC LIMIT 1", one=True))
    if not m:
        return redirect(url_for('history'))
    players = query('SELECT * FROM players ORDER BY name')
    msg = ''
    if request.method == 'POST':
      if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
      else:
        try:
            with rating_transaction():
                m = query("SELECT * FROM matches WHERE id=? AND status='approved'", (m['id'],), one=True)
                if not m:
                    return redirect(url_for('history'))
                players = query('SELECT * FROM players ORDER BY name')
                new_winner = request.form.get('winner')
                t1, t2 = validated_web_rosters(request.form.getlist('team1'), request.form.getlist('team2'), new_winner, players)
                names1, names2 = [p['name'] for p in t1], [p['name'] for p in t2]
                roster_changed = set(names1) != set(json.loads(m['team1'])) or set(names2) != set(json.loads(m['team2']))
                # Keep original order for a winner-only correction; OpenSkill replay is order-sensitive.
                if not roster_changed:
                    names1, names2 = json.loads(m['team1']), json.loads(m['team2'])
                query('UPDATE matches SET team1=?,team2=?,winner=? WHERE id=?',
                      (json.dumps(names1), json.dumps(names2), new_winner, m['id']), commit=True)
                if roster_changed:
                    query('UPDATE matches SET military_stats=NULL,evidence=NULL,rating_details=NULL WHERE id=?', (m['id'],), commit=True)
                replayed = recalc_all_openskill()
            logger.info(f'Edited match #{m["id"]} and replayed {replayed} approved matches')
            return redirect(url_for('history'))
        except ValueError as error:
            msg = flash_html(str(error), 'error')
    old_t1 = json.loads(m['team1'])
    old_t2 = json.loads(m['team2'])
    old_winner = m['winner']
    content = edit_match_content(m, players, old_t1, old_t2, old_winner, msg)
    return page('Edit Match', content, 'history')

@app.route('/admin/set_mmr', methods=['POST'])
@admin_required
def set_mmr():
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    pid = request.form.get('player_id')
    mmr = request.form.get('mmr')
    try:
        pid, mmr = int(pid), int(mmr)
        if not -100000 <= mmr <= 100000:
            return redirect(url_for('admin_panel'))
    except (TypeError, ValueError):
        return redirect(url_for('admin_panel'))
    with rating_transaction():
        player = query('SELECT * FROM players WHERE id=?', (pid,), one=True)
        if player:
            sigma = player['sigma'] if player.get('sigma') is not None else OS_DEFAULT_SIGMA
            mu = (mmr - RATING_OFFSET) / RATING_SCALE + 3 * sigma
            query('UPDATE players SET mmr=?,mu=?,sigma=? WHERE id=?', (mmr, mu, sigma, pid), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/reset/<int:player_id>', methods=['POST'])
@admin_required
def reset_player(player_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    with rating_transaction():
        query('UPDATE players SET mmr=?,mu=?,sigma=?,wins=0,losses=0 WHERE id=?',
              (DEFAULT_MMR, OS_DEFAULT_MU, OS_DEFAULT_SIGMA, player_id), commit=True)
    return redirect(url_for('admin_panel'))


@app.route('/admin/recalculate_mmr', methods=['POST'])
@admin_required
def recalculate_mmr():
    """Replay all approved matches in order with the current MMR formula,
    including team-size adjustments. Resets all players first."""
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    replayed = recalc_all_openskill()
    logger.info(f'Recalculated MMR (OpenSkill PlackettLuce) for {replayed} matches')
    return redirect(url_for('admin_panel'))

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

from companion_api import register_companion
register_companion(app, sys.modules[__name__])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
