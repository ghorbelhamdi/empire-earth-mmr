"""Standalone migration checks using only disposable SQLite databases."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tools.recalculate_ratings import apply_database, fingerprint, read_snapshot, review_database


class RatingMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eemmr-migration-test-")
        self.database = Path(self.tmp.name) / "ladder.sqlite"
        self.backup = Path(self.tmp.name) / "before.sqlite"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE players (
                    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    mmr INTEGER, wins INTEGER, losses INTEGER, mu REAL, sigma REAL,
                    created_at TEXT, note TEXT);
                CREATE TABLE matches (
                    id INTEGER PRIMARY KEY, team1 TEXT, team2 TEXT, winner TEXT,
                    mmr_changes TEXT, status TEXT, created_at TEXT, rating_version TEXT,
                    military_stats TEXT, evidence TEXT, rating_details TEXT,
                    source TEXT, rated_order INTEGER, note TEXT);
                CREATE TABLE companion_tokens(id INTEGER PRIMARY KEY,label TEXT,token_hash TEXT);
                CREATE TABLE companion_submissions(submission_id TEXT PRIMARY KEY,token_id INTEGER,
                    payload_hash TEXT,match_id INTEGER,screenshot_sha256 TEXT);
                INSERT INTO companion_tokens VALUES(7,'test device','fixture-token-hash');
                INSERT INTO companion_submissions VALUES('test-receipt',7,'fixture-payload-hash',2,'fixture-screenshot-hash');
            """)
            for i, name in enumerate(("Alice", "Bob", "Carol", "Dave", "Inactive"), 1):
                db.execute("INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?)",
                           (i, name, 1230 + i, 10, 11, 33.0, 4.0, "old date", "keep player"))
            stats = [{"player_id": i, "units_killed": k, "units_lost": l}
                     for i, k, l in ((1, 20, 4), (2, 3, 7), (3, 8, 4), (4, 2, 9))]
            # Approval order intentionally differs from ID order.
            for match_id, order, status, winner in ((1, 2, "approved", "team2"),
                                                    (2, 1, "approved", "team1"),
                                                    (3, None, "pending", "team1"),
                                                    (4, None, "denied", "invalid-preserved-winner")):
                db.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (match_id, json.dumps(["Alice", "Bob"]), json.dumps(["Carol", "Dave"]),
                            winner, '{"Alice":"+55","Dave":"-56"}', status, "old date", "military-v2",
                            json.dumps(stats) if match_id == 1 else None,
                            '{"source":"screenshot","ocr_text":"keep original"}',
                            '{"old":"audit"}', "companion", order, "keep match"))

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def db(self, path=None):
        connection = sqlite3.connect(path or self.database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def snapshot(self, path=None):
        with self.db(path) as db:
            db.row_factory = sqlite3.Row
            return read_snapshot(db)

    def other_rows(self):
        with self.db() as db:
            return [list(db.execute(f"SELECT * FROM {table}"))
                    for table in ("companion_tokens", "companion_submissions")]

    def test_dry_run_is_read_only_and_replays_approval_order(self):
        original = self.database.read_bytes()
        before = self.snapshot()
        plan = review_database(self.database)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(plan["applied"])
        self.assertFalse(self.backup.exists())
        self.assertEqual(plan["input_sha256"], fingerprint(before))
        self.assertEqual([row["id"] for row in plan["matches"]], [2, 1, 3])
        self.assertEqual(plan["counts"], {"players": 5, "approved": 2, "pending": 1, "denied_unchanged": 1})
        self.assertEqual(plan["totals"]["after_mmr"], 5000)
        first = plan["matches"][0]
        self.assertEqual(first["new_mmr_changes"], {"Alice": "+24", "Bob": "+24", "Carol": "-24", "Dave": "-24"})
        second = plan["matches"][1]["new_rating_details"]["players"]
        self.assertEqual(second["Alice"]["old_mmr"], 1024)
        self.assertEqual(second["Carol"]["old_mmr"], 976)
        self.assertNotIn("fixture-token-hash", json.dumps(plan))

    def test_apply_changes_only_rating_fields_and_refreshes_pending_without_counts(self):
        before, others = self.snapshot(), self.other_rows()
        plan = review_database(self.database)
        result = apply_database(self.database, plan["input_sha256"], self.backup)
        after = self.snapshot()
        self.assertTrue(result["applied"])
        self.assertEqual(self.snapshot(self.backup), before)
        with self.db(self.backup) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.other_rows(), others)
        self.assertEqual(fingerprint(after), plan["output_sha256"])
        for old, new in zip(before["players"], after["players"]):
            self.assertEqual({key: value for key, value in old.items() if key not in {"mu", "sigma", "mmr", "wins", "losses"}},
                             {key: value for key, value in new.items() if key not in {"mu", "sigma", "mmr", "wins", "losses"}})
        for old, new in zip(before["matches"], after["matches"]):
            if old["status"] == "denied":
                self.assertEqual(old, new)
            else:
                self.assertEqual(new["rating_version"], "team-mmr-v3")
                self.assertEqual({key: value for key, value in old.items() if key not in {"rating_version", "mmr_changes", "rating_details"}},
                                 {key: value for key, value in new.items() if key not in {"rating_version", "mmr_changes", "rating_details"}})
                self.assertEqual(sum(int(v) for v in json.loads(new["mmr_changes"]).values()), 0)
        self.assertEqual(sum(row["mmr"] for row in after["players"]), 5000)
        self.assertEqual(sum(row["wins"] for row in after["players"]), 4)
        self.assertEqual(sum(row["losses"] for row in after["players"]), 4)
        inactive = after["players"][-1]
        self.assertEqual((inactive["mmr"], inactive["wins"], inactive["losses"]), (1000, 0, 0))
        pending = json.loads(after["matches"][2]["rating_details"])["players"]
        for row in after["players"][:4]:
            self.assertEqual(pending[row["name"]]["old_mmr"], row["mmr"])

    def test_changed_source_sha_is_rejected_before_backup_or_writes(self):
        plan = review_database(self.database)
        with self.db() as db:
            db.execute("UPDATE matches SET evidence='new evidence' WHERE id=3")
        changed = self.snapshot()
        with self.assertRaisesRegex(ValueError, "SHA changed"):
            apply_database(self.database, plan["input_sha256"], self.backup)
        self.assertEqual(self.snapshot(), changed)
        self.assertFalse(self.backup.exists())

    def test_backup_is_never_overwritten(self):
        plan, before = review_database(self.database), self.snapshot()
        self.backup.write_bytes(b"existing backup")
        with self.assertRaisesRegex(ValueError, "Backup already exists"):
            apply_database(self.database, plan["input_sha256"], self.backup)
        self.assertEqual(self.backup.read_bytes(), b"existing backup")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_rosters_winner_and_stats_fail_without_writes(self):
        original = self.snapshot()
        bad_cases = [
            ("team1", '["Missing"]'), ("team1", '["Alice","Alice"]'),
            ("team2", '["Alice","Carol"]'), ("team1", "[]"), ("team1", "broken json"),
            ("winner", "draw"), ("rated_order", None),
            ("military_stats", '[{"player_id":999,"units_killed":1,"units_lost":2}]'),
            ("military_stats", '[{"player_id":1,"units_killed":1,"units_lost":2}]'),
            ("military_stats", '[{"player_id":1,"units_killed":1,"units_lost":2,"extra":0}]'),
            ("military_stats", '[{"player_id":true,"units_killed":1,"units_lost":2}]'),
            ("military_stats", '{"Alice":{"units_killed":1,"units_lost":2}}'),
            ("military_stats", '[{"player_id":1,"units_killed":1,"units_lost":2},{"player_id":1,"units_killed":1,"units_lost":2}]'),
        ]
        for column, value in bad_cases:
            with self.subTest(column=column, value=value):
                with self.db() as db:
                    db.execute(f"UPDATE matches SET {column}=? WHERE id=1", (value,))
                invalid = self.snapshot()
                with self.assertRaises(ValueError):
                    review_database(self.database)
                with self.assertRaises(ValueError):
                    apply_database(self.database, fingerprint(invalid), self.backup)
                self.assertEqual(self.snapshot(), invalid)
                self.assertFalse(self.backup.exists())
                with self.db() as db:
                    db.execute(f"UPDATE matches SET {column}=? WHERE id=1", (original["matches"][0][column],))

    def test_mid_write_failure_rolls_back_and_keeps_valid_backup(self):
        with self.db() as db:
            db.execute("""CREATE TRIGGER reject_bob BEFORE UPDATE ON players WHEN old.name='Bob'
                          BEGIN SELECT RAISE(ABORT,'fixture update failure'); END""")
        before, plan = self.snapshot(), review_database(self.database)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture update failure"):
            apply_database(self.database, plan["input_sha256"], self.backup)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.snapshot(self.backup), before)

    def test_backup_includes_committed_wal_data_while_another_connection_is_open(self):
        with self.db() as keeper:
            self.assertEqual(keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            keeper.execute("UPDATE players SET note='committed in WAL' WHERE id=1")
            keeper.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            before, plan = self.snapshot(), review_database(self.database)
            apply_database(self.database, plan["input_sha256"], self.backup)
            self.assertEqual(self.snapshot(self.backup), before)
            self.assertEqual(self.snapshot()["players"][0]["note"], "committed in WAL")

    def test_unrelated_trigger_write_is_detected_and_rolled_back(self):
        with self.db() as db:
            db.execute("""CREATE TRIGGER touch_token AFTER UPDATE ON players
                          BEGIN UPDATE companion_tokens SET label='unexpected'; END""")
        before, others, plan = self.snapshot(), self.other_rows(), review_database(self.database)
        with self.assertRaisesRegex(ValueError, "unrelated table changed"):
            apply_database(self.database, plan["input_sha256"], self.backup)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.other_rows(), others)

    def test_replay_is_idempotent_with_separate_review_and_backup(self):
        plan = review_database(self.database)
        apply_database(self.database, plan["input_sha256"], self.backup)
        first = self.snapshot()
        second_plan = review_database(self.database)
        self.assertEqual(second_plan["input_sha256"], second_plan["output_sha256"])
        second_backup = self.backup.with_name("before-second.sqlite")
        apply_database(self.database, second_plan["input_sha256"], second_backup)
        self.assertEqual(self.snapshot(), first)
        self.assertEqual(self.snapshot(second_backup), first)

    def test_paths_require_absolute_and_sqlite_sidecars_are_protected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            review_database(Path("relative.sqlite"))
        plan = review_database(self.database)
        with self.assertRaisesRegex(ValueError, "absolute"):
            apply_database(self.database, plan["input_sha256"], Path("relative-backup.sqlite"))
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.assertRaises(ValueError):
                apply_database(self.database, plan["input_sha256"], Path(str(self.database) + suffix))

    def test_cli_default_is_read_only_and_apply_requires_review_fields(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "recalculate_ratings.py"
        before = self.database.read_bytes()
        result = subprocess.run([sys.executable, str(script), "--database", str(self.database)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["applied"])
        self.assertEqual(self.database.read_bytes(), before)
        result = subprocess.run([sys.executable, str(script), "--database", str(self.database), "--apply"],
                                capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--expect-sha", result.stderr)
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
