# Instagram Scraper — Benford + Big Five

A local Python and Playwright application for collecting Instagram profile data and reviewing public follower counts with Benford’s Law. It also includes a separate exploratory Big Five caption report. These statistical and lexical summaries cannot identify bots or determine anyone’s personality.

## Features

- Collects posts, captions, public follower/following lists, and visible profile counters. Instagram may limit or round the data, and lists may be partial.
- Benford report compares first significant digits for follower and following counts, with at least 200 valid profiles required for each distribution.
- Separate Big Five report summarizes Spanish lexical matches and offers optional Gemini-assisted text review. It is not a validated personality test. Spanish vocabulary is intentionally retained because the target captions are in Spanish.
- Local dashboard and reports are generated from saved extraction files; no credentials or collected data belong in the repository.

## Requirements and installation (Windows PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Collect data

```powershell
.\.venv\Scripts\python.exe scraper.py --username josedeodo --limit 50 --relationship-limit 100
```

For Benford analysis, request a sample of up to 250 followers so the scraper can try to collect at least 200 profiles with both public counters:

```powershell
.\.venv\Scripts\python.exe scraper.py --username josedeodo --limit 25 --relationship-limit 250 --benford
```

The scraper opens a browser for your Instagram session. Sign in only in the browser window it opens. Follow Instagram’s rules and use a modest collection rate. A complete 200-profile sample is not guaranteed.

## Build and open the dashboard

```powershell
.\.venv\Scripts\python.exe dashboard.py
```

To enable optional Gemini analysis, set your API key in the same PowerShell terminal and start the local server:

```powershell
$env:GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
.\.venv\Scripts\python.exe gemini_server.py
```

Open `http://127.0.0.1:8765/dashboard.html`. Keep the terminal open while using the dashboard. Gemini receives captions only when you click the analysis button. Never commit API keys, browser sessions, or collected data.

## Benford interpretation

The report displays observed first-digit counts and proportions alongside Benford’s theoretical distribution. It reports mean absolute deviation (MAD) and uses descriptive fit categories. A deviation is a reason to inspect a sample with other indicators, not a bot threshold, bot probability, or individual classification. Instagram-rounded counts and partial samples can distort results.

## Project files

- `scraper.py` — Playwright data collection and extraction options.
- `dashboard.py` — builds the dashboard, Benford report, and Big Five report.
- `gemini_server.py` — local static server and optional Gemini API proxy.
- `requirements.txt` — Python dependencies.
