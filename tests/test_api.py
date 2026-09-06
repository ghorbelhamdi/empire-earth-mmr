"""API, audit/replay and transactional integration checks, always on disposable SQLite."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_import_db = tempfile.TemporaryDirectory(prefix='eemmr-test-import-')
os.environ['DATABASE_URL'] = ''
os.environ['SQLITE_PATH'] = str(Path(_import_db.name) / 'import.db')
import app as backend


class CompanionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eemmr-api-test-')
        backend.SQLITE_PATH = str(Path(self.tmp.name) / 'test.db')
        backend.use_postgres = False
        backend.app.config.update(TESTING=True, SECRET_KEY='test-session-key')
        self.client = backend.app.test_client()
        self.raw_token = 'eemmr_test_device_token'
        self.headers = {'Authorization': 'Bearer ' + self.raw_token}
        with backend.app.app_context():
            backend.init_db()
            for name in ('Alice', 'Bob', 'Carol', 'Dave'):
                backend.query('INSERT INTO players(name) VALUES (?)', (name,), commit=True)
            backend.query('INSERT INTO companion_tokens(label,token_hash) VALUES (?,?)',
                ('Test device', hashlib.sha256(self.raw_token.encode()).hexdigest()), commit=True)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, table, order='id'):
        with backend.app.app_context():
            return backend.query(f'SELECT * FROM {table} ORDER BY {order}')

    def payload(self, **overrides):
        value = {'submission_id': str(uuid.uuid4()), 'team1': [1, 2], 'team2': [3, 4],
                 'winner': 'team1', 'stats_confirmed': True,
                 'stats': [dict(player_id=pid, units_killed=kills, units_lost=losses)
                           for pid, kills, losses in ((1, 136, 55), (2, 91, 55), (3, 93, 175), (4, 16, 52))],
                 'evidence': {'source': 'screenshot', 'sha256': uuid.uuid4().hex * 2,
                              'captured_at': '2026-09-05T12:00:00+02:00', 'ocr_text': '<script>untrusted OCR</script>'}}
        value.update(overrides)
        return value

    def post(self, payload, path='/api/v1/matches', client=None, headers=None):
        return (client or self.client).post(path, json=payload, headers=headers or self.headers)

    def admin(self, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            session.update(is_admin=True, admin_login_time=time.time(), csrf_token='test-csrf')
        return client

    def approve(self, match_id, client=None):
        return self.admin(client).post(f'/admin/approve/{match_id}', data={'csrf_token': 'test-csrf'})

    def submit(self, payload=None):
        response = self.post(payload or self.payload())
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.json['match_id']

    def test_api_requires_per_device_bearer(self):
        for path in ('/api/v1/players', '/api/v1/companion'):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer bad'}).status_code, 401)
        result = self.client.get('/api/v1/players', headers=self.headers)
        self.assertEqual(set(result.json['players'][0]), {'id', 'name', 'mmr'})
        self.assertEqual(len(result.json['players']), 4)
        metadata = self.client.get('/api/v1/companion', headers=self.headers).json
        self.assertTrue(metadata['approval_required'])
        self.assertFalse(metadata['screenshot_uploads'])

    def test_token_issue_hash_revoke_and_csrf(self):
        self.assertEqual(self.client.post('/admin/companion', data={'action': 'issue', 'label': 'pc'}).status_code, 302)
        self.admin()
        response = self.client.post('/admin/companion', data={'action': 'issue', 'label': 'pc'})
        self.assertEqual(len(self.rows('companion_tokens')), 1)
        response = self.client.post('/admin/companion', data={'action': 'issue', 'label': 'pc', 'csrf_token': 'test-csrf'})
        raw = re.search(r'eemmr_[A-Za-z0-9_-]{43}', response.get_data(as_text=True)).group()
        token = self.rows('companion_tokens')[-1]
        self.assertEqual(token['token_hash'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw, json.dumps(token))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        headers = {'Authorization': 'Bearer ' + raw}
        self.assertEqual(self.client.get('/api/v1/players', headers=headers).status_code, 200)
        self.client.post('/admin/companion', data={'action': 'revoke', 'token_id': token['id'], 'csrf_token': 'test-csrf'})
        self.assertEqual(self.client.get('/api/v1/players', headers=headers).status_code, 401)

    def test_pending_even_when_web_approval_disabled_and_preview_no_writes(self):
        before = self.rows('players')
        payload = self.payload()
        preview = self.post(payload, '/api/v1/matches/preview')
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertEqual(set(preview.json['changes']), {'Alice', 'Bob', 'Carol', 'Dave'})
        self.assertEqual(preview.json['rating_details']['version'], backend.CURRENT_VERSION)
        with patch.object(backend, 'REQUIRE_MATCH_APPROVAL', False):
            response = self.post(payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['status'], 'pending')
        self.assertEqual(self.rows('players'), before)
        self.assertEqual(json.loads(self.rows('matches')[0]['mmr_changes']), preview.json['changes'])

    def test_complete_stat_validation_and_bad_rosters(self):
        cases = [dict(team1=[]), dict(team1='1'), dict(team1=[True]), dict(team1=['1']),
                 dict(team1=[1, 1]), dict(team1=[1, 3]), dict(team1=[999]), dict(team1=[1.0]),
                 dict(winner='draw'), dict(submission_id='invalid'), dict(extra='ignored?'),
                 dict(stats_confirmed=False), dict(stats_confirmed='true'), dict(stats=None),
                 dict(stats=[{'player_id': 1, 'units_killed': 10, 'units_lost': 2}]),
                 dict(evidence={'source': 'screenshot', 'captured_at': '2026-09-05T00:00:00Z'}),
                 dict(evidence={'source': 'manual', 'captured_at': '2026-09-05T00:00:00'}),
                 dict(evidence={'source': 'memory', 'captured_at': '2026-09-05T00:00:00Z'})]
        for fields in cases:
            with self.subTest(fields=fields):
                response = self.post(self.payload(**fields))
                self.assertEqual(response.status_code, 400, response.json)
                self.assertIn('error', response.json)
        for value in (True, -1, 0.5, '5', 1_000_001):
            with self.subTest(stat=value):
                payload = self.payload()
                payload['stats'][0]['units_killed'] = value
                self.assertEqual(self.post(payload).status_code, 400)
        payload = self.payload()
        payload['stats'][1]['player_id'] = 1
        self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.rows('matches'), [])

    def test_no_stats_is_valid(self):
        payload = self.payload(stats=[], stats_confirmed=False, evidence=None)
        match_id = self.submit(payload)
        self.approve(match_id)
        self.assertFalse(json.loads(self.rows('matches')[0]['rating_details'])['performance_applied'])

    def test_neo_nine_and_ten_player_matches_and_boundary(self):
        with backend.app.app_context():
            for index in range(5, 12):
                backend.query('INSERT INTO players(name) VALUES (?)', (f'Player {index}',), commit=True)
        for size in (9, 10):
            with self.subTest(size=size):
                payload = self.payload(team1=list(range(1, 6)), team2=list(range(6, size + 1)),
                    stats=[{'player_id': pid, 'units_killed': pid * 3, 'units_lost': pid} for pid in range(1, size + 1)])
                self.assertEqual(self.post(payload).status_code, 201)
                self.admin().post('/submit_match', data={'csrf_token': 'test-csrf', 'team1': payload['team1'],
                    'team2': payload['team2'], 'winner': 'team1'})
                response = self.client.post('/balance', data={'csrf_token': 'test-csrf', 'players': list(range(1, size + 1))})
                self.assertIn('Suggested split', response.get_data(as_text=True))
        self.assertEqual(len(self.rows('matches')), 4)
        excessive = self.payload(team1=list(range(1, 6)), team2=list(range(6, 12)), stats=[], stats_confirmed=False)
        self.assertEqual(self.post(excessive).status_code, 400)
        self.admin().post('/submit_match', data={'csrf_token': 'test-csrf', 'team1': excessive['team1'],
            'team2': excessive['team2'], 'winner': 'team1'})
        self.assertEqual(len(self.rows('matches')), 4)

    def test_json_types_and_request_limit(self):
        for value in ([], True, None, 'text'):
            response = self.post(value)
            self.assertEqual(response.status_code, 400)
            self.assertIn('error', response.json)
        response = self.client.post('/api/v1/matches', data='{broken', content_type='application/json', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/v1/matches', data='x' * 33000, content_type='application/json', headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIn('error', response.json)

    def test_idempotent_retry_and_changed_payload_conflict(self):
        payload = self.payload()
        first = self.post(payload)
        retry = self.post(payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json['duplicate'])
        self.assertEqual(retry.json['match_id'], first.json['match_id'])
        changed = dict(payload, winner='team2')
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(len(self.rows('matches')), 1)
        self.approve(first.json['match_id'])
        self.assertEqual(self.post(payload).json['status'], 'approved')

    def test_screenshot_deduplicates_across_devices(self):
        payload = self.payload()
        self.submit(payload)
        second = 'eemmr_other_device'
        with backend.app.app_context():
            backend.query('INSERT INTO companion_tokens(label,token_hash) VALUES (?,?)',
                ('Other', hashlib.sha256(second.encode()).hexdigest()), commit=True)
        headers = {'Authorization': 'Bearer ' + second}
        retry = self.post(payload, headers=headers)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json['duplicate'])
        payload['submission_id'] = str(uuid.uuid4())
        response = self.post(payload, headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn('screenshot', response.json['error'])

    def test_replacement_token_recovers_exact_retry_after_original_revoked(self):
        payload = self.payload()
        match_id = self.submit(payload)
        replacement = 'eemmr_replacement_token'
        with backend.app.app_context():
            backend.query('UPDATE companion_tokens SET revoked_at=CURRENT_TIMESTAMP WHERE id=1', commit=True)
            backend.query('INSERT INTO companion_tokens(label,token_hash) VALUES (?,?)',
                ('Replacement', hashlib.sha256(replacement.encode()).hexdigest()), commit=True)
        self.assertEqual(self.post(payload).status_code, 401)
        headers = {'Authorization': 'Bearer ' + replacement}
        recovered = self.post(payload, headers=headers)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json, {'match_id': match_id, 'status': 'pending', 'duplicate': True})
        self.assertEqual(self.post(dict(payload, winner='team2'), headers=headers).status_code, 409)
        self.assertEqual(self.rows('companion_submissions', 'submission_id')[0]['token_id'], 1)
        self.assertEqual(len(self.rows('matches')), 1)

    def test_deleted_receipt_cannot_resurrect_match(self):
        payload = self.payload()
        match_id = self.submit(payload)
        self.admin().post(f'/admin/delete_match/{match_id}', data={'csrf_token': 'test-csrf'})
        self.assertEqual(self.post(payload).status_code, 409)
        self.assertEqual(self.rows('matches'), [])
        self.assertIsNone(self.rows('companion_submissions', 'submission_id')[0]['match_id'])

    def test_approval_exactly_once_and_denial_cannot_unapprove(self):
        payload = self.payload()
        preview = self.post(payload, '/api/v1/matches/preview').json
        match_id = self.submit(payload)
        self.assertEqual(self.approve(match_id).status_code, 302)
        once = self.rows('players')
        match = self.rows('matches')[0]
        self.assertEqual(json.loads(match['mmr_changes']), preview['changes'])
        self.assertEqual(json.loads(match['rating_details']), preview['rating_details'])
        self.approve(match_id)
        self.admin().post(f'/admin/deny/{match_id}', data={'csrf_token': 'test-csrf'})
        self.assertEqual(self.rows('players'), once)
        self.assertEqual(self.rows('matches')[0]['status'], 'approved')
        self.assertEqual([p['wins'] + p['losses'] for p in once], [1] * 4)

    def test_denied_match_cannot_be_approved(self):
        match_id = self.submit()
        before = self.rows('players')
        self.admin().post(f'/admin/deny/{match_id}', data={'csrf_token': 'test-csrf'})
        self.approve(match_id)
        self.assertEqual(self.rows('players'), before)
        self.assertEqual(self.rows('matches')[0]['status'], 'denied')

    def test_replay_preserves_formula_and_approval_order(self):
        first = self.submit()
        second = self.submit(self.payload(winner='team2'))
        with backend.app.app_context():
            backend.query("UPDATE matches SET rating_version='openskill-v1',military_stats=NULL WHERE id=?", (second,), commit=True)
        with patch.object(backend, 'CURRENT_VERSION', 'openskill-v1'):
            self.approve(second)
        self.approve(first)
        before_players, before_matches = self.rows('players'), self.rows('matches')
        self.assertLess(before_matches[1]['rated_order'], before_matches[0]['rated_order'])
        with backend.app.app_context():
            self.assertEqual(backend.recalc_all_openskill(), 2)
        self.assertEqual(self.rows('players'), before_players)
        self.assertEqual(self.rows('matches'), before_matches)

    def test_history_last_actions_target_latest_approval_even_with_pending_reports(self):
        first = self.submit()
        second = self.submit()
        self.approve(second)
        self.approve(first)
        self.submit()  # A newer pending report must not hide the last approved actions.
        text = self.admin().get('/history').get_data(as_text=True)
        cards = re.split(r'<div class="match(?: pending)?">', text)[1:]
        last_card = next(card for card in cards if f'<span class="match-id">#{first}</span>' in card)
        other_card = next(card for card in cards if f'<span class="match-id">#{second}</span>' in card)
        self.assertIn('/admin/edit_last_match', last_card)
        self.assertIn('/admin/delete_last_match', last_card)
        self.assertNotIn('/admin/edit_last_match', other_card)
        self.assertIn(f'/admin/delete_match/{second}', other_card)

    def test_failed_approval_rolls_back_all_players_and_status(self):
        match_id = self.submit()
        before = self.rows('players')
        original_query = backend.query

        def fail_status(sql, *args, **kwargs):
            if sql.startswith('UPDATE matches SET status='):
                raise RuntimeError('simulated storage failure')
            return original_query(sql, *args, **kwargs)

        with patch.object(backend, 'query', fail_status):
            with self.assertRaisesRegex(RuntimeError, 'storage failure'):
                self.approve(match_id)
        self.assertEqual(self.rows('players'), before)
        self.assertEqual(self.rows('matches')[0]['status'], 'pending')

    def test_concurrent_approval_applies_once(self):
        match_id = self.submit()
        clients = [self.admin(backend.app.test_client()) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda client: client.post(f'/admin/approve/{match_id}', data={'csrf_token': 'test-csrf'}).status_code, clients))
        self.assertEqual(results, [302, 302])
        self.assertEqual([p['wins'] + p['losses'] for p in self.rows('players')], [1] * 4)

    def test_concurrent_submission_has_one_receipt(self):
        payload = self.payload()
        clients = [backend.app.test_client(), backend.app.test_client()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda client: self.post(payload, client=client).status_code, clients))
        self.assertEqual(sorted(codes), [200, 201])
        self.assertEqual(len(self.rows('matches')), 1)
        self.assertEqual(len(self.rows('companion_submissions', 'submission_id')), 1)

    def test_rename_preserves_stats_and_replay(self):
        payload = self.payload()
        match_id = self.submit(payload)
        self.approve(match_id)
        self.admin().post('/rename_player', data={'csrf_token': 'test-csrf', 'player_id': '1', 'new_name': 'Alice renamed'})
        match = self.rows('matches')[0]
        self.assertIn('Alice renamed', json.loads(match['rating_details'])['players'])
        self.assertEqual(json.loads(match['military_stats']), payload['stats'])
        players = self.rows('players')
        with backend.app.app_context():
            backend.recalc_all_openskill()
        self.assertEqual(self.rows('players'), players)
        self.assertEqual(self.post(payload).status_code, 200)

    def test_roster_edit_clears_stats_and_evidence(self):
        match_id = self.submit()
        self.approve(match_id)
        response = self.admin().post('/admin/edit_last_match', data={'csrf_token': 'test-csrf', 'match_id': str(match_id),
            'team1': ['1', '3'], 'team2': ['2', '4'], 'winner': 'team1'})
        self.assertEqual(response.status_code, 302)
        match = self.rows('matches')[0]
        self.assertIsNone(match['military_stats'])
        self.assertIsNone(match['evidence'])
        self.assertFalse(json.loads(match['rating_details'])['stats_present'])

    def test_winner_only_edit_keeps_stats(self):
        match_id = self.submit()
        self.approve(match_id)
        original = self.rows('matches')[0]
        self.admin().post('/admin/edit_last_match', data={'csrf_token': 'test-csrf', 'match_id': str(match_id),
            'team1': ['2', '1'], 'team2': ['3', '4'], 'winner': 'team2'})
        match = self.rows('matches')[0]
        self.assertEqual(match['military_stats'], original['military_stats'])
        self.assertEqual(match['team1'], original['team1'])
        self.assertEqual(match['winner'], 'team2')

    def test_player_with_history_cannot_be_deleted(self):
        self.submit()
        response = self.admin().post('/delete_player', data={'csrf_token': 'test-csrf', 'player_id': '1'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.rows('players')), 4)

    def test_existing_json_mutations_require_csrf_and_unexpired_session(self):
        self.admin()
        self.assertEqual(self.client.post('/api/players/rename', json={'player_id': 1, 'new_name': 'New'}).status_code, 403)
        self.assertEqual(self.client.delete('/api/players/Alice').status_code, 403)
        with self.client.session_transaction() as session:
            session['admin_login_time'] = time.time() - 3600
        response = self.client.post('/api/players/rename', json={'player_id': 1, 'new_name': 'New'}, headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(response.status_code, 403)

    def test_set_public_mmr_preserves_model_and_reset_resets_both(self):
        original = self.rows('players')[0]
        self.admin().post('/admin/set_mmr', data={'csrf_token': 'test-csrf', 'player_id': '1', 'mmr': '1550'})
        player = self.rows('players')[0]
        self.assertEqual(player['mmr'], 1550)
        self.assertEqual((player['mu'], player['sigma']), (original['mu'], original['sigma']))
        self.admin().post('/admin/reset/1', data={'csrf_token': 'test-csrf'})
        player = self.rows('players')[0]
        self.assertEqual((player['mmr'], player['mu'], player['sigma']), (1000, 25.0, 25.0 / 3.0))

    def test_public_points_preview_approval_and_legacy_pending_upgrade(self):
        with backend.app.app_context():
            backend.query('UPDATE players SET mmr=1450,mu=31,sigma=7 WHERE id=1', commit=True)
            backend.query('UPDATE players SET mmr=950,mu=24,sigma=3 WHERE id=2', commit=True)
        payload = self.payload(stats=[], stats_confirmed=False, evidence=None)
        preview = self.post(payload, '/api/v1/matches/preview').json
        self.assertEqual(preview['changes']['Alice'], preview['changes']['Bob'])
        match_id = self.submit(payload)
        with backend.app.app_context():
            backend.query("UPDATE matches SET rating_version='military-v2' WHERE id=?", (match_id,), commit=True)
        self.approve(match_id)
        match = self.rows('matches')[0]
        self.assertEqual(match['rating_version'], backend.CURRENT_VERSION)
        self.assertEqual(json.loads(match['mmr_changes']), preview['changes'])
        for player in self.rows('players'):
            audit = preview['rating_details']['players'][player['name']]
            self.assertEqual(player['mmr'], audit['new_mmr'])
            self.assertEqual(player['mmr'] - audit['old_mmr'], audit['final_mmr_delta'])
        history = self.client.get('/history').get_data(as_text=True)
        self.assertIn('Result', history)
        self.assertIn('Military', history)

    def test_web_rosters_reject_unknown_duplicate_and_invalid_ids(self):
        self.admin()
        for roster in (['999'], ['1', '1'], ['abc'], ['1', '3']):
            response = self.client.post('/submit_match', data={'csrf_token': 'test-csrf', 'team1': roster, 'team2': ['3', '4'], 'winner': 'team1'})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows('matches'), [])

    def test_stats_are_escaped_and_displayed_in_review_and_history(self):
        self.submit()
        self.admin()
        for path in ('/history', '/admin/panel'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Military stats', response.get_data(as_text=True))
            self.assertIn('136', response.get_data(as_text=True))
            self.assertNotIn('<script>untrusted OCR</script>', response.get_data(as_text=True))

    def test_migration_preserves_legacy_player_ratings(self):
        with backend.app.app_context():
            backend.query('UPDATE players SET mmr=1999,mu=40,sigma=4 WHERE id=1', commit=True)
            backend.query("INSERT INTO matches(team1,team2,winner,status,mmr_changes) VALUES (?,?,?,?,?)",
                ('["Alice"]', '["Bob"]', 'team1', 'approved', '{"Alice":"+15"}'), commit=True)
            before = backend.query('SELECT * FROM players ORDER BY id')
            backend._migrate_companion_columns()
            self.assertEqual(backend.query('SELECT * FROM players ORDER BY id'), before)
            match = backend.query('SELECT * FROM matches', one=True)
            self.assertEqual(match['rating_version'], 'openskill-v1')
            self.assertEqual(match['mmr_changes'], '{"Alice":"+15"}')


if __name__ == '__main__':
    unittest.main()
