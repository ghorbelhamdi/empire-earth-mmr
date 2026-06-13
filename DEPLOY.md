# Free hosting with a persistent database

This app runs **100% free** and keeps all data (players, matches, MMR) across
restarts, redeploys, and sleep/wake cycles.

The trick: free web hosts give you an **ephemeral filesystem**, so the bundled
SQLite file (`empire.db`) is wiped every time the service restarts. The fix is
to store data in a **free, persistent Postgres database** instead. The app
already supports this — it switches to Postgres automatically whenever the
`DATABASE_URL` environment variable is set.

```
  ┌────────────────────────┐        ┌──────────────────────────┐
  │  Render (free web)     │  TLS   │  Neon (free Postgres)    │
  │  gunicorn + Flask app  │ ─────► │  persists across restarts │
  └────────────────────────┘        └──────────────────────────┘
```

---

## Step 1 — Create a free, persistent Postgres database (Neon)

[Neon](https://neon.tech) has a free tier that **persists data indefinitely**
(unlike Render's own free Postgres, which is deleted after 30 days).

1. Sign up at <https://neon.tech> (free, no card).
2. Create a project (any name, any region close to your web host).
3. On the project dashboard, copy the **connection string**. It looks like:

   ```
   postgresql://USER:PASSWORD@ep-xxxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require
   ```

   Use the **pooled** connection string if offered — it handles many
   short-lived connections better.

> Alternatives that also work: **Supabase** (free Postgres), **Aiven**, or any
> Postgres URL. Just paste its connection string as `DATABASE_URL` below.

---

## Step 2 — Deploy the web service (Render, free)

### Option A — Blueprint (one click)

1. Push this repo to GitHub.
2. Go to <https://dashboard.render.com> → **New** → **Blueprint** and select
   your repo. Render reads `render.yaml` automatically.
3. When prompted for the env vars marked "sync: false", fill in:
   - **`DATABASE_URL`** → the Neon connection string from Step 1.
   - **`ADMIN_PASSWORD`** → the SHA-256 hash of your admin password (see Step 3).
4. Click **Apply**. Done.

### Option B — Manual web service

1. Render → **New** → **Web Service** → connect your repo.
2. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 2 --preload --timeout 120 --bind 0.0.0.0:$PORT`
   - **Health check path:** `/health`
   - **Plan:** Free
3. Add environment variables:

   | Key                      | Value                                             |
   | ------------------------ | ------------------------------------------------- |
   | `DATABASE_URL`           | Neon connection string from Step 1                |
   | `ADMIN_PASSWORD`         | SHA-256 hash of your admin password (Step 3)      |
   | `SECRET_KEY`             | any long random string                            |
   | `REQUIRE_MATCH_APPROVAL` | `true`                                            |

4. Create the service. On first boot the app auto-creates its tables in Neon.

---

## Step 3 — Set the admin password

The app stores only the **SHA-256 hash** of the admin password. Generate it:

```bash
python -c "import hashlib; print(hashlib.sha256(b'YOUR_PASSWORD').hexdigest())"
```

Paste the output as the `ADMIN_PASSWORD` env var. Log in at `/admin`.

---

## Step 4 — Verify persistence

1. Open `https://<your-app>.onrender.com/health` — it should return:

   ```json
   { "status": "ok", "backend": "postgres", "player_count": 0 }
   ```

   `"backend": "postgres"` confirms data is going to the persistent database.
   If it says `"sqlite"`, `DATABASE_URL` isn't set — recheck Step 2.

2. Add a player, then in Render click **Manual Deploy → Clear build cache &
   deploy** (or just **Restart**). After it comes back, your player is still
   there. ✅

---

## Notes & gotchas

- **Free web service sleeps** after ~15 min of inactivity and takes a few
  seconds to wake on the next request. Your **data is not affected** — only the
  web process sleeps; the database is always on. To avoid cold starts you can
  ping `/health` periodically (e.g. a free [cron-job.org](https://cron-job.org)
  or [UptimeRobot](https://uptimerobot.com) monitor).
- **Local development** needs no setup: leave `DATABASE_URL` unset and the app
  falls back to the local `empire.db` SQLite file automatically.
- **Other free hosts** (Fly.io, Koyeb, Railway) work the same way — deploy the
  app and set `DATABASE_URL` to your Neon/Supabase string.
- **Never commit real secrets.** `DATABASE_URL` and `ADMIN_PASSWORD` are set in
  the host dashboard, not in the repo.
