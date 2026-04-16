import os, json, sqlite3, hashlib, secrets, math, time, logging, html
from datetime import datetime
from itertools import combinations
from functools import wraps
from flask import Flask, request, redirect, url_for, session, jsonify, g
from openskill.models import PlackettLuce

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DATABASE_URL = os.environ.get('DATABASE_URL', '')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD', '')
REQUIRE_MATCH_APPROVAL = os.environ.get('REQUIRE_MATCH_APPROVAL', 'true').lower() == 'true'
DEFAULT_MMR = 1000
K_FACTOR = 32  # legacy, unused
ADMIN_SESSION_TIMEOUT = 1800  # 30 minutes

# --- OpenSkill (Plackett-Luce / Weng-Lin) rating model ---
OS_MODEL = PlackettLuce()
OS_DEFAULT_MU = 25.0
OS_DEFAULT_SIGMA = 25.0 / 3.0
RATING_SCALE = 40.0
RATING_OFFSET = 1000.0

def ordinal_to_mmr(mu, sigma):
    """Map an OpenSkill rating (mu, sigma) to a displayable integer MMR.
    Uses (mu - 3*sigma) * SCALE + OFFSET so a fresh player (mu=25, sigma=25/3)
    lands on 1000 and a strong/confident rating grows from there."""
    return round((mu - 3.0 * sigma) * RATING_SCALE + RATING_OFFSET)

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
    token = request.form.get('csrf_token', '')
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
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'empire.db')
            g.db = sqlite3.connect(db_path, timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA journal_mode=WAL')
            g.db.execute('PRAGMA busy_timeout=5000')
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
        if not use_postgres:
            db.commit()
        cur.close()
        return cur.lastrowid if not use_postgres else None
    rows = cur.fetchall()
    cur.close()
    if use_postgres:
        return rows[0] if one and rows else rows if not one else None
    else:
        results = [dict(r) for r in rows]
        return results[0] if one and results else results if not one else None


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
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'empire.db')
            if not os.path.exists(db_path):
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
            query('UPDATE matches SET team1=?, team2=?, mmr_changes=? WHERE id=?',
                  (json.dumps(t1), json.dumps(t2), json.dumps(changes), m['id']), commit=True)

def expected(ra, rb):
    """Legacy Elo helper (retained only for backward compatibility - not used by OpenSkill path)."""
    return 1.0 / (1 + 10 ** ((rb - ra) / 400.0))


def _player_rating(p):
    """Build an OpenSkill Rating object from a player row, using defaults if mu/sigma are null."""
    mu_val = p['mu'] if ('mu' in p and p['mu'] is not None) else OS_DEFAULT_MU
    sigma_val = p['sigma'] if ('sigma' in p and p['sigma'] is not None) else OS_DEFAULT_SIGMA
    return OS_MODEL.rating(mu=float(mu_val), sigma=float(sigma_val), name=p['name'])


def _os_rate_teams(w_players, l_players):
    """Run the Plackett-Luce update. Returns (new_w_ratings, new_l_ratings)."""
    w_ratings = [_player_rating(p) for p in w_players]
    l_ratings = [_player_rating(p) for p in l_players]
    # ranks=[0,1] means team 0 beats team 1
    new_w, new_l = OS_MODEL.rate([w_ratings, l_ratings], ranks=[0, 1])
    return new_w, new_l


def _fmt_delta(n):
    return f'+{n}' if n >= 0 else f'{n}'


def preview_openskill_deltas(w_players, l_players):
    """Compute expected per-player MMR deltas without touching the DB."""
    new_w, new_l = _os_rate_teams(w_players, l_players)
    changes = {}
    for p, r in zip(w_players, new_w):
        old_mmr = p['mmr'] if p.get('mmr') is not None else DEFAULT_MMR
        new_mmr = ordinal_to_mmr(r.mu, r.sigma)
        changes[p['name']] = _fmt_delta(new_mmr - old_mmr)
    for p, r in zip(l_players, new_l):
        old_mmr = p['mmr'] if p.get('mmr') is not None else DEFAULT_MMR
        new_mmr = ordinal_to_mmr(r.mu, r.sigma)
        changes[p['name']] = _fmt_delta(new_mmr - old_mmr)
    return changes


def apply_openskill_match(w_players, l_players, update_counts=True):
    """Rate a match and persist mu/sigma/mmr (and optionally wins/losses). Returns changes dict."""
    new_w, new_l = _os_rate_teams(w_players, l_players)
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
    return changes


def recalc_all_openskill():
    """Reset every player to fresh rating and replay every approved match in chronological order."""
    query('UPDATE players SET mu=?, sigma=?, mmr=?, wins=0, losses=0',
          (OS_DEFAULT_MU, OS_DEFAULT_SIGMA, DEFAULT_MMR), commit=True)
    matches = query("SELECT * FROM matches WHERE status='approved' ORDER BY id ASC")
    for m in matches:
        t1_names = json.loads(m['team1'])
        t2_names = json.loads(m['team2'])
        w_names = t1_names if m['winner'] == 'team1' else t2_names
        l_names = t2_names if m['winner'] == 'team1' else t1_names
        w_players = [query('SELECT * FROM players WHERE name=?', (n,), one=True) for n in w_names]
        l_players = [query('SELECT * FROM players WHERE name=?', (n,), one=True) for n in l_names]
        w_players = [p for p in w_players if p]
        l_players = [p for p in l_players if p]
        if not w_players or not l_players:
            continue
        changes = apply_openskill_match(w_players, l_players, update_counts=True)
        query('UPDATE matches SET mmr_changes=? WHERE id=?',
              (json.dumps(changes), m['id']), commit=True)
    return len(matches)

def team_avg_mmr(players_list):
    if not players_list:
        return 0
    return sum(p['mmr'] for p in players_list) / len(players_list)

HANDICAP_FACTOR = 0.5  # Tunable: controls how much extra avg MMR the smaller team gets

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
    # Calculate handicap target for unequal teams
    if unequal:
        all_mmr_avg = sum(p['mmr'] for p in players) / n
        size_ratio = (half + 1) / half  # larger / smaller
        target_handicap = all_mmr_avg * (size_ratio - 1) * HANDICAP_FACTOR
    else:
        target_handicap = 0
    for sz in sizes:
        for combo in combinations(range(n), sz):
            t1 = [players[i] for i in combo]
            t2 = [players[i] for i in range(n) if i not in combo]
            avg1 = team_avg_mmr(t1)
            avg2 = team_avg_mmr(t2)
            if unequal:
                # Identify which team is smaller
                if len(t1) <= len(t2):
                    small_avg, large_avg = avg1, avg2
                else:
                    small_avg, large_avg = avg2, avg1
                # Smaller team should have higher avg; minimize distance to target
                actual_diff = small_avg - large_avg
                score = abs(actual_diff - target_handicap)
            else:
                score = abs(avg1 - avg2)
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
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        login_time = session.get('admin_login_time')
        if login_time is None or time.time() - login_time > ADMIN_SESSION_TIMEOUT:
            session.clear()
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


CSS = ''':root { --bg: #0f1117; --surface: #1a1d27; --surface2: #242837; --border: #2d3148; --text: #e4e6f0;
  --text2: #9ea2b8; --accent: #6c5ce7; --accent2: #a29bfe; --green: #00b894; --red: #e17055; --gold: #fdcb6e; }
* { margin:0; padding:0; box-sizing:border-box; }
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height:100vh; }
.navbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 20px; display:flex; align-items:center; gap:8px; overflow-x:auto; }
.navbar .brand { font-weight:700; font-size:1.2em; color:var(--accent2); margin-right:20px; padding:16px 0; white-space:nowrap; }
.nav-link { color:var(--text2); text-decoration:none; padding:16px 12px; font-size:0.9em; white-space:nowrap; border-bottom:2px solid transparent; transition:all .2s; }
.nav-link:hover { color:var(--text); } .nav-link.active { color:var(--accent2); border-bottom-color:var(--accent2); }
.container { max-width:900px; margin:0 auto; padding:24px 16px; }
h1 { font-size:1.5em; margin-bottom:20px; color:var(--text); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:16px; }
table { width:100%; border-collapse:collapse; } th,td { padding:10px 14px; text-align:left; border-bottom:1px solid var(--border); }
th { color:var(--text2); font-size:0.85em; text-transform:uppercase; letter-spacing:0.5px; }
tr:hover { background:var(--surface2); }
.mmr { font-weight:700; color:var(--accent2); font-size:1.1em; }
.rank { color:var(--gold); font-weight:700; }
.na-text { color:var(--text2); opacity:0.5; font-style:italic; }
input,select { background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:10px 14px; border-radius:8px; font-size:1em; width:100%; margin-bottom:12px; }
input:focus,select:focus { outline:none; border-color:var(--accent); }
button,.btn { background:var(--accent); color:white; border:none; padding:10px 20px; border-radius:8px; font-size:1em; cursor:pointer; transition:background .2s; display:inline-block; text-decoration:none; }
button:hover,.btn:hover { background:var(--accent2); }
.btn-sm { padding:4px 10px; font-size:0.8em; border-radius:6px; }
.btn-green { background:var(--green); } .btn-red { background:var(--red); }
.btn-green:hover { background:#00d2a0; } .btn-red:hover { background:#e08060; }
.btn-outline { background:transparent; border:1px solid var(--border); color:var(--text2); }
.btn-outline:hover { border-color:var(--accent); color:var(--text); }
label { display:block; color:var(--text2); font-size:0.9em; margin-bottom:4px; }
.flash { padding:12px 16px; border-radius:8px; margin-bottom:16px; font-size:0.95em; }
.flash-success { background:rgba(0,184,148,0.15); color:var(--green); border:1px solid rgba(0,184,148,0.3); }
.flash-error { background:rgba(225,112,85,0.15); color:var(--red); border:1px solid rgba(225,112,85,0.3); }
.team-card { background:var(--surface2); border-radius:8px; padding:16px; flex:1; min-width:200px; }
.team-card h3 { margin-bottom:10px; } .teams-row { display:flex; gap:16px; flex-wrap:wrap; }
.vs { display:flex; align-items:center; font-size:1.5em; font-weight:700; color:var(--text2); padding:0 10px; }
.checkbox-grid { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }
.checkbox-grid label { display:flex; align-items:center; gap:6px; background:var(--surface2); padding:8px 12px; border-radius:6px; cursor:pointer; font-size:0.95em; color:var(--text); }
.checkbox-grid input { width:auto; margin:0; }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.8em; font-weight:600; }
.badge-pending { background:rgba(253,203,110,0.2); color:var(--gold); }
.badge-approved { background:rgba(0,184,148,0.2); color:var(--green); }
.badge-denied { background:rgba(225,112,85,0.2); color:var(--red); }
.win { color:var(--green); } .loss { color:var(--red); }
.actions { display:flex; gap:4px; }
.match-card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:0; margin-bottom:16px; overflow:hidden; transition:transform .15s, box-shadow .15s; }
.match-card:hover { transform:translateY(-2px); box-shadow:0 8px 24px rgba(0,0,0,0.3); }
.match-header { display:flex; justify-content:space-between; align-items:center; padding:14px 20px; border-bottom:1px solid var(--border); background:var(--surface2); }
.match-header .match-id { font-weight:700; color:var(--text2); font-size:0.9em; }
.match-header .match-date { color:var(--text2); font-size:0.82em; }
.match-body { display:flex; align-items:stretch; padding:20px; gap:0; }
.match-team { flex:1; padding:12px 16px; border-radius:10px; }
.match-team.winner { background:rgba(0,184,148,0.08); border:1px solid rgba(0,184,148,0.25); }
.match-team.loser { background:rgba(225,112,85,0.05); border:1px solid rgba(225,112,85,0.12); }
.match-team .team-label { font-size:0.75em; text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; font-weight:600; }
.match-team.winner .team-label { color:var(--green); }
.match-team.loser .team-label { color:var(--red); opacity:0.7; }
.match-team .team-winner-tag { display:inline-block; background:var(--green); color:#fff; font-size:0.7em; padding:2px 8px; border-radius:4px; margin-left:6px; font-weight:700; letter-spacing:0.5px; vertical-align:middle; }
.match-team .player-row { display:flex; justify-content:space-between; align-items:center; padding:5px 0; }
.match-team .player-name { font-weight:500; font-size:0.95em; }
.match-team.winner .player-name { color:var(--text); }
.match-team.loser .player-name { color:var(--text2); }
.match-team .player-mmr-change { font-weight:700; font-size:0.9em; }
.match-team .player-mmr-change.up { color:var(--green); }
.match-team .player-mmr-change.down { color:var(--red); }
.preview-grid { display:flex; gap:16px; flex-wrap:wrap; }
.preview-card { flex:1; min-width:220px; background:var(--surface2); border-radius:8px; padding:14px 16px; border:1px solid var(--border); }
.preview-card .preview-title { font-size:0.85em; text-transform:uppercase; letter-spacing:1px; color:var(--accent2); margin-bottom:10px; font-weight:700; }
.preview-section { margin-bottom:10px; }
.preview-section:last-child { margin-bottom:0; }
.preview-team-label { font-size:0.72em; color:var(--text2); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px; font-weight:600; }
.preview-row { display:flex; justify-content:space-between; align-items:center; padding:3px 0; font-size:0.92em; }
.preview-name { color:var(--text); }
.preview-change { font-weight:700; font-size:0.9em; }
.preview-change.up { color:var(--green); }
.preview-change.down { color:var(--red); }
.match-vs { display:flex; align-items:center; justify-content:center; padding:0 16px; flex-shrink:0; }
.match-vs span { font-size:1.3em; font-weight:800; color:var(--text2); opacity:0.4; letter-spacing:2px; }
.match-empty { text-align:center; padding:60px 20px; color:var(--text2); }
.match-empty .empty-icon { font-size:3em; margin-bottom:12px; opacity:0.3; }
.match-empty p { font-size:1.05em; }
.match-admin-actions { display:flex; gap:8px; padding:12px 20px; border-top:1px solid var(--border); background:var(--surface2); justify-content:flex-end; align-items:center; }
.match-admin-actions .admin-label { font-size:0.75em; text-transform:uppercase; letter-spacing:1px; color:var(--text2); margin-right:auto; font-weight:600; }
.btn-edit { background:var(--accent); } .btn-edit:hover { background:var(--accent2); }
.btn-delete { background:var(--red); } .btn-delete:hover { background:#e08060; }
@media(max-width:600px) { .container { padding:16px 8px; } .card { padding:16px; } th,td { padding:8px 6px; font-size:0.9em; }
  .match-body { flex-direction:column; gap:12px; padding:16px; }
  .match-vs { padding:4px 0; } .match-vs span { font-size:1em; }
  .match-team { padding:10px 12px; }
  .match-header { padding:10px 16px; }
  .match-admin-actions { padding:10px 16px; flex-wrap:wrap; } }'''

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

def page(title, content, nav_active=''):
    nav_items = [('/', 'Leaderboard', 'leaderboard'),
        ('/submit_match', 'Submit Match', 'match'), ('/balance', 'Team Balancer', 'balance'),
        ('/history', 'Match History', 'history'), ('/admin', 'Admin', 'admin')]
    nav_html = ''
    for href, label, key in nav_items:
        active = ' active' if key == nav_active else ''
        nav_html += f'<a href="{href}" class="nav-link{active}">{label}</a>'
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} - Empire Earth MMR</title><style>{CSS}</style></head><body>
<script>{RENAME_JS}</script>
<nav class="navbar"><span class="brand">Empire Earth MMR</span>{nav_html}</nav>
<div class="container">{content}</div></body></html>'''

def flash_html(msg, type='success'):
    return f'<div class="flash flash-{type}">{esc(msg)}</div>'


# ---------- ROUTES ----------
@app.route('/')
def leaderboard():
    all_players = query('SELECT * FROM players ORDER BY mmr DESC')
    # Separate active (played at least 1 game) from inactive (0 games)
    active = [p for p in all_players if p['wins'] + p['losses'] > 0]
    inactive = [p for p in all_players if p['wins'] + p['losses'] == 0]
    # Sort active by MMR desc, inactive by name
    active.sort(key=lambda p: p['mmr'], reverse=True)
    inactive.sort(key=lambda p: p['name'].lower())
    rows = ''
    for i, p in enumerate(active, 1):
        medal = ['&#129351;','&#129352;','&#129353;'][i-1] if i <= 3 else str(i)
        total = p['wins'] + p['losses']
        wr = f"{p['wins']/total*100:.0f}%"
        rows += f'<tr><td class="rank">{medal}</td><td><strong>{esc(p["name"])}</strong></td><td class="mmr">{p["mmr"]}</td><td class="win">{p["wins"]}</td><td class="loss">{p["losses"]}</td><td>{wr}</td></tr>'
    # Inactive players listed below with N/A for all stats
    for p in inactive:
        rows += f'<tr style="opacity:0.5"><td class="rank">-</td><td><strong>{esc(p["name"])}</strong></td><td class="na-text">N/A</td><td class="na-text">N/A</td><td class="na-text">N/A</td><td class="na-text">N/A</td></tr>'
    empty = '<p style="color:var(--text2);padding:20px;text-align:center">No players yet. Add some!</p>' if not all_players else ''
    content = f'<h1>Leaderboard</h1><div class="card"><table><tr><th>#</th><th>Player</th><th>MMR</th><th>W</th><th>L</th><th>WR</th></tr>{rows}</table>{empty}</div>'
    return page('Leaderboard', content, 'leaderboard')

@app.route('/rename_player', methods=['POST'])
@admin_required
def rename_player_route():
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    pid = request.form.get('player_id')
    new_name = request.form.get('new_name', '').strip()
    redirect_to = request.form.get('redirect', '/')
    if not pid or not new_name:
        return redirect(redirect_to)
    player = query('SELECT * FROM players WHERE id = ?', (int(pid),), one=True)
    if not player:
        return redirect(redirect_to)
    old_name = player['name']
    if old_name == new_name:
        return redirect(redirect_to)
    try:
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
    redirect_to = request.form.get('redirect', '/')
    if not pid:
        return redirect(redirect_to)
    try:
        query('DELETE FROM players WHERE id=?', (int(pid),), commit=True)
        logger.info(f'Player deleted: id={pid}')
    except Exception as e:
        logger.error(f'Delete failed: {e}')
    return redirect(redirect_to)

@app.route('/api/players/rename', methods=['POST'])
def api_rename_player():
    if not session.get('is_admin'):
        return jsonify({'error': 'Admin authentication required'}), 403
    data = request.get_json(silent=True) or {}
    player_id = data.get('player_id')
    new_name = data.get('new_name', '').strip()
    if not player_id or not new_name:
        return jsonify({'error': 'player_id and new_name are required'}), 400
    player = query('SELECT * FROM players WHERE id = ?', (int(player_id),), one=True)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    old_name = player['name']
    try:
        query('UPDATE players SET name=? WHERE id=?', (new_name, int(player_id)), commit=True)
        rename_player_in_matches(old_name, new_name)
        return jsonify({'success': True, 'old_name': old_name, 'new_name': new_name}), 200
    except Exception as e:
        logger.error(f'API error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/players/<name>', methods=['DELETE'])
def api_delete_player(name):
    if not session.get('is_admin'):
        return jsonify({'error': 'Admin authentication required'}), 403
    player = query('SELECT * FROM players WHERE name = ?', (name,), one=True)
    if not player:
        return jsonify({'error': 'Player not found'}), 404
    try:
        query('DELETE FROM players WHERE name=?', (name,), commit=True)
        return jsonify({'success': True, 'deleted': name}), 200
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
        elif not (name := request.form.get('name','').strip()):
            msg = flash_html('Please enter a name.', 'error')
        else:
            try:
                query('INSERT INTO players (name, mmr) VALUES (?, ?)', (name, DEFAULT_MMR), commit=True)
                msg = flash_html(f'Player {name} added with {DEFAULT_MMR} MMR!')
            except:
                msg = flash_html(f'Player already exists.', 'error')
    content = f'<h1>Add Player</h1>{msg}<div class="card"><form method="post">{csrf_field()}<label>Player Name</label><input name="name" placeholder="Enter player name" required><button type="submit">Add Player</button></form></div>'
    return page('Add Player', content, 'add')

@app.route('/submit_match', methods=['GET','POST'])
def submit_match():
    msg = ''
    players = query('SELECT * FROM players ORDER BY name')
    if request.method == 'POST':
      if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
      else:
        t1 = request.form.getlist('team1')
        t2 = request.form.getlist('team2')
        winner = request.form.get('winner')
        if not t1 or not t2:
            msg = flash_html('Both teams need at least one player.', 'error')
        elif set(t1) & set(t2):
            msg = flash_html('A player cannot be on both teams.', 'error')
        elif winner not in ('team1','team2'):
            msg = flash_html('Select a winner.', 'error')
        else:
            t1_names = [p['name'] for p in players if str(p['id']) in t1]
            t2_names = [p['name'] for p in players if str(p['id']) in t2]
            t1_players = [p for p in players if str(p['id']) in t1]
            t2_players = [p for p in players if str(p['id']) in t2]
            w_team = t1_players if winner == 'team1' else t2_players
            l_team = t2_players if winner == 'team1' else t1_players
            status = 'pending' if REQUIRE_MATCH_APPROVAL else 'approved'
            if status == 'approved':
                changes = apply_openskill_match(w_team, l_team, update_counts=True)
            else:
                # Preview only; real update happens on approval so ratings stay fresh.
                changes = preview_openskill_deltas(w_team, l_team)
            query('INSERT INTO matches (team1, team2, winner, mmr_changes, status) VALUES (?,?,?,?,?)',
                  (json.dumps(t1_names), json.dumps(t2_names), winner, json.dumps(changes), status), commit=True)
            summary = ', '.join(f"{n} {d}" for n, d in changes.items())
            if status == 'approved':
                msg = flash_html(f'Match recorded! MMR changes: {summary}')
            else:
                msg = flash_html(f'Match submitted for admin approval. Estimated changes: {summary}')
    checks1 = ''.join(f'<label><input type="checkbox" name="team1" value="{p["id"]}">{esc(p["name"])} ({p["mmr"]})</label>' for p in players)
    checks2 = ''.join(f'<label><input type="checkbox" name="team2" value="{p["id"]}">{esc(p["name"])} ({p["mmr"]})</label>' for p in players)
    content = f'<h1>Submit Match Result</h1>{msg}<div class="card"><form method="post">{csrf_field()}<label>Team 1</label><div class="checkbox-grid">{checks1}</div><label>Team 2</label><div class="checkbox-grid">{checks2}</div><label>Winner</label><select name="winner"><option value="team1">Team 1</option><option value="team2">Team 2</option></select><button type="submit">Submit Match</button></form></div>'
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
        if len(sel) < 2:
            result = flash_html('Select at least 2 players.', 'error')
        else:
            t1, t2, diff, handicapped = balance_teams([int(x) for x in sel])
            if t1 and t2:
                t1_html = ''.join(f'<div>{esc(p["name"])} <span class="mmr">({p["mmr"]})</span></div>' for p in t1)
                t2_html = ''.join(f'<div>{esc(p["name"])} <span class="mmr">({p["mmr"]})</span></div>' for p in t2)
                avg1, avg2 = team_avg_mmr(t1), team_avg_mmr(t2)
                if handicapped:
                    title = f'Balanced Teams (handicap applied: {len(t1)}v{len(t2)})'
                    handicap_note = f'<p style="color:var(--text2);font-size:0.85em;margin-top:10px">The {len(t1)}-player team is given a higher average MMR to compensate for the numbers disadvantage.</p>'
                else:
                    title = f'Balanced Teams (MMR diff: {diff:.0f})'
                    handicap_note = ''
                deltas_t1_wins = preview_openskill_deltas(t1, t2)
                deltas_t2_wins = preview_openskill_deltas(t2, t1)
                def _preview_row(p, deltas):
                    chg = deltas.get(p['name'], '')
                    arrow = '&#9650; ' if chg.startswith('+') else '&#9660; ' if chg.startswith('-') else ''
                    css = 'up' if chg.startswith('+') else 'down' if chg.startswith('-') else ''
                    return f'<div class="preview-row"><span class="preview-name">{esc(p["name"])}</span><span class="preview-change {css}">{arrow}{esc(chg)}</span></div>'
                def _preview_card(label, deltas):
                    rows1 = ''.join(_preview_row(p, deltas) for p in t1)
                    rows2 = ''.join(_preview_row(p, deltas) for p in t2)
                    return (f'<div class="preview-card"><div class="preview-title">{label}</div>'
                            f'<div class="preview-section"><div class="preview-team-label">Team 1</div>{rows1}</div>'
                            f'<div class="preview-section"><div class="preview-team-label">Team 2</div>{rows2}</div></div>')
                preview_html = ('<div class="card"><h3 style="margin-bottom:12px">Score Preview</h3>'
                                '<div class="preview-grid">'
                                + _preview_card('If Team 1 wins', deltas_t1_wins)
                                + _preview_card('If Team 2 wins', deltas_t2_wins)
                                + '</div></div>')
                result = f'<div class="card"><h3 style="margin-bottom:12px">{title}</h3><div class="teams-row"><div class="team-card"><h3 style="color:var(--accent2)">Team 1 ({len(t1)}) <span style="font-size:0.8em;color:var(--text2)">avg {avg1:.0f}</span></h3>{t1_html}</div><div class="vs">VS</div><div class="team-card"><h3 style="color:var(--gold)">Team 2 ({len(t2)}) <span style="font-size:0.8em;color:var(--text2)">avg {avg2:.0f}</span></h3>{t2_html}</div></div>{handicap_note}</div>{preview_html}'
    checks = ''.join(f'<label><input type="checkbox" name="players" value="{p["id"]}">{esc(p["name"])} ({p["mmr"]})</label>' for p in players)
    content = f'<h1>Team Balancer</h1><div class="card"><form method="post">{csrf_field()}<label>Select Players</label><div class="checkbox-grid">{checks}</div><button type="submit">Balance Teams</button></form></div>{result}'
    return page('Team Balancer', content, 'balance')

@app.route('/history')
def history():
    matches = query('SELECT * FROM matches ORDER BY id DESC')
    is_admin = session.get('is_admin', False)
    most_recent_id = matches[0]['id'] if matches else None
    cards = ''
    for m in matches:
        t1 = json.loads(m['team1'])
        t2 = json.loads(m['team2'])
        changes = json.loads(m['mmr_changes']) if m['mmr_changes'] else {}
        badge_cls = {'pending':'badge-pending','approved':'badge-approved','denied':'badge-denied'}.get(m['status'],'')
        is_t1_winner = m['winner'] == 'team1'
        # Format date
        date_str = ''
        if m.get('created_at'):
            try:
                dt = m['created_at']
                if isinstance(dt, str):
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S.%f'):
                        try:
                            dt = datetime.strptime(dt, fmt)
                            break
                        except:
                            continue
                if isinstance(dt, datetime):
                    date_str = dt.strftime('%b %d, %Y at %I:%M %p')
            except:
                date_str = str(m['created_at'])[:16]
        # Build player rows for each team
        def build_players(names, is_winner):
            html = ''
            for name in names:
                chg = changes.get(name, '')
                arrow = '&#9650; ' if chg.startswith('+') else '&#9660; ' if chg.startswith('-') else ''
                css = 'up' if chg.startswith('+') else 'down' if chg.startswith('-') else ''
                html += f'<div class="player-row"><span class="player-name">{esc(name)}</span><span class="player-mmr-change {css}">{arrow}{esc(chg)}</span></div>'
            return html
        w_class = 'winner' if is_t1_winner else 'loser'
        l_class = 'loser' if is_t1_winner else 'winner'
        w_tag = '<span class="team-winner-tag">WINNER</span>'
        t1_label = f'Team 1 {w_tag if is_t1_winner else ""}'
        t2_label = f'Team 2 {w_tag if not is_t1_winner else ""}'
        # Admin buttons: edit/delete for most recent, delete for all others
        admin_btns = ''
        if is_admin and m['status'] == 'approved':
            admin_btns = '<div class="match-admin-actions"><span class="admin-label">Admin</span>'
            if m['id'] == most_recent_id:
                admin_btns += '<a href="/admin/edit_last_match" class="btn btn-sm btn-edit">Edit Match</a>'
                admin_btns += f'<form method="post" action="/admin/delete_last_match" style="display:inline;margin:0">{csrf_field()}<button type="submit" class="btn btn-sm btn-delete" onclick="return confirm(\'Delete this match? MMR will be reversed.\')">Delete Match</button></form>'
            else:
                admin_btns += '<form method="post" action="/admin/delete_match/' + str(m["id"]) + '" style="margin:0">' + csrf_field() + '<button type="submit" class="btn btn-sm btn-delete" onclick="return confirm(\'Delete match #' + str(m["id"]) + '? All MMR will be recalculated from scratch. This cannot be undone.\')">Delete Match</button></form>'
            admin_btns += '</div>'
        elif is_admin and m['status'] == 'denied':
            admin_btns = '<div class="match-admin-actions"><span class="admin-label">Admin</span>'
            admin_btns += '<form method="post" action="/admin/delete_denied_match/' + str(m["id"]) + '" style="margin:0">' + csrf_field() + '<button type="submit" class="btn btn-sm btn-delete" onclick="return confirm(\'Delete denied match #' + str(m["id"]) + '?\')">Delete Match</button></form>'
            admin_btns += '</div>'
        cards += f'<div class="match-card">'
        cards += f'<div class="match-header"><div><span class="match-id">Match #{m["id"]}</span> <span class="badge {badge_cls}" style="margin-left:8px">{esc(m["status"])}</span></div><div class="match-date">{date_str}</div></div>'
        cards += f'<div class="match-body">'
        cards += f'<div class="match-team {w_class}"><div class="team-label">{t1_label}</div>{build_players(t1, is_t1_winner)}</div>'
        cards += f'<div class="match-vs"><span>VS</span></div>'
        cards += f'<div class="match-team {l_class}"><div class="team-label">{t2_label}</div>{build_players(t2, not is_t1_winner)}</div>'
        cards += f'</div>{admin_btns}</div>'
    if not matches:
        cards = '<div class="match-empty"><div class="empty-icon">&#9876;</div><p>No matches recorded yet.</p></div>'
    content = f'<h1>Match History</h1>{cards}'
    return page('Match History', content, 'history')


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
            msg = flash_html('Wrong password.', 'error')
    content = f'<h1>Admin Login</h1>{msg}<div class="card"><form method="post">{csrf_field()}<label>Admin Password</label><input type="password" name="password" placeholder="Enter password" required><button type="submit">Login</button></form></div>'
    return page('Admin', content, 'admin')

@app.route('/admin/panel')
@admin_required
def admin_panel():
    pending = query("SELECT * FROM matches WHERE status='pending' ORDER BY id DESC")
    players = query('SELECT * FROM players ORDER BY name')
    pending_html = ''
    for m in pending:
        t1 = json.loads(m['team1'])
        t2 = json.loads(m['team2'])
        changes = json.loads(m['mmr_changes']) if m['mmr_changes'] else {}
        winner_label = 'Team 1' if m['winner'] == 'team1' else 'Team 2'
        change_str = ', '.join(f'{esc(k)}: {esc(v)}' for k,v in changes.items())
        pending_html += f'<div class="card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px"><div><strong>Match #{m["id"]}</strong><br>{esc(", ".join(t1))} vs {esc(", ".join(t2))}<br>Winner: {winner_label} | Changes: {change_str}</div><div><form method="post" action="/admin/approve/{m["id"]}" style="display:inline;margin:0;margin-right:8px">{csrf_field()}<button type="submit" class="btn btn-green">Approve</button></form><form method="post" action="/admin/deny/{m["id"]}" style="display:inline;margin:0">{csrf_field()}<button type="submit" class="btn btn-red">Deny</button></form></div></div>'
    if not pending:
        pending_html = '<p style="color:var(--text2)">No pending matches.</p>'
    player_rows = ''
    for p in players:
        esc_name = p['name'].replace("'", "\\'")
        player_rows += f'<div class="card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">'
        player_rows += f'<div><strong>{esc(p["name"])}</strong> - MMR: <span class="mmr">{p["mmr"]}</span> | W:{p["wins"]} L:{p["losses"]}</div>'
        player_rows += f'<div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">'
        player_rows += f'<form method="post" action="/admin/set_mmr" style="display:flex;gap:4px;align-items:center;margin:0">{csrf_field()}'
        player_rows += f'<input type="hidden" name="player_id" value="{p["id"]}">'
        player_rows += f'<input name="mmr" type="number" value="{p["mmr"]}" style="width:80px;margin:0">'
        player_rows += f'<button type="submit" class="btn btn-sm">Set</button></form>'
        player_rows += f'<button class="btn btn-sm btn-outline" onclick="adminRenamePlayer({p["id"]}, \'{esc_name}\', \'{csrf_token()}\')">Rename</button>'
        player_rows += f'<form method="post" action="/admin/reset/{p["id"]}" style="display:inline;margin:0">{csrf_field()}<button type="submit" class="btn btn-sm btn-red">Reset</button></form>'
        player_rows += f'<button class="btn btn-sm btn-red" onclick="adminDeletePlayer({p["id"]}, \'{esc_name}\', \'{csrf_token()}\')">Delete</button>'
        player_rows += f'</div></div>'
    content = f'<h1>Admin Panel</h1><div style="margin-bottom:8px"><a href="/admin/logout" class="btn btn-red" style="font-size:0.85em">Logout</a> <a href="/add_player" class="btn" style="font-size:0.85em;margin-left:8px">+ Add Player</a> <form method="post" action="/admin/recalculate_mmr" style="display:inline;margin:0;margin-left:8px">{csrf_field()}<button type="submit" class="btn" style="font-size:0.85em" onclick="return confirm(\'Recalculate all MMR from scratch?\')">Recalculate MMR</button></form></div><h2 style="margin:20px 0 12px;font-size:1.2em">Pending Matches</h2>{pending_html}<h2 style="margin:20px 0 12px;font-size:1.2em">Manage Players</h2>{player_rows}'
    return page('Admin Panel', content, 'admin')

@app.route('/admin/approve/<int:match_id>', methods=['POST'])
@admin_required
def approve_match(match_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    m = query('SELECT * FROM matches WHERE id=? AND status=?', (match_id, 'pending'), one=True)
    if m:
        t1_names = json.loads(m['team1'])
        t2_names = json.loads(m['team2'])
        w_names = t1_names if m['winner'] == 'team1' else t2_names
        l_names = t2_names if m['winner'] == 'team1' else t1_names
        w_players = [query('SELECT * FROM players WHERE name=?', (n,), one=True) for n in w_names]
        l_players = [query('SELECT * FROM players WHERE name=?', (n,), one=True) for n in l_names]
        w_players = [p for p in w_players if p]
        l_players = [p for p in l_players if p]
        if w_players and l_players:
            # Recompute against CURRENT ratings (ignore stale preview).
            changes = apply_openskill_match(w_players, l_players, update_counts=True)
            query('UPDATE matches SET status=?, mmr_changes=? WHERE id=?',
                  ('approved', json.dumps(changes), match_id), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/deny/<int:match_id>', methods=['POST'])
@admin_required
def deny_match(match_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    query('UPDATE matches SET status=? WHERE id=?', ('denied', match_id), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete_last_match', methods=['POST'])
@admin_required
def delete_last_match():
    if not check_csrf():
        return redirect(url_for('history'))
    m = query("SELECT * FROM matches WHERE status='approved' ORDER BY id DESC LIMIT 1", one=True)
    if not m:
        return redirect(url_for('history'))
    query('DELETE FROM matches WHERE id=?', (m['id'],), commit=True)
    # OpenSkill updates aren't cleanly reversible, so replay from scratch.
    replayed = recalc_all_openskill()
    logger.info(f'Deleted last match #{m["id"]} and replayed {replayed} approved matches')
    return redirect(url_for('history'))


@app.route('/admin/delete_match/<int:match_id>', methods=['POST'])
@admin_required
def delete_match(match_id):
    """Delete any match by ID and recalculate all MMR from scratch."""
    if not check_csrf():
        return redirect(url_for('history'))
    m = query('SELECT * FROM matches WHERE id=?', (match_id,), one=True)
    if not m:
        return redirect(url_for('history'))
    # Delete the match
    query('DELETE FROM matches WHERE id=?', (match_id,), commit=True)
    logger.info(f'Deleted match #{match_id}, recalculating all MMR...')
    replayed = recalc_all_openskill()
    logger.info(f'MMR recalculated after deleting match #{match_id}, replayed {replayed} matches')
    return redirect(url_for('history'))

@app.route('/admin/delete_denied_match/<int:match_id>', methods=['POST'])
@admin_required
def delete_denied_match(match_id):
    """Delete a denied match. No MMR recalculation needed since denied matches don't affect ratings."""
    if not check_csrf():
        return redirect(url_for('history'))
    m = query('SELECT * FROM matches WHERE id=? AND status=?', (match_id, 'denied'), one=True)
    if not m:
        return redirect(url_for('history'))
    query('DELETE FROM matches WHERE id=?', (match_id,), commit=True)
    logger.info(f'Deleted denied match #{match_id}')
    return redirect(url_for('history'))

@app.route('/admin/edit_last_match', methods=['GET','POST'])
@admin_required
def edit_last_match():
    m = query("SELECT * FROM matches WHERE status='approved' ORDER BY id DESC LIMIT 1", one=True)
    if not m:
        return redirect(url_for('history'))
    players = query('SELECT * FROM players ORDER BY name')
    msg = ''
    if request.method == 'POST':
      if not check_csrf():
            msg = flash_html('Invalid request.', 'error')
      else:
        t1_ids = request.form.getlist('team1')
        t2_ids = request.form.getlist('team2')
        new_winner = request.form.get('winner')
        if not t1_ids or not t2_ids:
            msg = flash_html('Both teams need at least one player.', 'error')
        elif set(t1_ids) & set(t2_ids):
            msg = flash_html('A player cannot be on both teams.', 'error')
        elif new_winner not in ('team1','team2'):
            msg = flash_html('Select a winner.', 'error')
        else:
            t1_names = [p['name'] for p in players if str(p['id']) in t1_ids]
            t2_names = [p['name'] for p in players if str(p['id']) in t2_ids]
            # Update match row first, then replay all approved matches with OpenSkill.
            query('UPDATE matches SET team1=?, team2=?, winner=? WHERE id=?',
                  (json.dumps(t1_names), json.dumps(t2_names), new_winner, m['id']), commit=True)
            replayed = recalc_all_openskill()
            logger.info(f'Edited match #{m["id"]} and replayed {replayed} approved matches')
            return redirect(url_for('history'))
    old_t1 = json.loads(m['team1'])
    old_t2 = json.loads(m['team2'])
    old_winner = m['winner']
    checks1 = ''.join(f'<label><input type="checkbox" name="team1" value="{p["id"]}"{" checked" if p["name"] in old_t1 else ""}>{esc(p["name"])} ({p["mmr"]})</label>' for p in players)
    checks2 = ''.join(f'<label><input type="checkbox" name="team2" value="{p["id"]}"{" checked" if p["name"] in old_t2 else ""}>{esc(p["name"])} ({p["mmr"]})</label>' for p in players)
    sel1 = ' selected' if old_winner == 'team1' else ''
    sel2 = ' selected' if old_winner == 'team2' else ''
    content = f'<h1>Edit Match #{m["id"]}</h1>{msg}'
    content += f'<div class="card"><form method="post">{csrf_field()}'
    content += f'<label>Team 1</label><div class="checkbox-grid">{checks1}</div>'
    content += f'<label>Team 2</label><div class="checkbox-grid">{checks2}</div>'
    content += f'<label>Winner</label><select name="winner"><option value="team1"{sel1}>Team 1</option><option value="team2"{sel2}>Team 2</option></select>'
    content += f'<div style="display:flex;gap:8px;margin-top:8px"><button type="submit" class="btn">Save Changes</button><a href="/history" class="btn btn-outline">Cancel</a></div>'
    content += f'</form></div>'
    return page('Edit Match', content, 'history')

@app.route('/admin/set_mmr', methods=['POST'])
@admin_required
def set_mmr():
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    pid = request.form.get('player_id')
    mmr = request.form.get('mmr')
    if pid and mmr:
        query('UPDATE players SET mmr=? WHERE id=?', (int(mmr), int(pid)), commit=True)
    return redirect(url_for('admin_panel'))

@app.route('/admin/reset/<int:player_id>', methods=['POST'])
@admin_required
def reset_player(player_id):
    if not check_csrf():
        return redirect(url_for('admin_panel'))
    query('UPDATE players SET mmr=?, wins=0, losses=0 WHERE id=?', (DEFAULT_MMR, player_id), commit=True)
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
