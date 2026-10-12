# canvas-deadlines (Phase 1)

A script that pulls every exam, midterm, quiz, and assignment deadline out
of your University of Toronto Quercus (Canvas) courses - including dates
that are only mentioned in a syllabus or announcement, not entered as a
real Canvas calendar event - and writes them to an `.ics` file you can
import into Apple Calendar.

This is **Phase 1**: a script you run by hand on your own computer. Later
phases (not built yet) will automate this with GitHub Actions + a
subscribable calendar link, and eventually a native iPhone app.

## How it works, briefly

1. Fetches your active courses, their assignments, calendar events,
   announcements (last ~60 days), and syllabus text from Canvas.
2. Assignment due dates and calendar events already have a structured date
   from Canvas, so those are used directly.
3. Also scans Canvas **Modules** for Files/Pages that look like a course
   outline (title contains "outline", "syllabus", "schedule", etc.) -
   many instructors never fill in Canvas's official Syllabus tool and
   instead upload a PDF into a Module. PDFs are downloaded and their text
   extracted automatically.
4. Announcements, syllabus text, and these module documents are all free
   text, so the script sends that text to Claude (Anthropic's AI model,
   `claude-haiku-4-5` by default) and asks it to pull out any exam/quiz/
   assignment dates mentioned, as structured JSON.
5. Everything is merged into one list, duplicates are removed, and if the
   same event appears with conflicting dates (e.g. "the midterm moved to
   Nov 17"), the most recently posted source wins.
6. The result is written to `output/deadlines.ics`.

## Setup

### 1. Create a virtual environment and install dependencies

From inside this folder (PowerShell):

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

This keeps the packages this project needs separate from anything else on
your computer. Every time you come back to run the script in a new
terminal window, either activate the environment first:

```
.venv\Scripts\activate
python main.py
```

...or just call the venv's Python directly without activating:

```
.venv\Scripts\python main.py
```

### 2. Create your `.env` file

Copy the example file and fill in your real values:

```
copy .env.example .env
```

Then open `.env` in a text editor and fill in the three values below.

### 3. Getting a Canvas access token

1. Log into Quercus at https://q.utoronto.ca
2. Click **Account** (your profile picture, top-left) → **Settings**
3. Scroll down to **Approved Integrations**
4. Click **+ New Access Token**
5. Give it a purpose like "deadlines script", leave the expiry date blank
   (or set one - just remember to regenerate it later), and click
   **Generate Token**
6. Copy the long token string **immediately** - Canvas only shows it once.
   Paste it into `.env` as `CANVAS_TOKEN=...`

`CANVAS_BASE_URL` should already be correct (`https://q.utoronto.ca`) -
leave it as-is.

### 4. Getting an Anthropic API key

1. Go to https://console.anthropic.com/settings/keys
2. Create a key (you'll need a payment method on file - this script makes
   a small number of API calls per run, so cost should be a few cents at
   most per run, even with a full course load)
3. Paste it into `.env` as `ANTHROPIC_API_KEY=...`

**Never share your `.env` file or paste these values anywhere public.**
The `.gitignore` in this folder already excludes `.env` from being
committed if you put this project under version control.

## Running it

```
python main.py
```

This fetches everything, prints a summary table, and writes
`output/deadlines.ics`.

To just see what it *would* find, without writing the file:

```
python main.py --dry-run
```

### Importing into Apple Calendar

1. Run the script (without `--dry-run`) to produce `output/deadlines.ics`
2. On a Mac: double-click the file, or open Calendar.app and choose
   **File → Import...** and select it
3. On an iPhone: AirDrop the file to yourself, or email it to yourself and
   tap the attachment, then choose which calendar to add it to

Each event has a stable ID, so if you re-run the script later after a date
changes and re-import the file, it updates the existing event instead of
creating a duplicate (Apple Calendar matches on that ID - this is also
what will make Phase 2's auto-subscribed calendar link work smoothly).

## Understanding the output

The terminal prints a table like:

```
Course      Event                        Date/Time          Source
----------  ---------------------------  -----------------  ------------------------------
CSC369H1    [midterm] Midterm 1          2026-11-17 10:00   Announcement (Midterm update): moved from Nov 10 to Nov 17
MAT237Y1    [assignment] Assignment 3    2026-11-20          Canvas assignment due date (https://q.utoronto.ca/...)
```

- **Course** - the Canvas course code
- **Event** - `[type] title`, where type is one of exam / midterm / quiz /
  assignment / other
- **Date/Time** - in America/Toronto time; dates with no time shown are
  all-day events
- **Source** - where the date came from, so you can double check anything
  that looks off (especially `other`-typed rows pulled from Claude - those
  are its best guess, not a guarantee)

**Always skim the table before trusting it**, especially early on. The
Claude-extracted events (from announcements/syllabi) are a best effort at
reading free text - if a date looks wrong, check the source noted next to
it.

## The cache folder

`cache/extraction_cache.json` stores Claude's extraction results, keyed by
a hash of the text it was given. This means re-running the script doesn't
re-send unchanged announcements/syllabi to the Claude API - it only pays
for genuinely new or edited text. It's safe to delete this file any time;
it'll just be slower (and slightly more costly) on the next run while it
rebuilds.

## Known limitations (Phase 1)

- Course/term filtering uses Canvas's own term start/end dates; courses
  with no term end date are always included.
- Assignment due dates are always written as a timed event at whatever
  time Canvas reports (including 23:59/11:59pm deadlines) - they are never
  converted to all-day events, since many real deadlines genuinely are at
  11:59pm.
- Calendar events that look like regular lectures/tutorials/office hours
  are filtered out by keyword, so they don't clutter the calendar - this is
  a simple keyword match, not foolproof. Calendar events whose title is
  just a person's name (e.g. "TA Jane Doe(TUT501)") are also filtered out,
  since these are near-universally tutorial-leader assignment slots, not
  deadlines.
- Module scanning only looks at Files/Pages whose *title* contains a
  syllabus-like keyword (see `_SYLLABUS_LIKE_KEYWORDS` in `main.py`) - it
  does not scan every file in every module (that would be slow, noisy, and
  expensive to run through Claude). A course outline with an unusual title
  could be missed.
- PDF text extraction (`pypdf`) only works on PDFs that have a real text
  layer. A scanned/photographed syllabus with no text layer will extract
  as empty, and the script will print a warning telling you to check that
  file manually.
- A course can have more than one Canvas calendar entry for what's "really"
  the same event (e.g. one per lecture section). These aren't
  automatically merged if their titles differ enough - check the Source
  column for anything that looks like it might be a different section than
  yours.
- Deduplication matches on course + event type + a normalized title (noise
  words like "exam"/"midterm"/"the" stripped out). Two sources describing
  the same event very differently (e.g. "Test 1" vs "First Term Test")
  might not be recognized as the same event and could show up twice.
- If you re-run the script after Canvas text changes, previously-cached
  extractions for that exact text are reused automatically; edited text is
  treated as new automatically (its hash changes).

## Troubleshooting

- **"Missing or unfilled setting: ..."** - you haven't filled in `.env`
  yet, or still have one of the `paste_your_...` placeholder values in it.
- **"Canvas rejected your access token (401)"** - your token is wrong,
  expired, or was revoked. Generate a new one (see step 3 above).
- **"Canvas refused the request (403)"** - usually a rate limit; wait a
  minute and try again.
- **"Anthropic rejected your API key"** - double check
  `ANTHROPIC_API_KEY` in `.env` and that the key is active in the
  Anthropic console.
- Any other error will print a plain-language explanation and exit rather
  than crashing with a raw Python traceback - if you see a traceback
  instead, that's a bug, not expected behavior.

## Phase 2: automated daily sync + Apple Calendar subscription

A GitHub Actions workflow (`.github/workflows/sync-deadlines.yml`, already
in this repo) runs the script twice a day, publishes `deadlines.ics` to
GitHub Pages, and you *subscribe* to that URL in Apple Calendar - it just
updates itself from then on, no more manual re-importing.

### Privacy note before you start

`deadlines.ics` reveals your course codes and exam schedule. A GitHub
Pages URL is reachable by anyone who has the link, **regardless of whether
the source repo is public or private** - the only thing repo privacy
controls is whether people can casually *find* that URL by browsing your
repo/profile. The setup below does two things to protect you:

1. **Use a private repository** if you can (GitHub Pages on a private repo
   needs GitHub Pro - free for students via the
   [Student Developer Pack](https://education.github.com/pack), or skip
   this and use a public repo if you can't get Pro working right now).
2. **Publish at a secret, random path** (e.g. `/a1b2c3.../deadlines.ics`
   instead of `/deadlines.ics`) via the `ICS_PATH_TOKEN` secret below, so
   even a public repo's Pages site isn't at an obvious URL.

Neither is perfect secrecy (nothing posted to the open internet ever is),
but together they're the practical ceiling without paying for GitHub
Enterprise.

### Setup checklist

**1. Create the GitHub repository**
- Go to https://github.com/new
- Name it anything (e.g. `canvas-deadlines`)
- Set visibility to **Private** if you have GitHub Pro/Student Pack,
  otherwise **Public**
- Don't initialize with a README/.gitignore (this folder already has them)
- Click **Create repository**, then follow GitHub's "...or push an
  existing repository from the command line" instructions to push this
  folder - or ask me to do it once the repo exists and you've confirmed
  you can sign in.

**2. Generate a random path token** (defense-in-depth from the privacy
note above). In PowerShell:
```
[guid]::NewGuid().ToString("N")
```
Copy the resulting string (a long string of random letters/numbers) -
you'll paste it into a secret in the next step.

**3. Add three repository secrets**
- In the repo: **Settings → Secrets and variables → Actions → New
  repository secret**
- Add each of these (name must match exactly):
  - `CANVAS_TOKEN` - your Canvas access token (same one from `.env`)
  - `ANTHROPIC_API_KEY` - your Anthropic API key (same one from `.env`)
  - `ICS_PATH_TOKEN` - the random string from step 2

**4. Enable GitHub Pages**
- **Settings → Pages**
- Under **Build and deployment → Source**, choose **GitHub Actions** (not
  "Deploy from a branch" - the Actions method never commits the published
  file into any browsable branch, which matters for the privacy note
  above)

**5. Push the code and let the workflow run once**
- Push this folder to the repo (step 1)
- Go to the **Actions** tab → **Sync Canvas deadlines** → **Run workflow**
  to trigger it manually the first time
- Watch it go green. If it fails, click into the run - the same
  plain-language error messages from the Troubleshooting section above
  show up in the log.

**6. Get your subscribe URL**
- After a successful run, go to **Settings → Pages** - GitHub shows your
  site's base URL, something like `https://<username>.github.io/<repo>/`
- Your calendar subscribe URL is that base URL plus
  `<your-ICS_PATH_TOKEN>/deadlines.ics`, e.g.:
  `https://<username>.github.io/<repo>/a1b2c3.../deadlines.ics`
- Treat this URL like a password - anyone with it can see your schedule.

**7. Subscribe in Apple Calendar on your iPhone**
- Settings app → **Calendar** → **Accounts** → **Add Account** → **Other**
- Tap **Add Subscribed Calendar**
- Paste your subscribe URL from step 6, tap **Next**, then **Save**
- iOS will periodically re-fetch it on its own (you can adjust how often
  under the subscribed calendar's settings, though iOS doesn't let you
  force sub-hourly refresh - a few times a day is typical)

**8. Make sure failure emails reach you**
- GitHub already emails you when a workflow run fails, as long as it's not
  turned off: check **github.com → your profile photo → Settings →
  Notifications → System**, and make sure Actions failure notifications
  are enabled (on by default for workflows you own).

### Why twice a day, and why the time might drift

Cron schedules in GitHub Actions are always UTC. The two schedules in the
workflow (`12:00` and `00:00` UTC) land around 8am and 8pm Toronto time,
but will drift by about an hour around the twice-yearly daylight-saving
change - GitHub Actions cron has no timezone awareness, so this is a
known limitation, not a bug. You can also always trigger a sync
immediately from the Actions tab's **Run workflow** button regardless of
the schedule.

### Why the cache step looks unusual

`actions/cache` never overwrites an already-used key, but we want the
extraction cache to update every single run (otherwise newly-posted
announcements would never get cached and you'd pay to re-extract
unrelated unchanged text too, since the whole file is one JSON blob). The
workflow uses the standard pattern for this: a key that's always unique
(`extraction-cache-<run id>`) so saving always succeeds, with a shared
prefix (`extraction-cache-`) as the `restore-keys` fallback so each run
still picks up the most recent previous cache.

## What's next (not built yet)

- **Phase 3:** a native iPhone app (SwiftUI + EventKit) that writes events
  directly into Apple Calendar.

The code is already split so that `events.py` (the `Event` shape) and
`ics_writer.py` are reused as-is by Phase 2; Phase 3 would likely reuse
the same underlying event-fetching logic via a small JSON export.
