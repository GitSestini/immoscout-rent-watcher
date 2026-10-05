# ImmoScout rental alert

Checks an ImmobilienScout24 rental search every 10 minutes and emails you as
soon as a **new** offer appears. No history, no daily digest: just the alert.

Each alert shows the photo, **warm rent** (search results only show the cold
rent, so it's read from the listing itself), cold rent + utilities, whether
heating is included, size, rooms, €/m², floor, deposit, move-in date, private
landlord vs. agency, and a button to open the listing. For a single new offer,
the key numbers are in the subject line, so they show on your phone's lock
screen.

## How it works

- `watcher.py` calls ImmoScout's mobile app API (plain JSON, no bot wall). It
  turns your web search URL into the API query, including the hand-drawn map
  area and the "total rent" price filter (`pricetype=calculatedtotalrent`).
- `data/seen.json` only remembers which offers were already seen, so nothing is
  alerted twice. Offers are forgotten 60 days after they were last online.
- **Re-posts are skipped:** if an ad disappears and a new ad shows up with the
  same postcode, rooms and size (±1 m²), it's treated as the same flat and not
  alerted again.
- The first run emails every offer currently online, so you start from a known
  set. After that, only new offers trigger an email.
- If ImmoScout's API has a hiccup, the run just skips. The run only fails (and
  GitHub notifies you) after about an hour of consecutive failures.

## Setup

Repo → **Settings** → **Secrets and variables** → **Actions**:

| Secret | Value |
|---|---|
| `SEARCH_URL` | your ImmoScout rental search URL |
| `GMAIL_ADDRESS` | the Gmail account that sends the alerts |
| `GMAIL_APP_PASSWORD` | a Gmail app password (https://myaccount.google.com/apppasswords) |
| `MAIL_TO` | recipients, comma-separated |

Until all of them are set, every run skips quietly instead of failing.

**This repo is public** (Actions minutes are free and unlimited for public
repos). Secrets stay encrypted and are masked in logs, but the code and
`data/seen.json` (listing IDs, titles, postcodes) are visible to anyone.

## Manual run / test

Actions → **Rental alert** → **Run workflow**. Tick `send_test` to email every
offer currently online. Use **Run workflow**, not "Re-run jobs" (re-runs replay
the old commit's code).

## Tweaks

- **Frequency:** edit the `cron` line in `.github/workflows/watch.yml`
  (e.g. `"7,37 * * * *"` for every 30 min). GitHub may run scheduled jobs a few
  minutes late at busy times.
- **Change the search:** update the `SEARCH_URL` secret with a new ImmoScout
  URL.
