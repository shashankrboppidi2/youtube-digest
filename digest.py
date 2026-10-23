"""YouTube digest: new videos from the channels in channels.txt, summarised by an open-source model.

Runs in GitHub Actions (.github/workflows/digest.yml). For each channel it reads YouTube's public RSS feed, takes the
videos not yet in digests/seen.json, fetches the transcript and has an open model (a hosted OpenAI-compatible endpoint
such as Groq, or local Ollama) write the investor-focused signals, AI / private-market points and key insights, plus a
cross-video "Today's signals" briefing at the top. Output:

  digests/digest_YYYY-MM-DD.md   the day's digest (one run a day; a second run appends)
  digests/digest_latest.md       copy of the last digest written
  digests/seen.json              video ids already handled, so nothing is summarised twice

No e-mail from here: send_email.py (email.yml) sends the day's digest at 7am Eastern.

Env (all optional):
  LLM_BASE_URL   OpenAI-compatible base URL      default http://localhost:11434/v1 (Ollama)
  LLM_MODEL      model name                      default qwen2.5:3b
  LLM_API_KEY    key for a hosted endpoint       default "ollama"
  LLM_FALLBACK_MODEL  local Ollama model to switch to when the hosted endpoint runs out of quota   default none
  LLM_CHUNK_WORDS     transcript words per model call   default 2500
  YT_LOOKBACK_H  first sight of a channel: only videos newer than this many hours   default 48
  YT_MAX_VIDEOS  cap per run                     default 30
  YT_BUDGET_MIN  stop starting new summaries after this many minutes; the rest wait for the next run   default 240
  YT_MIN_WORDS   shorter transcripts are skipped as Shorts/clips   default 250
  YT_LANGS       preferred caption languages     default en,en-US,en-GB
  YT_PROXY       http(s) proxy for transcript downloads when GitHub's IPs are blocked (listing goes direct)
  YT_COOKIES_FILE  cookies.txt for yt-dlp (the workflow writes it from the YT_COOKIES secret)
"""
import os, re, sys, json, glob, time, shutil, tempfile, subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import requests

HERE = ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "digests")
SEEN_FILE = os.path.join(OUT, "seen.json")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LOOKBACK_H = float(os.getenv("YT_LOOKBACK_H", "48"))
MAX_VIDEOS = int(os.getenv("YT_MAX_VIDEOS", "30"))
BUDGET_MIN = float(os.getenv("YT_BUDGET_MIN", "240"))
START = time.time()
MIN_WORDS = int(os.getenv("YT_MIN_WORDS", "250"))
LANGS = [s.strip() for s in os.getenv("YT_LANGS", "en,en-US,en-GB").split(",") if s.strip()]
PROXY = os.getenv("YT_PROXY", "")
COOKIES = os.getenv("YT_COOKIES_FILE", "")   # Netscape cookies.txt from a signed-in browser, for yt-dlp
CHUNK_WORDS = int(os.getenv("LLM_CHUNK_WORDS", "2500"))   # transcript piece per model call: fits a small local
                                                          # model's context and a hosted free tier's tokens/minute
FALLBACK_URL = os.getenv("LLM_FALLBACK_URL", "http://localhost:11434/v1").rstrip("/")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "")      # local model to switch to when a hosted one runs out of quota
MAX_WORDS = 20000         # ~2h of speech; longer transcripts are cut here

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


# ---------- channels and feeds ----------

def read_channels():
    out = []
    for line in open(os.path.join(HERE, "channels.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line: out.append(line)
    return out

def read_skip_rules():
    """[(channel or None, compiled title regex)] from skip.txt."""
    path = os.path.join(HERE, "skip.txt")
    rules = []
    if not os.path.exists(path): return rules
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line: continue
        m = re.match(r"\[([^\]]+)\]\s*(.+)", line)     # "[Channel Name] pattern" limits a rule to one channel
        chan, pat = (m.group(1), m.group(2)) if m else ("", line)
        rules.append((chan.strip().lower() or None, re.compile(pat.strip(), re.I)))
    return rules

def skipped_by_rule(v, rules):
    return any((c is None or c == v["channel"].lower()) and r.search(v["title"]) for c, r in rules)

def channel_id(spec, cache):
    """UC... id for a channel URL, @handle or id. Handle lookups are cached in seen.json."""
    m = re.search(r"(UC[\w-]{22})", spec)
    if m: return m.group(1)
    handle = re.sub(r"^https?://(www\.|m\.)?youtube\.com/", "", spec).strip("/").split("/")[0]
    if not handle.startswith("@") and not handle.startswith(("c/", "user/")): handle = "@" + handle
    if handle in cache: return cache[handle]
    r = requests.get(f"https://www.youtube.com/{handle}", headers=UA, timeout=30,
                     cookies={"CONSENT": "YES+1"})
    r.raise_for_status()
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', r.text)
         or re.search(r'"externalId":"(UC[\w-]{22})"', r.text) or re.search(r'"channelId":"(UC[\w-]{22})"', r.text))
    if not m: raise RuntimeError(f"no channel id on the {handle} page")
    cache[handle] = m.group(1)
    return m.group(1)

def feed(cid):
    """Latest uploads: the RSS feed, or the channel's Videos tab when the feed errors (it often does)."""
    try:
        return _rss(cid)
    except Exception as e:
        print(f"  rss: {e}")
    return _videos_tab(cid)

def _rss(cid):
    r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", headers=UA,
                     timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    author = root.findtext("a:author/a:name", default=cid, namespaces=NS)
    vids = []
    for e in root.findall("a:entry", NS):
        vids.append({"id": e.findtext("yt:videoId", namespaces=NS), "title": e.findtext("a:title", namespaces=NS),
                     "published": e.findtext("a:published", namespaces=NS), "channel": author,
                     "link": e.find("a:link", NS).get("href")})
    return vids

def _videos_tab(cid):
    """The channel's Videos tab through yt-dlp. approximate_date gives upload times from "3 hours ago"."""
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", "15",
           "--extractor-args", "youtubetab:approximate_date", "--quiet", "--no-warnings",
           f"https://www.youtube.com/channel/{cid}/videos"]
    p = subprocess.run(cmd, timeout=180, capture_output=True, text=True)
    if p.returncode: raise RuntimeError((p.stderr.strip().splitlines() or ["yt-dlp failed"])[-1][:200])
    data = json.loads(p.stdout)
    author = data.get("channel") or data.get("uploader") or data.get("title") or cid
    now = datetime.now(timezone.utc)
    vids = []
    for e in data.get("entries") or []:
        if not e.get("id"): continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        pub = datetime.fromtimestamp(ts, timezone.utc) if ts else now
        vids.append({"id": e["id"], "title": e.get("title") or e["id"], "channel": author, "published": pub.isoformat(),
                     "link": f"https://www.youtube.com/watch?v={e['id']}"})
    if not vids: raise RuntimeError("no videos found on the Videos tab")
    return vids

# ---------- transcripts ----------

class Blocked(Exception):
    """YouTube refused the runner (bot check / IP block), as opposed to the video having no captions."""

BLOCK_SIGNS = ("RequestBlocked", "IpBlocked", "confirm you", "not a bot", "HTTP Error 429", "Too Many Requests",
               "needs to be reloaded")

def transcript(vid, attempts=3):
    """Like _transcript_once, retrying a block or rate limit; the rotating proxy gives a new IP each try."""
    for a in range(attempts):
        try:
            return _transcript_once(vid)
        except Blocked:
            if a == attempts - 1: raise
            print(f"  blocked/rate-limited, retrying in {20 * (a + 1)}s"); time.sleep(20 * (a + 1))

def _transcript_once(vid):
    """[(start_seconds, text)] or None. Tries youtube-transcript-api, then yt-dlp's captions.
    Raises Blocked when both were refused by YouTube rather than finding no captions."""
    msgs = []
    for name, fn in (("transcript-api", _transcript_api), ("yt-dlp", _transcript_ytdlp)):
        try:
            return fn(vid)
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
            print(f"  {name}: {msg}"); msgs.append(msg)
    if all(any(b in m for b in BLOCK_SIGNS) for m in msgs): raise Blocked()
    return None

def _transcript_api(vid):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig
    http = None
    if COOKIES:     # signed-in cookies make YouTube far less likely to refuse a datacenter IP
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar(COOKIES); jar.load(ignore_discard=True, ignore_expires=True)
        http = requests.Session(); http.cookies = jar; http.headers.update(UA)
    api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=PROXY, https_url=PROXY) if PROXY else None,
                               http_client=http)
    tl = api.list(vid)
    try: t = tl.find_transcript(LANGS)
    except Exception: t = next(iter(tl))     # any language; the model writes English regardless
    return [(s.start, s.text) for s in t.fetch()]

def _transcript_ytdlp(vid):
    d = tempfile.mkdtemp()
    try:
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", "en.*,en", "--sub-format", "json3",
               "--extractor-args", "youtube:player_client=default,tv,mweb", "-o", os.path.join(d, "%(id)s.%(ext)s"),
               "--quiet", "--no-warnings", f"https://www.youtube.com/watch?v={vid}"]
        if PROXY: cmd[3:3] = ["--proxy", PROXY]
        if COOKIES: cmd[3:3] = ["--cookies", COOKIES]
        subprocess.run(cmd, check=True, timeout=120, capture_output=True, text=True)
        files = sorted(glob.glob(os.path.join(d, "*.json3")), key=lambda f: ("orig" in f, f))
        if not files: raise RuntimeError("no captions")
        events = json.load(open(files[0], encoding="utf-8")).get("events", [])
        out = [(ev.get("tStartMs", 0) / 1000, "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip())
               for ev in events if ev.get("segs")]
        return [(t, x) for t, x in out if x]
    except subprocess.CalledProcessError as e:
        raise RuntimeError((e.stderr or "").strip().splitlines()[-1] if e.stderr else "yt-dlp failed")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def mmss(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"

def with_markers(snips, every=60):
    """Transcript text with a [m:ss] marker about once a minute, so the model can point at moments."""
    parts, next_mark = [], 0
    for start, text in snips:
        if start >= next_mark:
            parts.append(f"[{mmss(start)}]"); next_mark = start + every
        parts.append(text.replace("\n", " "))
    return " ".join(parts)


# ---------- model ----------

REASONING = ("gpt-oss", "qwen3", "deepseek-r1", "magistral")     # think before answering: give them room
NOTES_RUN = []          # problems worth a line in the digest (quota hit, fallback used)
USED_MODELS = []        # models that actually wrote something this run, for the footer

class QuotaError(Exception):
    """The hosted endpoint refused for longer than is worth waiting (daily quota, request too large)."""

def llm(system, user, max_tokens=900):
    """One chat completion. A hosted model that runs out of quota hands over to FALLBACK_MODEL for the rest of the run."""
    global LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
    while True:
        try:
            text = _chat(system, user, max_tokens)
            if LLM_MODEL not in USED_MODELS: USED_MODELS.append(LLM_MODEL)
            return text
        except QuotaError as e:
            if not FALLBACK_MODEL or LLM_MODEL == FALLBACK_MODEL: raise
            print(f"  {LLM_MODEL}: {e}; switching to {FALLBACK_MODEL} for the rest of this run")
            NOTES_RUN.append(f"{LLM_MODEL} hit its free-tier limit, so later summaries are by {FALLBACK_MODEL}")
            LLM_BASE_URL, LLM_MODEL, LLM_API_KEY = FALLBACK_URL, FALLBACK_MODEL, "ollama"

def _chat(system, user, max_tokens):
    body = {"model": LLM_MODEL, "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if any(m in LLM_MODEL for m in REASONING):
        body["max_tokens"] = max_tokens * 2
        body["reasoning_effort"] = "low"
    for attempt in range(6):
        r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=1800,
                          headers={"Authorization": f"Bearer {LLM_API_KEY}"}, json=body)
        if r.status_code == 413: raise QuotaError("request too large for the tokens-per-minute limit")
        if r.status_code == 429 or r.status_code >= 500:
            try: wait = float(r.headers.get("retry-after", ""))
            except ValueError: wait = 15 * (attempt + 1)
            if wait > 120 or attempt == 5:
                raise QuotaError(f"HTTP {r.status_code}, retry in {wait:.0f}s: {r.text[:160]}")
            print(f"  HTTP {r.status_code}, waiting {wait:.0f}s"); time.sleep(wait + 1); continue
        r.raise_for_status()
        break
    choice = r.json()["choices"][0]
    text = re.sub(r"<think>.*?</think>", "", choice["message"]["content"] or "", flags=re.S).strip()   # reasoning models
    if choice.get("finish_reason") == "length" and "\n" in text:
        text = text.rsplit("\n", 1)[0]     # cut off mid-sentence: drop the unfinished line
    return text

SYSTEM = ("You brief an investor on YouTube videos they will not watch. Be concrete: who said what, names, numbers, "
          "reasons. Never invent anything that is not in the source, and never state a price move, number or ticker "
          "that is not there. Correct obvious caption misspellings of well-known names (e.g. 'Heinix' -> SK Hynix). "
          "Plain English, no hype, no generic filler.")

def read_interests():
    path = os.path.join(HERE, "interests.txt")
    if not os.path.exists(path): return ""
    return "; ".join(l.split("#", 1)[0].strip() for l in open(path, encoding="utf-8") if l.split("#", 1)[0].strip())

INTERESTS = read_interests()

RELEVANCE_PROMPT = """The reader is an investor who tracks: {interests}.

Video: "{title}" by {channel}.
Opening of the transcript:
{opening}

Is this video mainly useful to that investor: about companies, stocks, sectors, the AI or tech industry, startups,
venture capital and pre-IPO companies, or markets and the economy? Answer NO for politics or ideology without a clear
market angle, government scandals and investigations, crime, courts, history, culture, sports, celebrities, lifestyle
and personal stories, even when they come from a finance channel. Answer with one word: YES or NO."""

NOTES_PROMPT = """Part {i} of {n} of the transcript of "{title}" ({channel}). Timestamps like [12:34] mark the time.
The reader is an investor who tracks: {interests}.
Take notes on this part, one line each, starting with the timestamp, in these forms:
- [12:34] SIGNAL: bullish / cautious / mixed on <company, sector or asset> — <who holds the view> — <their reason, with numbers>
- [12:34] AI: <what is said about the AI industry: who wins or loses, spending, chips, models, adoption, pricing>
- [12:34] PRIVATE: <private or pre-IPO company: valuation, revenue, growth, funding, investors, IPO timing>
- [12:34] FACT: <another specific point an investor can use, with a number or a named source>
Only what is said in this part. Skip sponsor reads, intros, small talk and anything outside the reader's interests.
If nothing qualifies, write NONE.{focus_note}

TRANSCRIPT PART:
{text}"""

FOCUS_PROMPT = """Part {i} of {n} of the transcript of "{title}" ({channel}). Timestamps like [12:34] mark the time.
List EVERY point made in this part about: {focus}
One line each, in this form:
- [12:34] the point, in a short sentence — the reasoning or example the speaker gives
Only points actually made here; no intros, no summaries of the whole video. If there are none, write NONE.

TRANSCRIPT PART:
{text}"""

STOCKS_PROMPT = """Part {i} of {n} of the transcript of "{title}" ({channel}). Timestamps like [12:34] mark the time.
List EVERY company or stock discussed in this part (not just named in passing), one line each:
- Company (ticker only if the speaker says it) — bullish / bearish / neutral / example — what is said about it, with any numbers [12:34]
"example" = used to illustrate a point rather than as an investment view. If none, write NONE.

TRANSCRIPT PART:
{text}"""

STOCKS_MERGE_PROMPT = """Below are company mentions collected from each part of one video. Merge them into one list:
one line per company, combining what was said across the video; keep the most specific numbers and the first
[m:ss] timestamp. Fix obvious caption misspellings of well-known company names (e.g. "Dualingo" -> Duolingo).
Output only lines of the form:
- **Company** (ticker if given) — bullish / bearish / neutral / example — what was said

MENTIONS:
{mentions}"""

CHECKLIST_PROMPT = """Below are points about "{focus}", taken in order from a long video. Turn them into a checklist
a reader can use:
- 4-7 short headings in bold (e.g. **Management**, **Competitive advantage**, **Red flags**)
- under each, 2-5 checklist items, 12-25 items in total: merge duplicates, keep the specific detail or example
- drop points that are not really about "{focus}" (biography, the speaker's own firm logistics, small talk)
- end each item with the [m:ss] timestamp of the point it came from
Output only the headings and items, in Markdown, starting each item with "- ".

POINTS:
{points}"""

FINAL_PROMPT = """Video: "{title}" by {channel}, {length} long.
The reader is an investor who tracks: {interests}. Leave out everything else, even if the video covers it.
Below is {what}. Timestamps like [12:34] mark the time.

Write exactly the sections below, in Markdown, nothing before or after. Replace each <...> with your own text and never
copy the <...> instructions. Every bullet must be specific: a name, a number, a claim someone made and their reason.
No generic statements ("AI is transforming industries", "markets are volatile"); leave out points without specifics.

**Bottom line:** <one or two sentences: the most useful thing an investor learns from this video>

**Signals:**
- <one bullet per stock, sector or asset someone is bullish or cautious about: start with "🟢 Bullish", "🔴 Cautious" or "🟡 Mixed", then **name** (ticker only if said), who holds the view, why in one line with their numbers, then [m:ss]. A company only named in passing is not a signal. Write "None" if there are none>

**AI & private markets:**
- <bullets on the AI industry (who wins or loses, spending, chips, models, adoption, pricing) and on private or pre-IPO companies (valuation, revenue, funding, IPO timing), each ending with [m:ss]. Write "None" if not discussed>

**Key insights:**
- <2-4 bullets: other specific, non-obvious points with numbers, each ending with [m:ss]>

**Jump to:** <the 2-3 most valuable moments as "[m:ss] topic", separated by " · ">
{focus_block}
{content_label}:
{content}"""

OVERVIEW_PROMPT = """Below are today's video summaries for an investor who tracks: {interests}.
Write a short briefing on what was said across them, in Markdown, nothing before or after:

**Themes:**
- <2-4 bullets: views or trends that came up today, above all on AI and on private / pre-IPO companies; each ends with the source videos in parentheses, in short form>

**Bullish on:**
- <one bullet per company, sector or asset with a bullish view: **name** — the reason in a few words (source)>

**Cautious on:**
- <the same for cautious views>

**Private & pre-IPO watch:**
- <private companies discussed: **name** — valuation, revenue, funding or IPO news (source)>

Use only what is in the summaries; merge duplicates; be specific. Leave out a section, heading included, if it
would be empty.

SUMMARIES:
{summaries}"""

ON_TOPIC_WORDS = re.compile(
    r"\b(stocks?|shares?|earnings|market|markets|econom\w*|fed|central bank|rates?|inflation|recession|bonds?|yields?|"
    r"tariffs?|trade|budget|tax\w*|gdp|jobs|dollar|currenc\w+|oil|gold|crypto|bitcoin|ipo|valuation|invest\w*|"
    r"funds?|vc|venture|startups?|ai|chips?|semiconductor\w*|nvidia|tech)\b", re.I)

def on_topic(v, snips):
    """Ask the model on the title + opening ~400 words; only a clear NO drops the video. With the small local model,
    market words in the title keep a video outright."""
    if not INTERESTS: return True
    if LLM_API_KEY == "ollama" and ON_TOPIC_WORDS.search(v["title"]): return True    # small local models misjudge
    opening = " ".join(" ".join(t for _, t in snips).split()[:400])
    answer = llm(SYSTEM, RELEVANCE_PROMPT.format(interests=INTERESTS, title=v["title"], channel=v["channel"],
                                                  opening=opening), max_tokens=200)
    return not re.sub(r"\W", "", answer).upper().startswith("NO")

def summarise(v, snips, focus=""):
    fb = fn = ""    # the focus list is extracted part by part (see below), not squeezed into the final answer
    words = with_markers(snips).split()
    cut = len(words) > MAX_WORDS
    words = words[:MAX_WORDS]
    length = mmss(snips[-1][0]) if snips else "?"
    if len(words) <= CHUNK_WORDS * 1.2:
        body = llm(SYSTEM, FINAL_PROMPT.format(title=v["title"], channel=v["channel"], length=length, interests=INTERESTS,
                                               focus_block=fb, what="the full transcript", content_label="TRANSCRIPT",
                                               content=" ".join(words)), max_tokens=2000 if focus else 1300)
    else:
        chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
        notes = []
        for i, c in enumerate(chunks, 1):
            print(f"  part {i}/{len(chunks)}")
            notes.append(llm(SYSTEM, NOTES_PROMPT.format(i=i, n=len(chunks), title=v["title"], interests=INTERESTS, focus_note=fn,
                                                         channel=v["channel"], text=c), max_tokens=900 if focus else 700))
        body = llm(SYSTEM, FINAL_PROMPT.format(title=v["title"], channel=v["channel"], length=length, interests=INTERESTS,
                                               focus_block=fb, what="notes taken while reading the transcript in parts",
                                               content_label="NOTES", content="\n".join(notes)), max_tokens=2000 if focus else 1300)
    if focus:     # one-off deep dives: stocks and the focus list are collected part by part, not left to one answer
        body = re.sub(r"\n*\*\*Stocks mentioned:\*\*.*?(?=\n\*\*[A-Z]|\Z)", "", body, flags=re.S)
        body += "\n\n" + stocks_list(v, words) + "\n\n" + focus_list(v, words, focus)
    body = link_timestamps(cap_bullets(tidy(body)), v["id"], snips[-1][0] if snips else 0)
    if cut: body += f"\n\n_Summary covers the first ~{MAX_WORDS:,} words of the transcript._"
    return body

def stocks_list(v, words):
    """Every company discussed, gathered from each part of the transcript and merged into one line per company."""
    chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
    mentions = []
    for i, c in enumerate(chunks, 1):
        print(f"  stocks {i}/{len(chunks)}")
        out = llm(SYSTEM, STOCKS_PROMPT.format(i=i, n=len(chunks), title=v["title"], channel=v["channel"], text=c),
                  max_tokens=700)
        mentions += [l.strip() for l in out.split("\n") if re.match(r"\s*(?:[-*•]|\d+[.)]|\|)", l)
                     and "none" not in l.lower()[:8] and not re.match(r"\s*\|[\s:|-]+\|?\s*$", l)]
    if not mentions: return "**Stocks mentioned:** none discussed."
    merged = llm(SYSTEM, STOCKS_MERGE_PROMPT.format(mentions="\n".join(mentions)), max_tokens=1200)
    lines = [re.sub(r"^\s*(?:[*•]|\d+[.)])\s*", "- ", l) for l in merged.split("\n") if re.match(r"\s*(?:[-*•]|\d+[.)])", l)]
    if len(lines) < len(mentions) // 3: lines = mentions      # the merge lost most of the list: keep the raw mentions
    return "**Stocks mentioned:**\n" + "\n".join(lines)

def focus_list(v, words, focus):
    """Every point about `focus`, collected from each part of the transcript so a long video is covered end to end."""
    chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
    points, seen_pts = [], set()
    for i, c in enumerate(chunks, 1):
        print(f"  focus {i}/{len(chunks)}")
        out = llm(SYSTEM, FOCUS_PROMPT.format(i=i, n=len(chunks), title=v["title"], channel=v["channel"],
                                              focus=focus, text=c), max_tokens=900)
        for line in out.split("\n"):
            line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            key = re.sub(r"\W+", " ", re.sub(r"\[[\d:]+\]", "", line)).strip().lower()
            if len(key) < 15 or key.startswith("none") or key in seen_pts: continue
            seen_pts.add(key); points.append(line)
    head = f"**{focus[0].upper() + focus[1:]}:**"
    if not points: return head + "\n\n_Nothing specific on this in the video._"
    raw = "\n".join(f"{n}. {p}" for n, p in enumerate(points, 1))
    if len(points) < 12: return head + "\n" + raw
    try:
        grouped = llm(SYSTEM, CHECKLIST_PROMPT.format(focus=focus, points=raw), max_tokens=1800)
    except Exception as e:
        print(f"  checklist step failed ({e}); keeping the raw list"); grouped = ""
    if grouped.count("\n") < 5: return head + "\n" + raw
    return f"{head}\n\n{grouped}\n\n_Condensed from {len(points)} points taken across the whole video._"

def tidy(md):
    """Undo template echoes: drop copied <...> instructions and pull the text up next to its **Label:**."""
    md = re.sub(r"<[^<>\n]{12,}>", "", md)
    md = re.sub(r"\[m{1,2}:ss\]:?\s*-?\s*", "", md)     # literal "[m:ss]" copied from the template
    md = re.sub(r"(\*\*[^*\n]+:\*\*[^\n]*?)[ \t]*\n\s*\n(?=[^\s*\-])", lambda m: m.group(1).rstrip() + " ", md)
    md = re.sub(r"(\d/5)\s*—\s*(?=\S)", r"\1 — ", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()

CAPS = {"Signals": 10, "Stocks mentioned": 99, "AI & private markets": 6}

def cap_bullets(md, keep=5):
    """Models sometimes ignore "2-4 bullets" on long videos; keep the per-section cap, spread across the video."""
    md = re.sub(r"(?m)^\s*[-*]\s*(?:None|N/A)\b[^\n]*\n?", "", md)       # "- None" next to real bullets
    lines, out, block = md.split("\n"), [], []
    def flush():
        under = next((l for l in reversed(out) if l.strip()), "")
        head = re.match(r"\*\*([^*:]+):?\*\*", under)
        cap = CAPS.get(head.group(1).strip(), keep) if head else keep
        if len(block) > cap:
            step = (len(block) - 1) / (cap - 1)
            block[:] = [block[round(k * step)] for k in range(cap)]
        out.extend(block); block.clear()
    for l in lines:
        if l.lstrip().startswith(("- ", "* ")): block.append(l)
        else: flush(); out.append(l)
    flush()
    md = "\n".join(out)
    # a section that says "None" (or is empty) adds nothing
    md = re.sub(r"\n*\*\*[^*\n]+:\*\*\s*(?:\n\s*[-*]\s*)?(?:None|N/A|-)?\.?\s*(?=\n\*\*|\Z)", "", md, flags=re.I)
    md = re.sub(r"\n+(?=\*\*[^*\n]+:\*\*)", "\n\n", md)      # keep a blank line before every section
    return md.strip()

def day_overview(entries):
    """A cross-video briefing for the top of the e-mail; empty when there is too little to compare."""
    if len(entries) < 2: return ""
    parts = []
    for v, _, body in entries:
        body = re.sub(r"\[(\d[\d:]*)\]\([^)]*\)", r"[\1]", body)      # drop the timestamp links
        body = re.sub(r"\*\*Jump to:\*\*.*", "", body)
        parts.append(f"### {v['title']} ({v['channel']})\n{body[:1800]}")
    try:
        out = llm(SYSTEM, OVERVIEW_PROMPT.format(interests=INTERESTS, summaries="\n\n".join(parts)), max_tokens=1000)
    except Exception as e:
        print(f"overview failed: {e}"); return ""
    return cap_bullets(tidy(out))

def link_timestamps(md, vid, last):
    """[12:34] -> a link to that moment. Times past the end of the video (model slips) are dropped."""
    def rep(m):
        parts = [int(p) for p in m.group(1).split(":")]
        sec = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
        if sec > last + 30: return ""
        return f"[{m.group(1)}](https://youtu.be/{vid}?t={sec})"
    return re.sub(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\](?!\()", rep, md)


# ---------- main ----------

def main():
    os.makedirs(OUT, exist_ok=True)
    state = json.load(open(SEEN_FILE)) if os.path.exists(SEEN_FILE) else {}
    seen, handles = set(state.get("seen", [])), state.get("handles", {})
    known_channels = set(state.get("channels", []))
    now = datetime.now(timezone.utc)

    todo, errors = [], []
    skip_rules = read_skip_rules()
    for spec in read_channels():
        try:
            cid = channel_id(spec, handles)
            vids = feed(cid)
        except Exception as e:
            print(f"{spec}: {e}"); errors.append(f"{spec}: could not read the channel ({str(e)[:100]})"); continue
        # a channel seen for the first time only contributes recent videos, not its whole feed
        cutoff = now - timedelta(hours=LOOKBACK_H) if cid not in known_channels else now - timedelta(days=30)
        for v in vids:     # skip.txt: never summarise these, never list them
            if v["id"] not in seen and skipped_by_rule(v, skip_rules):
                print(f"  skip (skip.txt): {v['title']}"); seen.add(v["id"])
        new = [v for v in vids if v["id"] not in seen and v["id"] not in {t["id"] for t in todo}
               and datetime.fromisoformat(v["published"]) >= cutoff]
        if cid not in known_channels:
            seen.update(v["id"] for v in vids if v not in new)
        print(f"{spec} ({vids[0]['channel'] if vids else cid}): {len(new)} new")
        todo += new
        known_channels.add(cid)
    todo.sort(key=lambda v: v["published"])
    if len(todo) > MAX_VIDEOS:
        print(f"{len(todo)} new videos; doing the newest {MAX_VIDEOS}, the rest next run")
        todo = todo[-MAX_VIDEOS:]

    entries, skipped, n_blocked, off_topic = [], [], 0, []
    todo.reverse()     # newest first, so a run that hits the time budget leaves the older ones
    for v in todo:
        if time.time() - START > BUDGET_MIN * 60:
            left = len(todo) - todo.index(v)
            print(f"\ntime budget reached; {left} video(s) left for the next run")
            errors.append(f"{left} video(s) held over to the next run (time budget)"); break
        print(f"\n{v['channel']}: {v['title']} ({v['id']})")
        try:
            snips = transcript(v["id"])
        except Blocked:
            n_blocked += 1; continue
        if snips is None:
            # live streams and fresh uploads get captions later: retry next run, give up after 3 days
            if now - datetime.fromisoformat(v["published"]) > timedelta(days=3):
                seen.add(v["id"]); skipped.append((v, "no transcript"))
            continue
        n_words = sum(len(t.split()) for _, t in snips)
        if n_words < MIN_WORDS:
            print(f"  {n_words} words, skipping (Short/clip)"); seen.add(v["id"]); continue
        t0 = time.time()
        try:
            if not on_topic(v, snips):
                print("  off-topic, skipping"); seen.add(v["id"]); off_topic.append(v); continue
            body = summarise(v, snips)
        except Exception as e:
            print(f"  model failed: {e}"); errors.append(f"model failed on {v['title']}"); continue
        print(f"  summarised {n_words} words in {time.time() - t0:.0f}s")
        entries.append((v, mmss(snips[-1][0]), body))
        seen.add(v["id"])

    if n_blocked:
        errors.append(f"YouTube blocked transcript downloads for {n_blocked} video(s); they are retried next run "
                      "(set the YT_PROXY or YT_COOKIES secret, see README.md)")
    state = {"seen": sorted(seen)[-5000:], "handles": handles, "channels": sorted(known_channels)}
    json.dump(state, open(SEEN_FILE, "w"), indent=1)

    if not entries and not skipped and not errors and not off_topic:
        print("\nnothing new to write"); return
    today = now.strftime("%Y-%m-%d")
    path = os.path.join(OUT, f"digest_{today}.md")
    md = []
    if not os.path.exists(path):
        md.append(f"# YouTube digest — {today}\n")
    else:
        md.append(f"\n<!-- run {now.strftime('%H:%MZ')} -->\n")
    overview = day_overview(entries)
    if overview:
        md.append(f"## Today's signals\n\n{overview}\n")
    for v, length, body in entries:
        pub = datetime.fromisoformat(v["published"]).strftime("%b %d")
        md.append(f"## {v['title']}\n\n**{v['channel']}** · {length} · {pub} · [watch]({v['link']})\n\n{body}\n")
    if off_topic:
        md.append("## Skipped as off-topic\n")
        md += [f"- [{v['title']}]({v['link']}) — {v['channel']}" for v in off_topic]
        md.append("")
    if skipped:
        md.append("## No transcript available\n")
        md += [f"- [{v['title']}]({v['link']}) — {v['channel']}" for v, _ in skipped]
    errors += NOTES_RUN
    if errors:
        md.append("\n_Problems this run: " + "; ".join(errors) + "_")
    md.append(f"\n_Summaries by {' and '.join(USED_MODELS) or LLM_MODEL} (open-source models)._\n")
    with open(path, "a", encoding="utf-8") as f: f.write("\n".join(md))
    shutil.copy(path, os.path.join(OUT, "digest_latest.md"))
    print(f"\nwrote {path}: {len(entries)} summaries, {len(skipped)} without transcript")


def find_video(spec):
    """A watch/youtu.be URL, an 11-character id, or search text -> {id, title, channel, link}."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", spec) or re.fullmatch(r"\s*([\w-]{11})\s*", spec)
    if m:
        vid = m.group(1)
        r = requests.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}",
                                                                   "format": "json"}, headers=UA, timeout=30)
        meta = r.json() if r.ok else {}
        title, channel = meta.get("title", vid), meta.get("author_name", "")
    else:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "--flat-playlist", "--dump-single-json", "--quiet",
                            "--no-warnings", f"ytsearch1:{spec}"], capture_output=True, text=True, timeout=120)
        hits = (json.loads(p.stdout).get("entries") or []) if p.returncode == 0 and p.stdout else []
        if not hits: raise SystemExit(f"no YouTube result for: {spec}")
        vid, title, channel = hits[0]["id"], hits[0].get("title", ""), hits[0].get("channel") or hits[0].get("uploader", "")
    return {"id": vid, "title": title, "channel": channel, "link": f"https://www.youtube.com/watch?v={vid}"}

def one_video(spec, focus):
    """Summarise a single video on request (video.yml); writes digests/video_<id>.md and video_latest.md."""
    os.makedirs(OUT, exist_ok=True)
    v = find_video(spec)
    print(f"{v['channel']}: {v['title']} ({v['id']})")
    try:
        snips = transcript(v["id"])
    except Blocked:
        raise SystemExit("YouTube blocked the transcript download; check the YT_PROXY secret")
    if not snips: raise SystemExit("this video has no captions to summarise")
    global LLM_MODEL
    models = [m.strip() for m in os.getenv("YT_MODELS", "").split(",") if m.strip()] or [LLM_MODEL]
    md = f"# {v['title']}\n\n**{v['channel']}** · {mmss(snips[-1][0])} · [watch]({v['link']})\n\n"
    for m in models:
        LLM_MODEL = m
        print(f"\n== {m}")
        t0 = time.time()
        try:
            body = summarise(v, snips, focus)
        except Exception as e:
            body = f"_{m} failed: {e}_"
        took = time.time() - t0
        print(f"  {m}: {took / 60:.1f} min")
        if len(models) > 1:
            md += f"---\n\n## Model: {m}  ({took / 60:.0f} min)\n\n{body}\n\n"
        else:
            md += f"{body}\n\n_Summary by {m} (open-source), {took / 60:.0f} min._\n"
    names = [os.getenv("YT_OUT")] if os.getenv("YT_OUT") else [f"video_{v['id']}.md", "video_latest.md"]
    for name in names:
        open(os.path.join(OUT, name), "w", encoding="utf-8").write(md)
    print(md)

if __name__ == "__main__":
    if os.getenv("YT_VIDEO"):
        one_video(os.environ["YT_VIDEO"], os.getenv("YT_FOCUS", "").strip())
    else:
        main()
