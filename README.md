# YouTube digest

Once a day, `.github/workflows/digest.yml` summarises new uploads from the channels in `channels.txt` with an
open-source model (`qwen2.5:3b` through Ollama, on the runner's CPU) and commits
`digests/digest_YYYY-MM-DD.md`. A scheduled Claude session e-mails that file.

Per video: TL;DR, key points with links to the moment in the video, and a 1–5 "worth watching in full?" score.
Shorts and clips (under 250 words of transcript) are skipped.

## Channels

Edit `channels.txt`: one `@handle`, channel URL or `UC…` id per line. A channel added later contributes
only its last 48 hours of uploads on its first run, not its whole back catalogue.

## YouTube blocks GitHub's servers

From GitHub-hosted runners YouTube refuses caption downloads ("Sign in to confirm you're not a bot").
The channel listing still works. Blocked videos are retried on the next run, and the digest says so.
Pick one of these fixes:

| Fix | Setup | Cost |
|---|---|---|
| **Residential proxy** (most reliable) | Add a `YT_PROXY` repo secret, e.g. `http://user:pass@p.webshare.io:80` from a Webshare "residential" plan | about $3–7/month |
| **Browser cookies** | Export youtube.com cookies (Netscape `cookies.txt`, e.g. with the "Get cookies.txt LOCALLY" extension) from a browser signed in to a spare Google account, and paste the whole file into a `YT_COOKIES` repo secret | free; expires every few weeks; use a spare account |
| **Self-hosted runner** | Register your own always-on machine as a runner and change `runs-on` to `self-hosted` | free; that machine's home IP isn't blocked, and Ollama runs faster there |

## Knobs

`LLM_MODEL` in the workflow picks the model. Any Ollama tag works (`llama3.2:3b`, `gemma3:4b`, `qwen2.5:7b` for
better but roughly 2× slower summaries). To use a hosted open model instead of the runner's CPU, set
`LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` to any OpenAI-compatible endpoint (Groq, OpenRouter, Together) and
drop the Ollama steps. Other settings are at the top of `digest.py`.

A manual run (Actions → YouTube digest → Run workflow) takes `lookback_hours` and `max_videos`.

Runtime: about 2 minutes of setup, then about 1–3 minutes per video on a 2-core runner. In a private repo that uses
the account's free Actions minutes; public repos run free on faster 4-core runners.
