# Daily Briefing

A personal morning briefing page that automatically fetches your Google Tasks each weekday at 06:00, generates an AI summary via Claude, and publishes a fresh `index.html` to GitHub Pages.

**Live at:** `https://jgilbert82.github.io/daily-briefing`

---

## Setup (one-time)

### 1. Create the repo

- Create a new GitHub repository called `daily-briefing`
- Enable GitHub Pages: **Settings → Pages → Source: Deploy from branch → main → / (root)**

### 2. Get a Google OAuth refresh token

You need a refresh token that allows the script to read your Google Tasks without your interaction.

**a) Create a Google Cloud project:**
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. `daily-briefing`)
3. Enable the **Google Tasks API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON — you'll get `client_id` and `client_secret`

**b) Get a refresh token (run once locally):**

```bash
pip install google-auth-oauthlib
python get_token.py
```

This opens a browser, asks you to sign in, and prints a JSON blob with your credentials. Copy the whole thing.

### 3. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these two secrets:

| Secret name | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | The full JSON from step 2b |
| `ANTHROPIC_API_KEY` | Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com) |

### 4. Push these files

```
daily-briefing/
├── .github/
│   └── workflows/
│       └── daily-briefing.yml
├── generate.py
├── get_token.py
├── index.html          ← auto-generated, commit a placeholder first
└── README.md
```

Commit and push. The workflow will run automatically at 06:00 UTC+1 on weekdays, or you can trigger it manually from **Actions → Daily Briefing → Run workflow**.

---

## Manual trigger

Go to **Actions → Daily Briefing → Run workflow** any time you want a refresh.

---

## Adjusting the schedule

Edit `.github/workflows/daily-briefing.yml` — the cron line:

```yaml
- cron: '0 5 * * 1-5'   # 05:00 UTC = 06:00 Copenhagen (winter)
```

Change to `'0 4 * * 1-5'` for summer time (CEST, UTC+2).
