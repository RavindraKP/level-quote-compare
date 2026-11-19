# Deploying Level

Level is one Docker container: one Streamlit process, with the background worker thread inside it. SQLite and the original vendor files live on a **persistent disk mounted at `/var/level`**. Pick **one** host below (Render or Railway); both build from the `Dockerfile` in this repo.

Rules that matter for both hosts:

- **Exactly one instance.** Never scale to two: each process starts its own worker, and SQLite lives on one disk.
- **The disk must be mounted at `/var/level`.** Without it, every redeploy wipes the database.
- **Set `LEVEL_PASSCODE`.** Without it the app is open to anyone with the URL (the logs warn about this).
- The vendor reply links (`?t=...`) skip the passcode by design. They only open that vendor's own page.

## What is in the repo for this

| File | What it does |
|---|---|
| `Dockerfile` | Builds the image: Python 3.12, dependencies from `uv.lock`, starts `deploy/start.sh` |
| `deploy/start.sh` | Creates the schema if missing, works out the public URL for vendor links, starts Streamlit on `$PORT` |
| `.streamlit/config.toml` | Production Streamlit settings: headless, no file watcher, 50 MB upload limit, no "Deploy" button |
| `render.yaml` | Render Blueprint: one web service, Docker, 1 GB disk at `/var/level`, health check |
| `railway.json` | Railway config: Dockerfile build, one replica, health check (the volume is added by hand, see below) |
| `level/demo.py` | Save / restore / reload the demo data (also on the Overview page, under **Demo data**) |

## Environment variables

| Name | Required | Value |
|---|---|---|
| `LEVEL_ANTHROPIC_API_KEY` | yes | The app's own Claude API key. Mark it secret. |
| `LEVEL_PASSCODE` | yes | Any passcode you will type to enter the app. Mark it secret. |
| `LEVEL_MODEL` | no | Default `claude-haiku-5-5` |
| `LEVEL_VISION_MODEL` | no | Model for images; defaults to `LEVEL_MODEL` |
| `LEVEL_DB_PATH` | no | Already `/var/level/level.db` in the image |
| `LEVEL_UPLOAD_DIR` | no | Already `/var/level/uploads` in the image |
| `LEVEL_PUBLIC_URL` | no | Address used in vendor reply links. Filled in automatically on Render and Railway; set it only if you use a custom domain, e.g. `https://level.example.com` |
| `LEVEL_LINK_SECRET` | no | Signs vendor links. If unset, a random one is made once and kept on the disk |

Do **not** set `ANTHROPIC_API_KEY`; the app never reads it.

---

## Option A: Render

A persistent disk needs a paid instance type (Starter or above). Free instances have no disk.

1. In the Render dashboard, click **New → Blueprint**.
2. Pick the connected repository and the branch to deploy (`main`). Render finds `render.yaml` and shows one service, `level`, with a disk `level-data`.
3. Render asks for the two values marked secret. Enter:
   - `LEVEL_ANTHROPIC_API_KEY`: your key
   - `LEVEL_PASSCODE`: your passcode
4. Optional: change the region (the file says `singapore`) before you click **Apply** (or **Deploy Blueprint**).
5. Wait for the first build (about 3–5 minutes). The service page shows **Live** when the health check at `/_stcore/health` passes.
6. Open the URL shown at the top of the service page (`https://level-xxxx.onrender.com`). Continue at **First run** below.

If you prefer not to use a Blueprint: **New → Web Service**, choose the repo, Language **Docker**, Instance type **Starter**, add the two secret env vars, then under **Advanced** add a **Disk** with mount path `/var/level` and size 1 GB, and set the health check path to `/_stcore/health`.

## Option B: Railway

1. In the Railway dashboard, click **New Project → Deploy from GitHub repo** and pick the repository. Railway sees `railway.json` and builds the `Dockerfile`.
2. Open the service, then the **Variables** tab. Add:
   - `LEVEL_ANTHROPIC_API_KEY`: your key
   - `LEVEL_PASSCODE`: your passcode
   - `PORT`: `8501` (fixes the port, so the domain in step 4 points at the right one)
3. Add the disk: right-click the service on the project canvas (or press ⌘K / Ctrl+K and type "volume"), choose **Attach Volume**, and set the **mount path** to `/var/level`.
4. Open **Settings → Networking** and click **Generate Domain**. When it asks for the target port, enter `8501`.
5. Check **Settings → Deploy**: replicas must be **1** (already set in `railway.json`).
6. Click **Deploy** if the variable and volume changes have not triggered a redeploy. Wait for the deployment to show **Active** (health check `/_stcore/health`).
7. Open the generated `https://<name>.up.railway.app` address. Continue at **First run** below.

---

## First run (once, on the hosted app)

The disk starts empty. Load the demo event and read it once with the model, then keep that state:

1. Open the URL, enter the passcode. The Overview page says "No event yet".
2. Open **Demo data** under "Administration" at the bottom of the Overview page. Type `RESET` in the box ("Type RESET to enable these actions").
3. Click **Reload and read every reply**. This loads seed A and queues every reply. Go to **Inbox** and watch them read on the **Replies** tab (about 5–6 minutes in total; it uses a little API credit).
4. When every reply shows **Read in N min** (or **Read**) instead of a progress bar, go back to **Overview → Demo data**, type `RESET`, click **Save current data as the copy**.

From then on, before a demo or after a messy rehearsal: **Overview → Demo data → type `RESET` → Restore saved copy**. It takes a second and makes no model calls. The buttons refuse while a reply is still being read ("A job is still queued or running…").

The Review entry in the sidebar shows the number of open points, for example **Review (N open)**.

## Checks after deploying

1. **Full flow:** Inbox → Replies shows every reply as **Read in N min**; Compare shows the grid (switch **Price basis** between Price per unit, Delivered price and As quoted); Analyst answers a question; Award opens.
2. **Upload:** Inbox → **Add a reply** → pick the vendor, upload any quote file → **Save and read the reply** → it reads to **Read in N min**.
3. **Re-extraction:** Inbox → **Read again** on any read reply (or **Try again** on one that failed) → it runs again to **Read in N min**.
4. **Restart keeps data:** Render: **Manual Deploy → Restart service** (or Railway: **⋮ → Restart** on the deployment). Reopen the app: same replies, same grid.
5. **Cold start under 10 s:** after the restart, the passcode screen should appear within a few seconds.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Data gone after a redeploy | The disk is not mounted at `/var/level`. Check the disk / volume mount path. |
| "LEVEL_ANTHROPIC_API_KEY is not set" when reading a reply | Add the variable and redeploy. |
| Vendor reply links point to `localhost` | Set `LEVEL_PUBLIC_URL` to the app's public address. |
| A reply stays "queued" forever | More than one instance is running, or the service was scaled. Set instances / replicas to 1. |
| Upload rejected as too large | The limit is 50 MB per file (`.streamlit/config.toml`). |

## Run the same image on your own machine

```
docker build -t level .
docker run -p 8501:8501 --env-file .env -v level-data:/var/level level
```

Then open http://localhost:8501. The `level-data` volume plays the part of the host's disk.
