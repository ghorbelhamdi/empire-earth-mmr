"""Review and transactionally migrate a SQLite ladder to team-mmr-v3.

This command imports only the pure rating module, never the Flask application.
It defaults to a read-only replay. Applying requires the reviewed input digest
and an unused, absolute backup filename.
"""

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ratings import CURRENT_VERSION, MAX_STAT_COUNT, OS_DEFAULT_MU, OS_DEFAULT_SIGMA, rate_match


TARGET_VERSION = "team-mmr-v3"
PLAYER_FIELDS = ("mu", "sigma", "mmr", "wins", "losses")
MATCH_FIELDS = ("rating_version", "mmr_changes", "rating_details")
PLAYER_COLUMNS = {"id", "name", *PLAYER_FIELDS}
MATCH_COLUMNS = {"id", "team1", "team2", "winner", "status", "rated_order",
                 "military_stats", *MATCH_FIELDS}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _absolute(path, label):
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise ValueError(f"{label} must be an absolute path.")
    return value.resolve()


def _connect(database, readonly=True):
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(database.as_uri() + f"?mode={mode}", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    return connection


def read_snapshot(connection):
    snapshot = {}
    for table, required in (("players", PLAYER_COLUMNS), ("matches", MATCH_COLUMNS)):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if required - columns:
            raise ValueError(f"{table} is missing required columns: {', '.join(sorted(required - columns))}.")
        snapshot[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
    return snapshot


def fingerprint(snapshot):
    """Hash all player/match columns, including preserved evidence and status."""
    return hashlib.sha256(_json(snapshot).encode("utf-8")).hexdigest()


def _preserved_tables(connection):
    # Compare other tables in memory only; token material never enters the plan.
    tables = list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    data = []
    for name, schema in tables:
        if name in ("players", "matches"):
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        rows = [list(row) for row in connection.execute(f"SELECT * FROM {quoted}")]
        # SQLite BLOB values in unrelated tables are hashed without exposing them.
        encoded = [json.dumps(row, default=lambda value: {"blob_hex": value.hex()},
                              sort_keys=True, separators=(",", ":")) for row in rows]
        data.append((name, schema, sorted(encoded)))
    return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def _decode(value, label):
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} contains invalid JSON.") from exc


def _roster(match, players):
    label = f"Match {match['id']}"
    if match["winner"] not in ("team1", "team2"):
        raise ValueError(f"{label} has an invalid winner.")
    teams = [_decode(match[field], f"{label} {field}") for field in ("team1", "team2")]
    for team in teams:
        if not isinstance(team, list) or not team or any(not isinstance(name, str) or not name.strip() for name in team):
            raise ValueError(f"{label} needs two nonempty lists of canonical player names.")
    names = teams[0] + teams[1]
    if len(names) != len(set(names)):
        raise ValueError(f"{label} contains duplicate players.")
    if len(names) > 10:
        raise ValueError(f"{label} exceeds the ten-player roster limit.")
    if set(names) - set(players):
        raise ValueError(f"{label} references missing players: {', '.join(sorted(set(names) - set(players)))}.")
    winner_index = 0 if match["winner"] == "team1" else 1
    return teams, teams[winner_index], teams[1 - winner_index]


def _stats(match, roster, players):
    if match["military_stats"] is None:
        return None
    label = f"Match {match['id']} military statistics"
    rows = _decode(match["military_stats"], label)
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list of player-ID records.")
    if not rows:
        return None
    by_id = {players[name]["id"]: name for name in roster}
    output = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"player_id", "units_killed", "units_lost"}:
            raise ValueError(f"{label} has missing or unknown fields.")
        player_id = row["player_id"]
        if type(player_id) is not int or player_id not in by_id:
            raise ValueError(f"{label} references an unknown roster player ID.")
        name = by_id[player_id]
        if name in output:
            raise ValueError(f"{label} contains a duplicate player ID.")
        for field in ("units_killed", "units_lost"):
            if type(row[field]) is not int or not 0 <= row[field] <= MAX_STAT_COUNT:
                raise ValueError(f"{label} has an invalid {field} count.")
        output[name] = {field: row[field] for field in ("units_killed", "units_lost")}
    if set(output) != set(roster):
        raise ValueError(f"{label} does not cover the entire roster.")
    return output


def _old_changes(value):
    try:
        return json.loads(value) if value is not None else None
    except (ValueError, TypeError):
        return value  # Old display text is review information, never replay input.


def build_plan(snapshot):
    if CURRENT_VERSION != TARGET_VERSION:
        raise ValueError(f"Migration requires {TARGET_VERSION}; installed formula is {CURRENT_VERSION}.")
    source_players = snapshot["players"]
    players, ids = {}, set()
    for row in source_players:
        name, player_id = row["name"], row["id"]
        if not isinstance(name, str) or not name.strip() or name in players:
            raise ValueError("Player table has a missing or duplicate canonical name.")
        if type(player_id) is not int or player_id <= 0 or player_id in ids:
            raise ValueError("Player table has an invalid or duplicate ID.")
        ids.add(player_id)
        players[name] = {"id": player_id, "name": name, "mu": OS_DEFAULT_MU,
                         "sigma": OS_DEFAULT_SIGMA, "mmr": 1000, "wins": 0, "losses": 0}
    matches = snapshot["matches"]
    for match in matches:
        if match["status"] not in ("approved", "pending", "denied"):
            raise ValueError(f"Match {match['id']} has an unknown status.")
        if match["status"] == "approved" and (type(match["rated_order"]) is not int or match["rated_order"] <= 0):
            raise ValueError(f"Approved match {match['id']} needs an explicit positive approval order.")
    approved = sorted((m for m in matches if m["status"] == "approved"), key=lambda m: (m["rated_order"], m["id"]))
    pending = sorted((m for m in matches if m["status"] == "pending"), key=lambda m: m["id"])
    reports, updates = [], {}
    participation_w = participation_l = 0
    for match in approved + pending:
        teams, winners, losers = _roster(match, players)
        stats = _stats(match, winners + losers, players)
        before = {name: dict(players[name]) for name in winners + losers}
        try:
            new_w, new_l, details = rate_match([dict(before[name]) for name in winners],
                                              [dict(before[name]) for name in losers],
                                              stats=stats, version=CURRENT_VERSION)
        except Exception as exc:
            raise ValueError(f"Match {match['id']} could not be rated: {exc}") from exc
        changed = new_w + new_l
        names = winners + losers
        if len(changed) != len(names) or {r.name for r in changed} != set(names) or set(details["players"]) != set(names):
            raise ValueError(f"Match {match['id']} returned an inconsistent rating roster.")
        changes = {}
        for rating in changed:
            new_mmr = details["players"][rating.name]["new_mmr"]
            if type(new_mmr) is not int or not math.isfinite(rating.mu) or not math.isfinite(rating.sigma) or rating.sigma <= 0:
                raise ValueError(f"Match {match['id']} returned invalid ratings.")
            delta = new_mmr - before[rating.name]["mmr"]
            if (rating.name in winners and delta < 0) or (rating.name in losers and delta > 0):
                raise ValueError(f"Match {match['id']} reversed a public rating result.")
            changes[rating.name] = f"{delta:+d}"
            if match["status"] == "approved":
                players[rating.name].update(mu=rating.mu, sigma=rating.sigma, mmr=new_mmr)
                players[rating.name]["wins" if rating.name in winners else "losses"] += 1
        if sum(int(delta) for delta in changes.values()) != 0:
            raise ValueError(f"Match {match['id']} did not conserve public MMR.")
        if match["status"] == "approved":
            participation_w += len(winners)
            participation_l += len(losers)
        updates[match["id"]] = {"rating_version": CURRENT_VERSION,
                                 "mmr_changes": _json(changes), "rating_details": _json(details)}
        reports.append({"id": match["id"], "status": match["status"], "rated_order": match["rated_order"],
                        "team1": teams[0], "team2": teams[1], "winner": match["winner"],
                        "old_rating_version": match["rating_version"], "new_rating_version": CURRENT_VERSION,
                        "old_mmr_changes": _old_changes(match["mmr_changes"]), "new_mmr_changes": changes,
                        "new_rating_details": details})
    after_total = sum(row["mmr"] for row in players.values())
    if after_total != 1000 * len(players):
        raise ValueError("Replay did not conserve total public MMR.")
    if sum(row["wins"] for row in players.values()) != participation_w or sum(row["losses"] for row in players.values()) != participation_l:
        raise ValueError("Replay win/loss counts do not match approved participation.")
    roster = [{"id": row["id"], "name": row["name"],
               "before": {field: row[field] for field in PLAYER_FIELDS},
               "after": {field: players[row["name"]][field] for field in PLAYER_FIELDS}} for row in source_players]
    plan = {"plan_version": 1, "formula_version": CURRENT_VERSION, "input_sha256": fingerprint(snapshot),
            "applied": False, "counts": {"players": len(players), "approved": len(approved),
            "pending": len(pending), "denied_unchanged": sum(m["status"] == "denied" for m in matches)},
            "totals": {"before_mmr": sum(row["mmr"] for row in source_players), "after_mmr": after_total,
                       "expected_mmr": 1000 * len(players), "wins": participation_w, "losses": participation_l},
            "players": roster, "matches": reports}
    expected = copy.deepcopy(snapshot)
    for row in expected["players"]:
        row.update({field: players[row["name"]][field] for field in PLAYER_FIELDS})
    for row in expected["matches"]:
        row.update(updates.get(row["id"], {}))
    plan["output_sha256"] = fingerprint(expected)
    return plan, expected


def review_database(database):
    database = _absolute(database, "Database")
    connection = _connect(database)
    try:
        connection.execute("BEGIN")
        plan, _ = build_plan(read_snapshot(connection))
        plan["database"] = str(database)
        return plan
    finally:
        connection.close()


def _backup(database, backup, expected_sha):
    # Reserve exclusively so an existing backup, including a symlink, is never overwritten.
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    source, destination = _connect(database), sqlite3.connect(backup)
    try:
        # The separate reader avoids backup() deadlocking on our write connection.
        # BEGIN IMMEDIATE on that connection prevents any intervening writer.
        source.backup(destination)
        if [row[0] for row in destination.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The new backup failed SQLite integrity_check.")
        destination.row_factory = sqlite3.Row
        if fingerprint(read_snapshot(destination)) != expected_sha:
            raise ValueError("Backup content does not match the reviewed input snapshot.")
    finally:
        destination.close()
        source.close()


def apply_database(database, expect_sha, backup):
    database = _absolute(database, "Database")
    unresolved_backup = Path(backup).expanduser()
    if not unresolved_backup.is_absolute():
        raise ValueError("Backup must be an absolute path.")
    # Check before resolve() too: a dangling symlink is still an existing backup name.
    if os.path.lexists(unresolved_backup):
        raise ValueError("Backup already exists; choose a new absolute filename.")
    backup = unresolved_backup.resolve()
    if not isinstance(expect_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expect_sha):
        raise ValueError("Expected SHA must be a reviewed 64-character SHA256 digest.")
    if backup == database or str(backup) in {str(database) + suffix for suffix in ("-wal", "-shm", "-journal")}:
        raise ValueError("Backup must be separate from the database and its SQLite sidecars.")
    if os.path.lexists(backup):
        raise ValueError("Backup already exists; choose a new absolute filename.")
    if any(os.path.lexists(str(backup) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Backup SQLite sidecars already exist; choose a new backup filename.")
    connection = _connect(database, readonly=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = read_snapshot(connection)
        if fingerprint(snapshot) != expect_sha.lower():
            raise ValueError("Input SHA changed since review. Generate and review a fresh plan.")
        plan, expected = build_plan(snapshot)
        preserved = _preserved_tables(connection)
        _backup(database, backup, plan["input_sha256"])
        for row in expected["players"]:
            connection.execute("UPDATE players SET mu=?,sigma=?,mmr=?,wins=?,losses=? WHERE id=?",
                               tuple(row[field] for field in PLAYER_FIELDS) + (row["id"],))
        for row in expected["matches"]:
            if row["status"] in ("approved", "pending"):
                connection.execute("UPDATE matches SET rating_version=?,mmr_changes=?,rating_details=? WHERE id=?",
                                   tuple(row[field] for field in MATCH_FIELDS) + (row["id"],))
        actual = read_snapshot(connection)
        if actual != expected or fingerprint(actual) != plan["output_sha256"]:
            raise ValueError("Post-write verification failed; rolling back.")
        if _preserved_tables(connection) != preserved:
            raise ValueError("An unrelated table changed; rolling back.")
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("Database integrity_check failed; rolling back.")
        connection.commit()
        plan.update(applied=True, database=str(database), backup=str(backup))
        return plan
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Absolute SQLite database path")
    parser.add_argument("--output", help="New JSON review/result file; defaults to stdout")
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed replay transactionally")
    parser.add_argument("--expect-sha", help="input_sha256 from the reviewed dry-run plan")
    parser.add_argument("--backup", help="Unused absolute backup filename, required with --apply")
    args = parser.parse_args(argv)
    if args.apply and (not args.expect_sha or not args.backup):
        parser.error("--apply requires --expect-sha and --backup")
    if not args.apply and (args.expect_sha or args.backup):
        parser.error("--expect-sha and --backup are only valid with --apply")
    output = None
    applied = False
    try:
        database = _absolute(args.database, "Database")
        if args.output:
            requested_output = Path(args.output).expanduser()
            if os.path.lexists(requested_output):
                raise ValueError("Output already exists; choose a new JSON filename.")
            path = requested_output.resolve()
            protected = {str(database) + suffix for suffix in ("", "-wal", "-shm", "-journal")}
            if args.backup:
                protected.update(str(_absolute(args.backup, "Backup")) + suffix for suffix in ("", "-wal", "-shm", "-journal"))
            if str(path) in protected:
                raise ValueError("Output must be separate from the database, backup, and SQLite sidecars.")
            output = path.open("x", encoding="utf-8")
        plan = apply_database(database, args.expect_sha, args.backup) if args.apply else review_database(database)
        applied = plan["applied"]
        rendered = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if output:
            output.write(rendered)
            output.flush()
            print(f"{'Applied' if applied else 'Dry run'}: {args.output}; input_sha256={plan['input_sha256']}")
        else:
            print(rendered, end="")
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        prefix = "Migration committed, but result output failed" if applied else "Migration not applied"
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 1
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
