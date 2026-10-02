"""YouTube digest: new videos from the channels in channels.txt, summarised by an open-source model.

Runs in GitHub Actions (.github/workflows/digest.yml). For each channel it reads YouTube's public RSS feed, takes the
videos not yet in digests/seen.json, fetches the transcript and has a local model (Ollama, any
OpenAI-compatible endpoint works) write a TL;DR, key points and a watch-or-skip verdict. Output:

  digests/digest_YYYY-MM-DD.md   the day's digest (one run a day; a second run appends)
  digests/digest_latest.md       copy of the last digest written
  digests/seen.json              video ids already handled, so nothing is summarised twice

No e-mail from here: send_email.py (email.yml) sends the day's digest at 7am Eastern.

Env (all optional):
  LLM_BASE_URL   OpenAI-compatible base URL      default http://localhost:11434/v1 (Ollama)
  LLM_MODEL      model name                      default qwen2.5:3b
  LLM_API_KEY    key for a hosted endpoint       default "ollama"
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
CHUNK_WORDS = 2500        # transcript piece per model call; fits a 3-4B model's context with room to answer
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

def llm(system, user, max_tokens=900):
    body = {"model": LLM_MODEL, "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if any(m in LLM_MODEL for m in REASONING):
        body["max_tokens"] = max_tokens * 4
        body["reasoning_effort"] = "low"
    r = requests.post(f"{LLM_BASE_URL}/chat/completions", timeout=1800,
                      headers={"Authorization": f"Bearer {LLM_API_KEY}"}, json=body)
    r.raise_for_status()
    choice = r.json()["choices"][0]
    text = re.sub(r"<think>.*?</think>", "", choice["message"]["content"] or "", flags=re.S).strip()   # reasoning models
    if choice.get("finish_reason") == "length" and "\n" in text:
        text = text.rsplit("\n", 1)[0]     # cut off mid-sentence: drop the unfinished line
    return text

SYSTEM = ("You condense YouTube videos for a busy reader who will not watch them. Be concrete: names, numbers, "
          "claims, conclusions. Never invent anything that is not in the transcript. Plain English, no hype.")

def read_interests():
    path = os.path.join(HERE, "interests.txt")
    if not os.path.exists(path): return ""
    return "; ".join(l.split("#", 1)[0].strip() for l in open(path, encoding="utf-8") if l.split("#", 1)[0].strip())

INTERESTS = read_interests()

RELEVANCE_PROMPT = """The reader follows: {interests}.
The reader does NOT want: general news, human-interest stories, accidents, crime, sports, celebrities, lifestyle.

Video: "{title}" by {channel}.
Opening of the transcript:
{opening}

Answer NO only if the video is clearly about something else (crime, courts, accidents, sports, celebrities,
entertainment, lifestyle). Anything about companies, stocks, earnings, markets, the economy, central banks, trade,
government policy or technology is YES. Answer with one word: YES or NO."""

NOTES_PROMPT = """Part {i} of {n} of the transcript of "{title}" ({channel}). Timestamps like [12:34] mark the time.
The reader follows: {interests}.
Write 3-5 terse bullet points with the most substantive points made in this part that matter to the reader
(facts, arguments, numbers, advice). Put the timestamp at the start of each, e.g. "- [12:34] ...".
When a company or stock is discussed, name it (ticker only if the speaker says it) and the view and numbers given.
Leave out sponsor reads, intros, calls to subscribe and segments the reader doesn't follow (human-interest, sports...).{focus_note}

TRANSCRIPT PART:
{text}"""

FOCUS_PROMPT = """Part {i} of {n} of the transcript of "{title}" ({channel}). Timestamps like [12:34] mark the time.
List EVERY point made in this part about: {focus}
One line each, in this form:
- [12:34] the point, in a short sentence — the reasoning or example the speaker gives
Only points actually made here; no intros, no summaries of the whole video. If there are none, write NONE.

TRANSCRIPT PART:
{text}"""

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
The reader follows: {interests}. Leave out anything else, even if the video covers it.
Below is {what}. Timestamps like [12:34] mark the time.

Write exactly this, in Markdown, nothing before or after. Replace each <...> with your own text; never copy the
<...> instructions or the angle brackets into your answer:

**TL;DR:** <2-3 sentences: what the video is about and its main conclusion>

**Worth watching in full?** <score>/5 — <one sentence why; 5 = the summary cannot replace it, 1 = the summary covers it>

**Bookmarks:**
- <3-6 bullets in time order, each: [m:ss] short section title — one line on what is covered there>

**Key takeaways:**
- <4-6 bullets: the most important specific points of the whole video, with numbers>

**Stocks mentioned:**
- <one bullet per company or stock actually discussed: Company (ticker only if the speaker says it; never guess one) — bullish / bearish / neutral — the view and any numbers (targets, valuation, growth). Write only "None" if no specific company was discussed>
{focus_block}
{content_label}:
{content}"""

ON_TOPIC_WORDS = re.compile(
    r"\b(stocks?|shares?|earnings|market|markets|econom\w*|fed|central bank|rates?|inflation|recession|bonds?|yields?|"
    r"tariffs?|trade|budget|tax\w*|gdp|jobs|dollar|currenc\w+|oil|gold|crypto|bitcoin|ipo|valuation|invest\w*|"
    r"funds?|vc|venture|startups?|ai|chips?|semiconductor\w*|nvidia|tech)\b", re.I)

def on_topic(v, snips):
    """Cheap first look: market/economy words in the title keep a video outright; otherwise ask the model on the
    title + opening ~400 words, and only a clear NO drops it."""
    if not INTERESTS or ON_TOPIC_WORDS.search(v["title"]): return True
    opening = " ".join(" ".join(t for _, t in snips).split()[:400])
    answer = llm(SYSTEM, RELEVANCE_PROMPT.format(interests=INTERESTS, title=v["title"], channel=v["channel"],
                                                  opening=opening), max_tokens=5)
    return not answer.strip().upper().startswith("NO")

def summarise(v, snips, focus=""):
    fb = fn = ""    # the focus list is extracted part by part (see below), not squeezed into the final answer
    words = with_markers(snips).split()
    cut = len(words) > MAX_WORDS
    words = words[:MAX_WORDS]
    length = mmss(snips[-1][0]) if snips else "?"
    if len(words) <= CHUNK_WORDS * 1.2:
        body = llm(SYSTEM, FINAL_PROMPT.format(title=v["title"], channel=v["channel"], length=length, interests=INTERESTS,
                                               focus_block=fb, what="the full transcript", content_label="TRANSCRIPT",
                                               content=" ".join(words)), max_tokens=2000 if focus else 1100)
    else:
        chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
        notes = []
        for i, c in enumerate(chunks, 1):
            print(f"  part {i}/{len(chunks)}")
            notes.append(llm(SYSTEM, NOTES_PROMPT.format(i=i, n=len(chunks), title=v["title"], interests=INTERESTS, focus_note=fn,
                                                         channel=v["channel"], text=c), max_tokens=700 if focus else 500))
        body = llm(SYSTEM, FINAL_PROMPT.format(title=v["title"], channel=v["channel"], length=length, interests=INTERESTS,
                                               focus_block=fb, what="notes taken while reading the transcript in parts",
                                               content_label="NOTES", content="\n".join(notes)), max_tokens=2000 if focus else 1100)
    if focus:
        body += "\n\n" + focus_list(v, words, focus)
    body = link_timestamps(cap_bullets(tidy(body)), v["id"], snips[-1][0] if snips else 0)
    if cut: body += f"\n\n_Summary covers the first ~{MAX_WORDS:,} words of the transcript._"
    return body

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

def cap_bullets(md, keep=7):
    """Small models ignore "4-6 bullets" on long videos; keep `keep` per list, spread across the video."""
    lines, out, block = md.split("\n"), [], []
    def flush():
        if len(block) > keep:
            step = (len(block) - 1) / (keep - 1)
            block[:] = [block[round(k * step)] for k in range(keep)]
        out.extend(block); block.clear()
    for l in lines:
        if l.lstrip().startswith(("- ", "* ")): block.append(l)
        else: flush(); out.append(l)
    flush()
    md = "\n".join(out)
    # "Stocks mentioned: None" (or an empty list) adds nothing
    return re.sub(r"\n*\*\*Stocks mentioned:\*\*\s*(?:\n\s*[-*]\s*)?(?:None|N/A|-)?\.?\s*(?=\n\*\*|\Z)", "", md, flags=re.I).strip()

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
    if errors:
        md.append("\n_Problems this run: " + "; ".join(errors) + "_")
    md.append(f"\n_Summaries by {LLM_MODEL} (open-source) running in GitHub Actions._\n")
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
