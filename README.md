# JWA Meta Tracker

A Jurassic World Alive **meta tier list** built from the teams of the top 100 arena
players. It shows which creatures top players put on their teams, sorts them into
S / A / B / C / D tiers by how often they are used, and keeps a history so you can see
what is rising or falling, plus each creature's best teammates and the builds top players
use. It checks for new data automatically every 6 hours (midnight, 6 am, noon and 6 pm
US Central time).

You can use it in two ways (they share the same code):

* **On your own Windows PC** - double-click `START.bat`. Private, keeps its own history.
* **As a free public website** - GitHub hosts it and updates it for you, so anyone
  can open it on a phone or computer. See *Put it online* below.

---

## On your PC: quick start

1. **Double-click `START.bat`.**
2. Your browser opens the dashboard at `http://127.0.0.1:8765/`.
   (The first time, it also sets up automatic updates and downloads the existing
   history; the page fills itself in after about a minute.)
3. Keep the black START window open while you look at the dashboard; close it when you
   are done. **Automatic updates keep running even when it is closed.**

| File | What it does |
|---|---|
| `START.bat` | Opens the dashboard. First run also sets everything up. |
| `UPDATE_NOW.bat` | Checks for new data immediately (optional). |
| `VIEW_UPDATE_LOG.bat` | Shows whether updates work and opens the detailed log. |
| `REMOVE_AUTO_UPDATE.bat` | Turns automatic updates off (your data is kept). |
| `PUBLISH_WEBSITE.bat` | Uploads this folder to your GitHub repository (see *Put it online*). |

Needs Windows 10/11 and Python 3.10 or newer; if Python is missing, `START.bat` offers
to install it. The PC version checks for new data every 6 hours (midnight, 6 am, noon
and 6 pm, in the computer's own time). The PC must be on and
signed in for that; if it was off, the check happens shortly after you turn it back on,
and missed snapshots are still downloaded.

---

## Share it with friends

**Easiest:** put it online (next section) and send people the website link.

**Or send the program itself:** zip this folder (or send them the download link of
your GitHub copy). They unzip it anywhere and double-click `START.bat`. Their copy keeps
its own history in their own user folder - nothing of yours is included or shared.

---

## Put it online as a free website (GitHub Pages)

Costs nothing: GitHub hosts the website and checks for new data every 6 hours on its
own computers, so it keeps updating even when your PC is off.

1. Create a free account at <https://github.com> (choose a username you are happy to
   have in the website address, e.g. `https://yourname.github.io/jwa-meta-tracker/`).
2. Click **+ → New repository**, name it `jwa-meta-tracker`, choose **Public**, leave
   everything else empty, and click **Create repository**.
3. In that repository: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. Double-click **`PUBLISH_WEBSITE.bat`** in this folder and paste the repository address
   (e.g. `https://github.com/yourname/jwa-meta-tracker`). GitHub will ask you to sign in
   in a browser window. After 1-2 minutes the site is live at
   `https://<your-username>.github.io/jwa-meta-tracker/`.

Run `PUBLISH_WEBSITE.bat` again any time you change files; the website rebuilds itself.

After that it checks for new data at midnight, 6 am, noon and 6 pm US Central time
(GitHub may start a few minutes late) and keeps a growing history in the `archive/`
folder of the repository. If the data source is down, the
site keeps showing the last good data and says that the update failed.

---

## Where the data comes from, and what it can't see

* **Leaderboard teams** come from the public
  [jwa-dashboard](https://github.com/Lullatsch/jwa-dashboard) project on GitHub, an
  unofficial community project that publishes snapshots of the Jurassic World Alive
  leaderboards (the monthly arena "Seasonal League" and weekly tournaments), usually
  twice a day (around 11:30 and 21:20 UTC). A new weekly tournament only appears once the
  feed has published its first snapshot. This tracker reads those public files. It never logs into the game,
  reads game traffic, or automates an account.
* Checked against Paleo.gg's independently published top-50 usage for the same day:
  every top creature matched to within about 1-2 percentage points.
* **Top 100 only.** That is all any permitted source publishes: Ludia offers no
  leaderboard API and its terms of service forbid scraping the game. You can view the
  Top 50, the Top 100, or ranks 51-100.
* **Anonymous rank groups.** Teams come in groups of ten ranks (1-10, 11-20, ...)
  without player names or trophy counts, which is exact enough for every view here.
* **Builds.** For every creature on every team the feed also gives its level,
  enhancement, stat boosts and omega training points. That is where the builds come from.
* **Names.** The feed sometimes gives German game names (e.g. *Wiedergeburt-T-Rex* =
  Rebirth T. Rex); these are translated and matched automatically. Creatures newer than
  the feed's own name list can arrive with no name at all, only a code. Those were
  identified from the feed's own evidence (rarity rules, levels, release version), and
  each one's page explains how, e.g. Pelorosuchus. A code with no such evidence is
  shown as *Unidentified creature* with a ? icon, never guessed.
* **Tournament rules.** Some tournaments set every creature to level 35 and switch
  stat boosts off; there is no build to choose there, and the dashboard says so instead
  of showing one.
* **No win rates.** No source publishes battle results, so the tracker cannot say which
  creature or build *wins* most. "Best build" means the build most top players use.
* The feed's maintainer does not document how the data is collected, and the feed could
  change or stop. If it does, the dashboard says so and keeps everything saved so far.

Other sources: the creature list comes from the public Paleo.gg Dinodex (refreshed
daily); the game version from Apple's App Store listing.

**Pictures.** Official game art belongs to Ludia/Universal and is not used. Every
creature gets a white silhouette of a real animal from [PhyloPic](https://www.phylopic.org)
(each creature's details show its credit): its own species where one exists, otherwise
the animal whose body shape it shares - a raptor-like hybrid gets a raptor, a flying
one a pterosaur, a snake a snake. These body types were checked by hand; new creatures
are matched automatically through their fusion ingredients.

---

## Using the dashboard

* **Players:** Top 100 (default), Top 50, or ranks 51-100. Every percentage and tier is
  recalculated for the chosen players.
* **Leaderboard:** arena ladder (default) or tournaments (special rules, kept separate).
* **Snapshot or game version:** the latest snapshot, any earlier one (historical tier
  lists), or a whole game version with all its snapshots combined.
* **Search**, **tier filter**, **sort** (usage, name, biggest rise/fall), and
  **Tiers / Ranking** layout. Only creatures used on at least one team are listed.
* Each card shows the exact percentage, how many teams use it (e.g. 82/100), a bar, and
  the change since the previous snapshot in percentage points (pp). The **Ranking**
  layout also shows every creature's best build (its most used stat boosts).
* **Click a creature** for its details, all for the players and snapshot you chose:
  * **Best build** - the exact build (level, enhancement, Attack/Health/Speed boosts) most
    top players use, the next most popular ones, average boosts, and omega training.
  * **Best teammates** - the creatures most often on the same team.
  * **Where it's used** - how many teams in each rank group (1-10, 11-20, ...) use it.
  * Usage over time, and how the data source names it.
* **History:** compare two snapshots or game versions, see risers, fallers and tier
  changes, and chart usage over time (up to 6 creatures; *Show as table* for numbers).
* The coloured dot at the top shows whether the data is up to date; if an update fails,
  a red note appears above the tier list (details on a PC: `VIEW_UPDATE_LOG.bat`).

**Usage % = teams that include the creature ÷ valid teams × 100.** A team is valid only
with exactly eight different, identified creatures and a known rank.
**Tiers:** S 80-100% · A 60-79.99% · B 40-59.99% · C 20-39.99% · D below 20%, using
exact counts (79.99% is never rounded up into S).

---

## Troubleshooting (PC version)

| Problem | What to do |
|---|---|
| "No data yet" | Wait a minute after the first start, or double-click `UPDATE_NOW.bat`. |
| "The last update failed" | Usually no internet or the data source is down. It retries every hour by itself. |
| "This data may be out of date" | The data source has not published anything for over 30 hours; the tracker keeps checking. |
| Moved this folder or upgraded Python | Double-click `START.bat` once; it repairs the automatic update. |
| Browser didn't open | Go to `http://127.0.0.1:8765/` (shown in the START window). |

Your data is stored in `%LOCALAPPDATA%\JWA Meta Tracker\` (outside OneDrive on purpose:
cloud sync can lock a database that is being written). **Uninstall:** run
`REMOVE_AUTO_UPDATE.bat`, then delete this folder and that data folder.

---

## Advanced (optional)

* **Your own creature pictures:** put an image in `web\img\custom\` named after the
  creature key, e.g. `web\img\custom\baryotor.png`.
* **Import other permitted data:** `python tracker.py import-csv board.csv --captured-at 2026-10-08T20:00`
  (columns `rank, trophies, player, creature1 ... creature8`). If it covers more than 100
  ranks, Top 250 / Top 500 views appear automatically.
* **Example data for testing:** `python tracker.py demo` (separate, clearly labelled).
* **Tests:** `scripts\run_tests.bat` (uses temporary folders only).
* **All commands:** `python tracker.py --help`.

## For programmers

Python 3.10+ standard library only, SQLite, and a static HTML/CSS/JavaScript dashboard.
`site.py` turns the database into `data/site.json` (per snapshot and rank range: the
number of valid teams and how many use each creature); the page adds those counts up
and divides. Builds and teams for the creature panel are in `data/details/YYYY-MM.json` (one file
per month, loaded only when a creature is opened); `stats.creature_profile` is the
tested reference that `web/app.js` mirrors exactly. The local server (`server.py`,
127.0.0.1 only) builds these files on the fly;
`tracker.py build-site` writes a complete static website for GitHub Pages
(`.github/workflows/update-website.yml`). `analysis.py` is the reference implementation
the tests compare against.

## Credits and disclaimer

Unofficial fan tool, not affiliated with or endorsed by Ludia, Jam City, Universal or
Jurassic World Alive. Creature names are used only to identify them. Leaderboard data:
the jwa-dashboard community project. Creature list: Paleo.gg. Silhouettes: PhyloPic
contributors under their Creative Commons licences (listed in the dashboard).
