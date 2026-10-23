# YouTube digest

Once a day, `.github/workflows/digest.yml` summarises new uploads from the channels in `channels.txt` with an
open-source model and commits `digests/digest_YYYY-MM-DD.md`. `email.yml` e-mails it at 7am Eastern. The model is
`openai/gpt-oss-120b` on Groq's free tier when the `LLM_API_KEY` secret is set (see below), otherwise `qwen2.5:3b`
through Ollama on the runner's CPU, which is also the fallback when the free quota runs out.

The e-mail opens with **Today's signals**: themes across the videos, what people are bullish and cautious on, and
private / pre-IPO companies in the news. Then per video: the bottom line, signals (🟢 bullish / 🔴 cautious / 🟡 mixed,
who holds the view and why), AI and private-market points, other key insights, and links to the best moments.
Videos off the topics in `interests.txt` are skipped (listed at the end), and off-topic segments are left out of the
videos that are kept.
Shorts and clips (under 250 words of transcript) are skipped.

## Channels

Edit `channels.txt`: one `@handle`, channel URL or `UC…` id per line. A channel added later contributes
only its last 48 hours of uploads on its first run, not its whole back catalogue.

## YouTube blocks GitHub's servers: the `YT_COOKIES` secret

From GitHub-hosted runners YouTube refuses caption downloads ("Sign in to confirm you're not a bot").
The fix used here is cookies from a signed-in browser, which yt-dlp sends along:

1. Use a **spare Google account**, not your main one. YouTube can flag accounts used by scripts.
2. In Chrome or Firefox, install the **"Get cookies.txt LOCALLY"** extension.
3. Open a **private/incognito window** (allow the extension there), sign in to youtube.com with the spare account,
   and open any video.
4. Export cookies for youtube.com with the extension (Netscape format), then **close the private window
   without signing out**. That stops the browser from rotating the cookies you just exported.
5. In this repo: Settings → Secrets and variables → Actions → New repository secret, name `YT_COOKIES`,
   and paste the whole file.

The cookies last a few weeks. When they expire, the digest e-mail says "YouTube blocked transcript
downloads". Repeat the steps above then. Blocked videos from the last 30 days are retried every run.

### NordVPN (`YT_PROXY`, free if you already subscribe)

NordVPN's SOCKS5 servers are data-centre IPs, so YouTube may block them too, but they cost nothing to try:
Nord Account → NordVPN → Set up NordVPN manually → **Service credentials** (not your login), then a secret
`YT_PROXY` = `socks5h://SERVICE_USER:SERVICE_PASS@amsterdam.nl.socks.nordhold.net:1080`
(other SOCKS hosts: `atlanta.us.socks.nordhold.net`, `dallas.us.socks.nordhold.net`, `los-angeles.us.socks.nordhold.net`,
`new-york.us.socks.nordhold.net`, `stockholm.se.socks.nordhold.net`).

### Residential proxy (`YT_PROXY`)

If cookies stop working, route the transcript downloads through a residential proxy (channel listing goes direct):
1. Sign up at webshare.io and buy a **Residential** plan (rotating residential, not "proxy server" or
   "static residential"). About 1–2 GB a month covers ~30 videos a day.
2. In the dashboard under Proxy → Residential, copy the rotating endpoint's username and password.
3. Add a repo secret `YT_PROXY` = `http://USERNAME-rotate:PASSWORD@p.webshare.io:80`.

## Model: the `LLM_API_KEY` secret

1. Sign in at https://console.groq.com (free, no card) → **API Keys** → **Create API Key**, copy it.
2. In this repo: **Settings → Secrets and variables → Actions → New repository secret**, name `LLM_API_KEY`, paste it.

That's all: the next run uses `openai/gpt-oss-120b`. Another OpenAI-compatible provider or model works too: add
repository *variables* `LLM_BASE_URL` (e.g. `https://openrouter.ai/api/v1`) and `LLM_MODEL`. The free tier allows a
limited number of tokens per day; when it is used up the rest of the run falls back to the local model and the digest
says so. Delete the secret to go back to the local model only.

## Knobs

`LLM_MODEL` in the workflow picks the local (Ollama) model. Any Ollama tag works (`llama3.2:3b`, `gemma3:4b`, `qwen2.5:7b` for
better but roughly 2× slower summaries). To use a hosted open model instead of the runner's CPU, set
`LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` to any OpenAI-compatible endpoint (Groq, OpenRouter, Together) and
drop the Ollama steps. Other settings are at the top of `digest.py`.

A manual run (Actions → YouTube digest → Run workflow) takes `lookback_hours` and `max_videos`.

Runtime: about 2 minutes of setup, then about 1–3 minutes per video on a 2-core runner, longer for 1–2 hour
podcasts. A run stops starting new summaries after 4 hours (`YT_BUDGET_MIN`); the rest go into the next day's digest. In a private repo that uses
the account's free Actions minutes; public repos run free on faster 4-core runners.

## E-mail

`email.yml` runs `send_email.py` at 7am Eastern (06:57, on whichever of its two UTC triggers matches the
current daylight-saving offset) and sends the day's digest as HTML through Gmail. It needs three repo secrets:

| Secret | Value |
|---|---|
| `SMTP_USER` | the Gmail address that sends the digest |
| `SMTP_PASS` | a Gmail **app password** for that address (Google Account → Security → 2-Step Verification → App passwords) |
| `MAIL_TO` | where the digest goes; comma-separate several addresses |

Actions → YouTube digest e-mail → Run workflow sends today's digest immediately (handy for testing).

## Skipping videos

`skip.txt` lists title patterns to ignore (case-insensitive regular expressions). `[Channel Name] pattern`
limits a rule to one channel. Skipped videos are never summarised or listed.
