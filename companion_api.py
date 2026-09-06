"""Authenticated companion submissions and their persistent audit receipts."""
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

MAX_REQUEST_BYTES = 32 * 1024
MAX_STAT = 1_000_000
MAX_PLAYERS = 10  # Neo Empire Earth supports ten-player matches.


class InvalidSubmission(ValueError):
    pass


def validate_submission(data, players=None):
    """Normalize untrusted input before looking up rosters or calculating ratings."""
    if not isinstance(data, dict):
        raise InvalidSubmission('The request must be a JSON object.')
    allowed = {'submission_id', 'team1', 'team2', 'winner', 'stats',
               'stats_confirmed', 'evidence'}
    if set(data) - allowed:
        raise InvalidSubmission('Unknown submission fields.')
    try:
        if not isinstance(data.get('submission_id'), str):
            raise ValueError()
        submission_id = str(uuid.UUID(data['submission_id']))
    except (ValueError, AttributeError):
        raise InvalidSubmission('submission_id must be a UUID.') from None
    rosters = [data.get('team1'), data.get('team2')]
    if any(not isinstance(team, list) or not team for team in rosters):
        raise InvalidSubmission('Both teams need at least one player.')
    ids = rosters[0] + rosters[1]
    if not 2 <= len(ids) <= MAX_PLAYERS:
        raise InvalidSubmission(f'A match must contain 2 to {MAX_PLAYERS} players.')
    if any(type(pid) is not int or not 0 < pid <= 2_147_483_647 for pid in ids):
        raise InvalidSubmission('Player IDs must be positive integers.')
    if len(set(ids)) != len(ids):
        raise InvalidSubmission('Each player may appear only once in a match.')
    known = {p['id'] for p in players} if players is not None else None
    if known is not None and not set(ids).issubset(known):
        raise InvalidSubmission('One or more players no longer exist. Refresh the player list.')
    if data.get('winner') not in ('team1', 'team2'):
        raise InvalidSubmission('winner must be team1 or team2.')
    stats = data.get('stats', [])
    if not isinstance(stats, list) or len(stats) > MAX_PLAYERS:
        raise InvalidSubmission('stats must be a list for all players, or an empty list.')
    confirmed = data.get('stats_confirmed', False)
    if type(confirmed) is not bool:
        raise InvalidSubmission('stats_confirmed must be a boolean.')
    stat_ids = []
    for row in stats:
        if not isinstance(row, dict) or set(row) != {'player_id', 'units_killed', 'units_lost'}:
            raise InvalidSubmission('Each stat row needs player_id, units_killed and units_lost.')
        if type(row['player_id']) is not int:
            raise InvalidSubmission('Stat player IDs must be integers.')
        stat_ids.append(row['player_id'])
        for key in ('units_killed', 'units_lost'):
            if type(row[key]) is not int or not 0 <= row[key] <= MAX_STAT:
                raise InvalidSubmission(f'{key} must be an integer from 0 to {MAX_STAT}.')
    if stats and (len(stat_ids) != len(ids) or set(stat_ids) != set(ids)):
        raise InvalidSubmission('Military stats must cover every player exactly once.')
    if stats and confirmed is not True:
        raise InvalidSubmission('Review the military stats and set stats_confirmed to true.')
    evidence = data.get('evidence')
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - {'source', 'sha256', 'captured_at', 'ocr_text'}:
            raise InvalidSubmission('Invalid evidence fields.')
        if evidence.get('source') not in ('screenshot', 'manual'):
            raise InvalidSubmission('Evidence source must be screenshot or manual.')
        timestamp = evidence.get('captured_at')
        try:
            if not isinstance(timestamp, str) or len(timestamp) > 64:
                raise ValueError()
            captured_at = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if captured_at.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise InvalidSubmission('captured_at must be an ISO timestamp with a timezone.') from None
        digest = evidence.get('sha256')
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest)):
            raise InvalidSubmission('sha256 must contain exactly 64 hexadecimal characters.')
        if evidence['source'] == 'screenshot' and not digest:
            raise InvalidSubmission('Screenshot evidence needs its sha256 hash.')
        ocr_text = evidence.get('ocr_text', '')
        if not isinstance(ocr_text, str) or len(ocr_text) > 12_000:
            raise InvalidSubmission('ocr_text must be at most 12000 characters.')
        evidence = {'source': evidence['source'],
                    'captured_at': captured_at.astimezone(timezone.utc).isoformat(),
                    'sha256': digest.lower() if digest else None, 'ocr_text': ocr_text}
    return {'submission_id': submission_id, 'team1': sorted(rosters[0]),
            'team2': sorted(rosters[1]), 'winner': data['winner'],
            'stats': sorted(stats, key=lambda row: row['player_id']),
            'stats_confirmed': confirmed, 'evidence': evidence}


def payload_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def register_companion(app, backend):
    api = Blueprint('companion_api', __name__, url_prefix='/api/v1')

    @api.before_request
    def authenticate():
        request.max_content_length = MAX_REQUEST_BYTES
        authorization = request.headers.get('Authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 256:
            return jsonify(error='A companion bearer token is required.'), 401
        raw = authorization[7:]
        if not raw or raw.strip() != raw:
            return jsonify(error='Invalid or revoked companion token.'), 401
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        token = backend.query('SELECT * FROM companion_tokens WHERE token_hash=? AND revoked_at IS NULL',
                              (digest,), one=True)
        if not token:
            return jsonify(error='Invalid or revoked companion token.'), 401
        g.companion_token_id = token['id']
        backend.query('UPDATE companion_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?',
                      (token['id'],), commit=True)

    @api.errorhandler(RequestEntityTooLarge)
    def too_large(error):
        return jsonify(error='Submission exceeds the 32 KB limit.'), 413

    @api.errorhandler(InvalidSubmission)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @api.errorhandler(BadRequest)
    @api.errorhandler(UnsupportedMediaType)
    def malformed(error):
        return jsonify(error='A valid application/json body is required.'), 400

    @api.get('/players')
    def players():
        return jsonify(players=backend.query('SELECT id, name, mmr FROM players ORDER BY name'))

    @api.get('/companion')
    def metadata():
        return jsonify(api_version=1, game='Empire Earth (original)', rating_version='military-v2',
                       approval_required=True, max_players=MAX_PLAYERS, max_stat=MAX_STAT,
                       screenshot_uploads=False, download_url=backend.companion_download_url())

    def parse_payload(data=None):
        data = request.get_json() if data is None else data
        players = backend.query('SELECT * FROM players ORDER BY id')
        payload = validate_submission(data, players)
        by_id = {p['id']: p for p in players}
        t1, t2 = [[by_id[pid] for pid in payload[key]] for key in ('team1', 'team2')]
        winners, losers = (t1, t2) if payload['winner'] == 'team1' else (t2, t1)
        return payload, t1, t2, winners, losers

    @api.post('/matches/preview')
    def preview():
        with backend.rating_transaction():
            payload, _, _, winners, losers = parse_payload()
            changes, details = backend.calculate_match(winners, losers, payload['stats'], 'military-v2')
        return jsonify(changes=changes, rating_details=details)

    @api.post('/matches')
    def submit():
        with backend.rating_transaction():
            payload = validate_submission(request.get_json())
            digest = payload_hash(payload)
            receipt = backend.query('SELECT * FROM companion_submissions WHERE submission_id=?',
                                    (payload['submission_id'],), one=True)
            if receipt:
                # A replacement credential can recover a timed-out submission. Keep the original
                # device in the audit receipt, while requiring the exact same UUID and payload.
                if receipt['payload_hash'] != digest:
                    return jsonify(error='This submission_id was already used with a different payload.'), 409
                match = backend.query('SELECT id, status FROM matches WHERE id=?', (receipt['match_id'],), one=True)
                if not match:
                    return jsonify(error='This submission was already processed and subsequently deleted.'), 409
                return jsonify(match_id=match['id'], status=match['status'], duplicate=True), 200
            screenshot_hash = (payload['evidence'] or {}).get('sha256')
            if screenshot_hash and backend.query('SELECT submission_id FROM companion_submissions WHERE screenshot_sha256=?',
                                                  (screenshot_hash,), one=True):
                return jsonify(error='This screenshot has already been submitted.'), 409
            payload, t1, t2, winners, losers = parse_payload(payload)
            changes, details = backend.calculate_match(winners, losers, payload['stats'], 'military-v2')
            match = backend.query('''INSERT INTO matches
                (team1,team2,winner,mmr_changes,status,rating_version,military_stats,evidence,rating_details,source)
                VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id''',
                (json.dumps([p['name'] for p in t1]), json.dumps([p['name'] for p in t2]), payload['winner'],
                 json.dumps(changes), 'pending', 'military-v2', json.dumps(payload['stats']),
                 json.dumps(payload['evidence']), json.dumps(details), 'companion'), one=True)
            backend.query('''INSERT INTO companion_submissions
                (submission_id,token_id,payload_hash,match_id,screenshot_sha256) VALUES (?,?,?,?,?)''',
                (payload['submission_id'], g.companion_token_id, digest, match['id'], screenshot_hash), commit=True)
        return jsonify(match_id=match['id'], status='pending', duplicate=False), 201

    app.register_blueprint(api)

    @app.route('/companion')
    def companion_guide():
        content = render_template('companion.html', api_base_url=request.url_root.rstrip('/'),
                                  download_url=backend.companion_download_url())
        return backend.page('Companion', content, 'companion')

    @app.route('/admin/companion', methods=['GET', 'POST'])
    @backend.admin_required
    def companion_admin():
        new_token = None
        error = None
        if request.method == 'POST':
            if not backend.check_csrf():
                error = 'Invalid request. Refresh the page and try again.'
            elif request.form.get('action') == 'issue':
                label = request.form.get('label', '').strip()
                if not label or len(label) > 80:
                    error = 'Enter a device label from 1 to 80 characters.'
                else:
                    new_token = 'eemmr_' + secrets.token_urlsafe(32)
                    digest = hashlib.sha256(new_token.encode('utf-8')).hexdigest()
                    backend.query('INSERT INTO companion_tokens (label,token_hash) VALUES (?,?)',
                                  (label, digest), commit=True)
            elif request.form.get('action') == 'revoke':
                token_id = request.form.get('token_id', '')
                if not token_id.isdigit():
                    error = 'Invalid device token.'
                else:
                    backend.query('UPDATE companion_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=? AND revoked_at IS NULL',
                                  (int(token_id),), commit=True)
            else:
                error = 'Unknown action.'
        tokens = backend.query('SELECT id,label,created_at,last_used_at,revoked_at FROM companion_tokens ORDER BY id DESC')
        content = render_template('companion_admin.html', tokens=tokens, new_token=new_token,
                                  error=error, csrf_token=backend.csrf_token())
        response = app.make_response(backend.page('Companion devices', content, 'admin'))
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response
