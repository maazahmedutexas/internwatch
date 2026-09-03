# internwatch

Polls the ATS platforms that big tech actually posts to, filters hard for roles that
fit your profile, and pings your phone the moment a new one shows up.

Built for: ECE, embedded SWE lean, tier-1 companies only, US West Coast / Austin,
pay floor around your current Tesla rate.

---

## Why not LinkedIn

LinkedIn rate-limits and IP-bans scrapers within hours, and it is a downstream mirror
anyway. Roles hit the company's applicant tracking system (Workday, Greenhouse, Apple's
own search API) at the same moment or *before* they appear on LinkedIn. This polls the
source directly, so it is both faster and does not break.

## Sources

| Source | Covers |
|---|---|
| SimplifyJobs community feed | Broadest and usually fastest signal in existence. Thousands of people watch that repo, so new reqs land within minutes. |
| Workday CXS API | NVIDIA, AMD, Qualcomm, Micron, Intel, Marvell, ADI, Applied Materials |
| Greenhouse Boards API | Anduril, Databricks, Stripe, Zoox, Waymo, Skydio, Astranis |
| Lever API | any company on Lever, add tokens to config |
| Direct endpoints | Microsoft, Amazon, Apple, Google, Meta, Tesla |

Multiple sources overlap on purpose. If Apple changes its endpoint next month, Simplify
still catches Apple reqs. Deduplication happens on a normalized company+title+location hash.

---

## Your setup: ntfy + GitHub Actions + Summer 2027

Already configured. Five steps, about ten minutes.

### 1. Install ntfy and subscribe

Get the **ntfy** app (iOS App Store / Google Play). Tap **+**, subscribe to:

```
maaz-iw-746de7f46018
```

That topic was randomly generated for you and is already in `config.yaml`. The topic
string is the only credential, so treat it like a password.

Test it right now before doing anything else:

```bash
curl -d "internwatch test" https://ntfy.sh/maaz-iw-746de7f46018
```

If your phone buzzes, the hard part is done.

### 2. Make the repo PUBLIC (and move the topic to Secrets)

This matters. Public repos get **unlimited** free Actions minutes. Private repos get
2,000/month, and polling every 15 minutes burns roughly 2,880. You would hit the wall
around day 20 of every month.

So: push this as a **public** repo, then blank the topic in `config.yaml`:

```yaml
  ntfy:
    server: https://ntfy.sh
    topic: ""      # supplied by the NTFY_TOPIC secret
```

and add it under **Settings > Secrets and variables > Actions > New repository secret**:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | `maaz-iw-746de7f46018` |

The script reads `NTFY_TOPIC` from the environment first and falls back to config, so
this works locally and in CI without changing anything else.

If you would rather keep the repo private, change the cron to `*/30 * * * *` to stay
inside the free tier, and you can leave the topic in `config.yaml`.

### 3. Verify the sources from your laptop

```bash
pip install -r requirements.txt
python internwatch.py --check-sources
```

Any row reading `EMPTY` means that company moved its endpoint. Fix the slug (see below)
or ignore it, since the Simplify feed backstops most of them.

### 4. Seed it

Go to the **Actions** tab, pick **internwatch**, hit **Run workflow**, and toggle
**seed** to `true`.

This marks all ~2,500 currently-open postings as already-seen and sends nothing. Skip it
and your first scheduled run will fire 53 notifications at once.

### 5. Let the schedule take over

Nothing more to do. It runs every 15 minutes from then on.

### Two GitHub Actions gotchas

**Timing.** `*/15` is a request, not a promise. GitHub throttles scheduled workflows
under load, so expect 15 to 30 minutes of real latency. For internship reqs that is
fine, since the bottleneck is you writing the application, not the alert. If you ever
want sub-5-minute latency, move it to a Pi or a VPS with cron; the code is identical.

**The 60-day rule.** GitHub disables scheduled workflows on repos with no activity for
60 days, and commits made by the workflow itself do not reset that timer. You will get an
email when it happens. Either click re-enable, or push any real commit every couple of
months. Setting a calendar reminder for early November is the easy fix.

### How state persists

The runner has no disk between runs, so `seen.json` lives on an orphan branch named
`state` that holds exactly one file and exactly one commit, force-pushed each run. Your
`main` branch stays clean and the repo never accumulates thousands of state commits.

If you ever want to reset and start fresh:

```bash
git push origin --delete state
```

Then run the workflow in seed mode again.

---

## Setup (local / other deployments)

```bash
pip install -r requirements.txt
```

### 1. Pick a notification channel

**ntfy (free, recommended)** — install the `ntfy` app on iOS or Android, pick a random
topic name, put it in `config.yaml`, subscribe to it in the app. Instant push, zero cost,
no signup. The topic string is the only secret, so make it random:

```yaml
notify:
  channels: [ntfy]
  ntfy:
    topic: maaz-iw-9f2k4jx7qp
```

**Twilio (real SMS to +1 346-666-4713)** — buy a number in the Twilio console
(~$1.15/mo), then:

```yaml
notify:
  channels: [twilio]
  twilio:
    to_number: "+13466664713"
    from_number: "+1XXXXXXXXXX"    # your purchased Twilio number
```

```bash
export TWILIO_SID=ACxxxxxxxx
export TWILIO_TOKEN=xxxxxxxx
```

Never put those in `config.yaml` if the repo is public.

You can run both: `channels: [ntfy, twilio]`.

### 2. Verify every source works

```bash
python internwatch.py --check-sources
```

Any row showing `EMPTY` means that company changed its slug. Fix it by opening their
careers page and reading the URL. For Workday:

```
https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
        └──────── host ────────────┘        └────── site ────────┘
        └tenant┘
```

For Greenhouse the token is the slug in `job-boards.greenhouse.io/<token>`.
For Lever it is the slug in `jobs.lever.co/<token>`.

### 3. Seed the database

**Do this before your first real run**, or you will get 55 notifications at once.

```bash
python internwatch.py --seed
```

This marks everything currently posted as already-seen. From then on you only hear about
genuinely new reqs.

### 4. Dry run

```bash
python internwatch.py --once --dry-run
```

Prints what it would send without sending anything. Tune `min_score` and the keyword
lists until the output looks right.

### 5. Run it

```bash
python internwatch.py            # foreground loop, polls every 10 min
python internwatch.py --once     # single pass, for cron
```

---

## Deployment

Pick one.

### GitHub Actions

See the "Your setup" section above.

### A machine you control (most reliable)

Any always-on box: a Raspberry Pi, a $5/mo VPS, or a spare laptop.

```bash
# crontab -e
*/10 * * * * cd /home/maaz/internwatch && /usr/bin/python3 internwatch.py --once >> watch.log 2>&1
```

Or systemd if you want it supervised:

```ini
# /etc/systemd/system/internwatch.service
[Unit]
Description=internwatch
After=network-online.target

[Service]
Type=simple
User=maaz
WorkingDirectory=/home/maaz/internwatch
ExecStart=/usr/bin/python3 /home/maaz/internwatch/internwatch.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now internwatch
```

---

## Tuning

Everything lives in `config.yaml`. The three knobs that matter:

**`min_score`** — currently `6`. Raise to `8` for embedded-SWE-at-tier-1 only. Drop to
`4` if you feel like you are missing things.

**Scoring breakdown:**

| Signal | Points |
|---|---|
| tier1 company | +3 |
| tier2 company | +2 |
| embedded AND software in the posting | +5 |
| embedded only | +4 |
| software only | +3 |
| hardware only | +1 |
| Bay Area / Austin / Seattle / SD / NYC / Boston | +2 |
| other allowed US metro or remote | +1 |
| pay at or above `great_hourly` (58) | +3 and tagged `[TOP PAY]` |
| pay at or above `floor_hourly` (45) | +1 |
| title mentions 2027 | +2 |
| pay below floor | -3 and tagged `[LOW PAY]` |
| pay not published anywhere | 0 and tagged `[pay?]` |

**Hard rejects, before scoring even runs:** not an internship title, grad-gated
(PhD / master's / new grad), wrong term season, company not on either tier list,
location outside the US allowlist, or no embedded / software / hardware signal at all.

**`companies.tier1` / `tier2`** — 118 tier1 and 38 tier2 names covering big tech,
semiconductors, autonomy, aerospace, defense and quant. A company not on either list is
dropped outright. This is the hard filter that kills the noise your old script was sending.

**`roles.undergrad`** — rejects PhD, master's, doctoral, new grad and early career
postings. It rejects on title outright, and on description only for exclusively
grad-gated phrasing, since plenty of good reqs say "BS/MS/PhD in EE" and you qualify for
those. Anything mentioning bachelor's or undergrad overrides the description reject.

**`locations.block`** — the Poland killer. Anything matching gets dropped before scoring.

### Pay handling

California, Washington, New York and Colorado have pay transparency laws, so postings in
those states usually publish a range and the script parses it out of the description
(handling hourly, monthly and annual, all normalized to USD/hour).

Postings elsewhere often hide pay. Rather than silently dropping half the market,
`known_pay_hourly` supplies a market estimate and the message prefixes it with `~`. Parsed
numbers never show the tilde, so you can always tell which is real. Edit those estimates
as you hear actual numbers from people.

Worth knowing before you set the floor too high: Micron, TSMC and Applied Materials tend
to land in the high 30s, AMD and TI in the low 40s, and defense (L3Harris, Lockheed,
Northrop) around 32. You specifically asked for Micron, TSMC and TI, all of which sit
below your Tesla rate. So `notify_if_below_floor` is `true`: they reach you, tagged
`[LOW PAY]` with a 3-point penalty so they sort to the bottom. Flip it to `false` if you
decide you never want to see them.

At the other end, quant and HFT firms (Jane Street, HRT, Citadel, Optiver, Jump) pay
roughly 2x anything in tech for SWE interns and hire ECE students heavily. They are in
tier1 and tagged `[TOP PAY]`. Delete that block from `config.yaml` if you are not
interested.

### Term filtering

`block_terms` drops the wrong cycle. Update it each year. If you don't want another gap
semester, add `"spring 2027"` to that list.

`term_boost` only adds points, it does not filter, because a lot of good reqs are titled
plainly as "Software Engineer Intern" with the season buried in the description.

This is why Summer 2027 filtering blocks the *wrong seasons* rather than requiring the
right one. Demanding "summer 2027" in the title would have cut Databricks, Stripe and
half the Amazon reqs from the live test run.

---

## Files

```
internwatch.py    the whole thing
config.yaml       every knob
seen.json         state file, created on first run, do not delete
requirements.txt
.github/workflows/watch.yml
```

## Troubleshooting

**No notifications at all** — run `--once --dry-run -v`. If matches print but nothing
sends, the channel config is wrong. If nothing matches, lower `min_score`.

**Same job twice** — different sources spelled the location differently enough to change
the hash. Harmless, and rare.

**A source went EMPTY** — that company changed its endpoint. Fix the slug, or just leave
it; the Simplify feed covers most of them as a backstop.

**Getting too much** — raise `min_score` to 8, or trim `tier2` to nothing.
