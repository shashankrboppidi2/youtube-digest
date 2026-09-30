# YouTube digest

Once a day, `.github/workflows/digest.yml` summarises new uploads from the channels in `channels.txt` with an
open-source model (`qwen2.5:3b` through Ollama, on the runner's CPU) and commits
`digests/digest_YYYY-MM-DD.md`. A scheduled Claude session e-mails that file.

Per video: TL;DR, key points with links to the moment in the video, and a 1–5 "worth watching in full?" score.
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

Alternatives: a residential proxy in a `YT_PROXY` secret (`http://user:pass@host:port`, about $3–7/month), or a
self-hosted runner on an always-on machine at home (`runs-on: self-hosted`).

## Knobs

`LLM_MODEL` in the workflow picks the model. Any Ollama tag works (`llama3.2:3b`, `gemma3:4b`, `qwen2.5:7b` for
better but roughly 2× slower summaries). To use a hosted open model instead of the runner's CPU, set
`LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` to any OpenAI-compatible endpoint (Groq, OpenRouter, Together) and
drop the Ollama steps. Other settings are at the top of `digest.py`.

A manual run (Actions → YouTube digest → Run workflow) takes `lookback_hours` and `max_videos`.

Runtime: about 2 minutes of setup, then about 1–3 minutes per video on a 2-core runner, longer for 1–2 hour
podcasts. A run stops starting new summaries after 4 hours (`YT_BUDGET_MIN`); the rest go into the next day's digest. In a private repo that uses
the account's free Actions minutes; public repos run free on faster 4-core runners.
