# Empire Earth MMR

A Flask ladder for the original Empire Earth, with OpenSkill team ratings and a Windows companion that reads post-game Military screenshots.

## Run the ladder

Use Python 3.12 or later. Install `requirements.txt`, set `SECRET_KEY` to a long random secret and `ADMIN_PASSWORD` to the SHA-256 hash of the admin password, then run `python app.py`. The local server listens on port 3000 by default (`PORT` overrides it). Keep secrets in the environment, never in Git.

SQLite defaults to `empire.db` beside the app. Set `SQLITE_PATH` to use a separate database, particularly for development. `DATABASE_URL` selects PostgreSQL when it starts with `postgres`. The existing repository contains a historical SQLite file; do not overwrite a live database with it during deployment.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
$env:SQLITE_PATH = "$PWD/.local/development.sqlite"
New-Item -ItemType Directory -Force .local
.\.venv\Scripts\python app.py
```

Use Gunicorn behind an HTTPS reverse proxy in production. Existing deployments can retain their environment and database. Schema changes are additive on startup; existing ladder ratings are not recalculated as part of migration.

## Companion

The Windows companion lives in [`companion/`](companion/). It can watch the Empire Earth window, recognize the English Military screen using local OCR, import a screenshot, and accept corrected or manually entered statistics. A user maps every row to a ladder player and explicitly chooses teams and winner before previewing and submitting a match.

The ladder's `/companion` page provides setup instructions. Admins create and revoke device tokens at `/admin/companion`. The portable build is served from `static/downloads/Empire-Earth-Companion-0.1.0.exe`; build it using the instructions in the companion directory and copy the resulting executable there. Build artifacts are ignored by Git.

Screenshots remain on the user's PC. The API receives reviewed military counts and capture metadata, not the image. A hash detects byte-identical screenshots but is not proof of a genuine result. Every companion submission requires admin approval even when automatic approval is enabled for web reports.

## Ratings

OpenSkill Plackett–Luce remains the result-based model. MMR is the conservative estimate `(mu - 3 * sigma) * 40 + 1000`. Fresh players start at 1000. Uncertainty is part of this display, so MMR is not a fixed win/loss points schedule.

New matches use `military-v2`. When complete, confirmed stats are supplied, a smoothed logarithmic kills/losses score makes a bounded adjustment to each player's underlying skill change. Adjustments are centered within each team, sum to zero, and cannot exceed 10% of the player's base skill change. Single-player teams, equal contributions, and matches without stats are unchanged. See [`ratings.py`](ratings.py) for the exact formula and audit fields.

This military modifier is a conservative starting policy, not a calibrated skill model. Unit counts do not capture unit cost, economy, scouting, or support. Review real matches with the group before increasing its influence. Capture errors must be corrected before submission.

Existing matches keep `openskill-v1`. Match approval recalculates against current ratings, and replay uses each match's stored rating version and approved order. Team balancing uses the same model's win probabilities instead of comparing average displayed MMR.

## Verification

```powershell
.\.venv\Scripts\python -m pytest
cd companion
npm ci
npm test
npm run dist
```

Backend tests use isolated temporary databases. They cover malformed requests, authorization, approval, replay, duplicate submissions, and rating invariants. The companion's unit tests cover parsing and submission behavior. Test actual game capture on the players' PCs: legacy exclusive fullscreen can produce blank captures, and OCR accuracy depends on resolution and display language.

## Capture integration research

The [EEApi project](https://github.com/SoucupB/EEApi) targets Art of Conquest and documents unresolved online-play compatibility. The [Empire Earth Stats project](https://github.com/EE-modders/Empire-Earth-Stats) collects compatibility and playtime telemetry rather than post-match military results. Screenshot capture therefore provides a practical first integration for original Empire Earth without coupling the companion to game memory offsets.

Local `.ees` saves are also useful. The read-only [`save inspector`](tools/inspect_save.py) decodes the header, player/team metadata, and compressed-section boundaries. Optional decompression enables further counter research. See [`save format findings`](docs/save-format-research.md) for what is verified and what still needs a matching in-game reference. Unverified save counters never enter the rating API automatically.

## Existing-host deployment

[`tools/deploy_existing_host.sh`](tools/deploy_existing_host.sh) targets the existing `/opt/empire-mmr/app` deployment and `empire-mmr` service. Fetch a tested commit onto that host, then pass its full SHA to the script as root. It verifies the tracked database blob is unchanged between revisions, stops the service briefly, backs up SQLite consistently, switches code, restarts, and verifies health and preservation of all old player/match fields. A failed check rolls code back without automatically replacing the live database. The Windows download is transferred separately into `static/downloads/`; it is not stored in Git.
