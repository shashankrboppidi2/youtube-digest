"""Pull the investor signals out of digest Markdown: who was bullish or cautious on which stock or sector, and which
companies came up in the AI / private-market notes. Used for the daily TL;DR (digest.py) and the monthly and
quarterly reports (report.py). Understands both the current format ("**Signals:**" with 🟢/🔴/🟡 bullets) and the
older one ("**Stocks mentioned:**" with "bullish / bearish" in the line).
"""
import re
from collections import Counter, defaultdict

SPECIAL = ("TL;DR", "Today's signals", "Skipped as off-topic", "No transcript available")
EMOJI = {"🟢": "bullish", "🔴": "cautious", "🟡": "mixed"}
WORDS = {"bullish": "bullish", "positive": "bullish", "bearish": "cautious", "cautious": "cautious",
         "negative": "cautious", "mixed": "mixed"}
SECTION = re.compile(r"^\*\*(Signals|Stocks mentioned|AI & private markets|Moats, risks & black swans|Key insights|"
                     r"Bottom line|Key takeaways|TL;DR):?\*\*:?\s*(.*)$")
STOP = re.compile(r"\b(?:inc|corp|corporation|co|ltd|plc|stock|stocks|shares|the|holdings|group)\b\.?", re.I)


def videos(md, date=""):
    """[{title, channel, link, date, signals: [(sentiment, name, text)], notes, risks, insights: [text], bottom}]
    for each video in a digest."""
    out = []
    for block in re.split(r"(?m)^## ", md)[1:]:
        title, _, body = block.partition("\n")
        if title.strip().startswith(SPECIAL): continue
        m = re.search(r"^\*\*(.+?)\*\* · .*?\[watch\]\(([^)]+)\)", body, re.M)
        v = {"title": title.strip(), "channel": m.group(1) if m else "", "link": m.group(2) if m else "",
             "date": date, "signals": [], "notes": [], "risks": [], "insights": [], "bottom": ""}
        section = None
        for line in body.split("\n"):
            h = SECTION.match(line.strip())
            if h:
                section = h.group(1)
                if section in ("Bottom line", "TL;DR"): v["bottom"] = untime(h.group(2)); section = None
                continue
            if line.startswith("**"): section = None; continue
            if not section or not line.lstrip().startswith(("- ", "* ")): continue
            text = line.strip()[2:].strip()
            if section == "AI & private markets":
                v["notes"].append(text)
            elif section == "Moats, risks & black swans":
                v["risks"].append(untime(text))
            elif section in ("Key insights", "Key takeaways"):
                v["insights"].append(untime(text))
            else:
                s = signal(text)
                if s: v["signals"].append(s)
        out.append(v)
    return out


def untime(text):
    """Drop the [m:ss](link) timestamp links, which only cost tokens in prompts."""
    return re.sub(r"\s*\[(\d[\d:]*)\]\([^)]*\)", "", text).strip()


def signal(text):
    """(sentiment, name, text) from one bullet, or None when it states no view."""
    t = re.sub(r"\[(\d[\d:]*)\]\([^)]*\)", "", text)      # timestamp links
    sent = next((s for e, s in EMOJI.items() if t.startswith(e)), None)
    if sent:
        t = t[1:].strip()
        t = re.sub(r"^\**\s*(bullish|cautious|bearish|mixed)\s*\**\s*[–—:-]*\s*", "", t, flags=re.I)
    else:     # older format: "**Company** (ticker) — bullish / bearish / neutral — what was said"
        w = re.search(r"\b(bullish|bearish|cautious|positive|negative|mixed)\b", t[:120], re.I)
        if not w: return None
        sent = WORDS[w.group(1).lower()]
    b = re.match(r"\s*\*\*(.+?)\*\*", t)
    name = b.group(1) if b else re.split(r"\s+[–—-]\s+|:", t, 1)[0]
    name = re.sub(r"\s*\(.*$", "", name).strip(" *.,;")      # "(NVDA)", "(ticker: ..." 
    if not name or len(name) > 60: return None
    return sent, name, text


def key(name):
    n = re.sub(r"\(.*?(\)|$)", "", name)
    n = STOP.sub("", re.sub(r"[*$`'’]", "", n))
    return re.sub(r"\s+", " ", n).strip(" .,-–—:/").lower()


def tally(vids):
    """{key: {"name", "bullish", "cautious", "mixed" (sets of video links), "channels", "samples"}}"""
    t = defaultdict(lambda: {"names": Counter(), "bullish": set(), "cautious": set(), "mixed": set(),
                             "channels": set(), "samples": []})
    for v in vids:
        for sent, name, text in v["signals"]:
            k = key(name)
            if not k: continue
            e = t[k]
            e["names"][name] += 1
            e[sent].add(v["link"] or v["title"])
            e["channels"].add(v["channel"])
            e["samples"].append((sent, text, v["title"], v["channel"], v["date"]))
    for e in t.values():
        e["name"] = re.sub(r"\s*\(.*$", "", e["names"].most_common(1)[0][0]).strip()
    return t


def ranked(t, sent, n=10):
    """Names with the most videos holding `sent` (then the most channels), as [(key, entry)]."""
    rows = [(k, e) for k, e in t.items() if e[sent]]
    rows.sort(key=lambda r: (-len(r[1][sent]), -len(r[1]["channels"]), r[1]["name"]))
    return rows[:n]


def short_channel(c):
    return re.sub(r"\s*[:|(].*$", "", c).replace(" with Molly O'Shea", "").replace(" with Harry Stebbings", "").strip()


def counts_line(t, sent, n=8):
    """'Nvidia 3 (BG2, TheStreet); Micron 2 (...)' for prompts and the fallback TL;DR."""
    return "; ".join(f"{e['name']} {len(e[sent])} ({', '.join(sorted(short_channel(c) for c in e['channels']))})"
                     for _, e in ranked(t, sent, n)) or "none"


def private_candidates(vids, n=40):
    """Companies named in bold in the AI & private-market notes (where private companies come up), most mentioned
    first, with up to three snippets each. Public ones are filtered out later by the model."""
    t = defaultdict(lambda: {"names": Counter(), "videos": set(), "snippets": [], "notes": 0})
    for v in vids:
        named = [(n, note, 1) for note in v["notes"] for n in re.findall(r"\*\*(.+?)\*\*", note)]
        named += [(name, text, 0) for _, name, text in v["signals"]]     # older digests have no AI section
        for name, note, from_notes in named:
            k = key(name)
            if not k or len(k) > 40: continue
            e = t[k]
            e["names"][name] += 1
            e["notes"] += from_notes
            e["videos"].add(v["link"] or v["title"])
            if len(e["snippets"]) < 2: e["snippets"].append(f"{untime(note)[:200]} ({short_channel(v['channel'])}, {v['date']})")
    # names from the AI / private-market notes first: that is where private companies come up
    rows = sorted(t.items(), key=lambda r: (-(r[1]["notes"] > 0), -len(r[1]["videos"])))[:n]
    for _, e in rows: e["name"] = re.sub(r"\s*\(.*$", "", e["names"].most_common(1)[0][0]).strip()
    return rows
