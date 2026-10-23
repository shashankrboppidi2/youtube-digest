"""Monthly and quarterly signals report, built from the daily digests (run by .github/workflows/report.yml).

Counts which stocks and sectors the videos were most bullish and most cautious on, lists the private / pre-IPO
companies that came up, and has the model write the trends, moats, risks and black swans of the period. Writes
digests/reports/<month_YYYY-MM | quarter_YYYY-Qn>.md and e-mails it once (digests/reports/sent.json).

Env:
  REPORT_PERIOD  month | quarter | auto (default: last month, plus last quarter in Jan/Apr/Jul/Oct)
  REPORT_TO_DATE 1 = the current month / quarter so far instead of the last full one (a preview)
  FORCE          1 = e-mail even if this report was sent already
  NO_EMAIL       1 = write the file only
  plus the LLM_* settings of digest.py and the SMTP_* settings of send_email.py
"""
import os, re, glob, json
from datetime import date, timedelta
import digest, signals

OUT = os.path.join(digest.OUT, "reports")
SENT = os.path.join(OUT, "sent.json")
MONTHS = "January February March April May June July August September October November December".split()


def period(kind, today, to_date):
    """(start, end exclusive, label, slug) of the last full month/quarter, or of the current one so far."""
    if kind == "month":
        cur = today.replace(day=1)
        start = cur if to_date else (cur - timedelta(days=1)).replace(day=1)
        end = today + timedelta(days=1) if to_date else cur
        label = f"{MONTHS[start.month - 1]} {start.year}"
        return start, end, label, f"month_{start:%Y-%m}"
    q0 = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    start = q0 if to_date else date(q0.year - (q0.month == 1), (q0.month - 4) % 12 + 1, 1)
    end = today + timedelta(days=1) if to_date else q0
    q = (start.month - 1) // 3 + 1
    return start, end, f"Q{q} {start.year}", f"quarter_{start.year}-Q{q}"


def load(start, end):
    vids = []
    for f in sorted(glob.glob(os.path.join(digest.OUT, "digest_*.md"))):
        m = re.search(r"digest_(\d{4}-\d{2}-\d{2})\.md$", f)
        if m and start <= date.fromisoformat(m.group(1)) < end:
            vids += signals.videos(open(f, encoding="utf-8").read(), m.group(1))
    return vids


def sample(lines, n):
    """At most n lines, spread evenly over the period, duplicates dropped."""
    seen, uniq = set(), []
    for l in lines:
        k = re.sub(r"\W+", " ", l.lower())[:80]
        if k not in seen: seen.add(k); uniq.append(l)
    if len(uniq) <= n: return uniq
    step = len(uniq) / n
    return [uniq[int(i * step)] for i in range(n)]


WHY_PROMPT = """Investor views collected from {n} YouTube videos ({label}) on AI, tech, private markets and markets.
Each numbered item is a stock, sector or asset, with how many videos were bullish / cautious on it and sample views.

{items}

Write, in plain text, nothing else:
BOTTOM LINE: <2-3 sentences: where the consensus was this period and what changed>
then one line per item, in this form:
<number>: <why the videos were bullish or cautious on it, at most 20 words, specific (numbers, catalysts)>
Take the reasons only from the sample views; if they give none, write "no reason given". Don't repeat the name."""

PRIVATE_PROMPT = """Below are companies named in the AI and private-market notes of {n} YouTube videos ({label}), most
mentioned first, with what was said about them.

{items}

Pick the companies that are PRIVATE (not listed on a stock exchange) and look like fast growers or sit in a
fast-growing sector. Leave out public companies: any you know to be listed (e.g. CoreWeave, Nvidia), and any the
notes say has listed (stock price, earnings, ticker, completed IPO). Up to 15, the most promising first. One line each, in exactly this form, nothing else:
Name | sector | what the videos said: valuation, revenue, growth, funding, investors | IPO timing if mentioned, else -
Use only facts from the notes; write - for anything not mentioned."""

THEMES_PROMPT = """Below are notes from {n} YouTube videos ({label}) for an investor who tracks AI, private / pre-IPO
companies, tech stocks and market-moving macro.

BOTTOM LINES AND KEY POINTS:
{insights}

MOATS, RISKS AND BLACK SWANS RAISED:
{risks}

Write these sections in Markdown, nothing before or after. Specific, no generic filler; merge repeats; where a point
came up in several videos, say so:

**Trends:**
- <4-6 bullets: the trends of the period, above all in AI and private markets, with evidence>

**Moats:**
- <up to 6 bullets: **company** — the durable advantage the videos credited it with>

**Risks:**
- <up to 6 bullets: the risks raised most, and what they would hit>

**Black swans:**
- <up to 4 bullets: unlikely but high-impact scenarios that were raised, and what they would hit>

Leave out a section, heading included, if the notes have nothing for it."""


def ask(prompt, max_tokens):
    try:
        return digest.llm(digest.SYSTEM, prompt, max_tokens=max_tokens)
    except Exception as e:
        print(f"model failed: {e}"); return ""


def build(kind, start, end, label):
    vids = load(start, end)
    if not vids: return None
    plen = end - start
    prev = signals.tally(load(start - plen, start))
    t = signals.tally(vids)
    chans = {v["channel"] for v in vids}
    days = (min(end, date.today() + timedelta(days=1)) - start).days
    title = f"{'Monthly' if kind == 'month' else 'Quarterly'} signals — {label}"
    md = [f"# {title}\n",
          f"_From {len(vids)} videos across {len(chans)} channels, {start:%b %d} – {end - timedelta(days=1):%b %d, %Y}"
          f"{' (so far)' if end > date.today() else ''}._\n"]

    bull = [r for r in signals.ranked(t, "bullish", 40) if len(r[1]["bullish"]) > len(r[1]["cautious"])][:15]
    caut = [r for r in signals.ranked(t, "cautious", 40) if len(r[1]["cautious"]) >= len(r[1]["bullish"])][:10]
    items, idx = [], 0
    for rows in (bull, caut):
        for k, e in rows:
            idx += 1
            views = [s[1] for s in e["samples"]][:3]
            items.append(f"{idx}. {e['name']} — bullish {len(e['bullish'])}, cautious {len(e['cautious'])}, "
                         f"mixed {len(e['mixed'])}\n   " + "\n   ".join(signals.untime(x)[:200] for x in views))
    why_out = ask(WHY_PROMPT.format(n=len(vids), label=label, items="\n".join(items)), 1500) if items else ""
    why = {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"(?m)^\s*(\d+)[:.)]\s*(.+)$", why_out)}
    names = {i: e["name"] for i, (_, e) in enumerate(bull + caut, 1)}
    for i, w in why.items():     # "Nvidia – bullish due to ..." -> "due to ..."
        w = re.sub(rf"^\**{re.escape(names.get(i, '~'))}\**\s*[–—:-]\s*", "", w, flags=re.I)
        why[i] = re.sub(r"^(bullish|cautious|bearish)[,;:]?\s*", "", w, flags=re.I)
    bottom = re.search(r"BOTTOM LINE:\s*(.+?)(?:\n\s*\n|\n\s*\d+[:.)])", why_out + "\n\n1:", re.S)
    if bottom: md.append(f"**Bottom line:** {bottom.group(1).strip()}\n")

    def table(rows, sent, offset):
        has_prev = bool(prev)
        head = "| # | Stock / sector | Bullish | Cautious | Why |" + (" Last period |" if has_prev else "")
        out = [head, "|---|---|---|---|---|" + ("---|" if has_prev else "")]
        prev_rank = {k: i for i, (k, _) in enumerate(signals.ranked(prev, sent, 50), 1)}
        for i, (k, e) in enumerate(rows, 1):
            change = ""
            if has_prev:
                p = prev_rank.get(k)
                change = " new |" if p is None else (f" #{p} |")
            out.append(f"| {i} | **{e['name']}** | {len(e['bullish'])} | {len(e['cautious'])} | "
                       f"{why.get(offset + i, '').replace('|', '/')} |{change}")
        return "\n".join(out)

    if bull: md.append("## Most bullish\n\n" + table(bull, "bullish", 0) + "\n")
    if caut: md.append("## Most cautious\n\n" + table(caut, "cautious", len(bull)) + "\n")
    md.append("_Bullish / Cautious = number of videos holding that view._\n")

    cands = signals.private_candidates(vids)
    if cands:
        items = "\n".join(f"- {e['name']} ({len(e['videos'])} videos): " + " / ".join(e["snippets"]) for _, e in cands)
        out = ask(PRIVATE_PROMPT.format(n=len(vids), label=label, items=items[:14000]), 1500)
        rows = [l.strip().strip("|").split("|") for l in out.split("\n") if l.count("|") >= 3]
        mentions = {signals.key(e["name"]): len(e["videos"]) for _, e in cands}
        if rows:
            md.append("## Private & pre-IPO companies to watch\n")
            md.append("| Company | Sector | What was said | IPO timing | Videos |\n|---|---|---|---|---|")
            for r in rows[:15]:
                r = [c.strip() for c in r] + ["-"] * 4
                md.append(f"| **{r[0].strip('* ')}** | {r[1]} | {r[2]} | {r[3]} | {mentions.get(signals.key(r[0]), '-')} |")
            md.append("\n_Pre-IPO shares are usually bought through secondary platforms (EquityZen, Forge, Hiive), "
                      "funds or SPVs that hold stakes, or tender offers; minimums, fees and lock-ups vary._\n")

    insights = sample([f"{v['bottom']}" for v in vids if v["bottom"]] + [i for v in vids for i in v["insights"]], 70)
    risks = sample([r for v in vids for r in v["risks"]], 60)
    if insights or risks:
        out = ask(THEMES_PROMPT.format(n=len(vids), label=label, insights="\n".join(f"- {x[:220]}" for x in insights) or "-",
                                       risks="\n".join(f"- {x[:200]}" for x in risks) or "-"), 1500)
        if out: md.append(digest.cap_bullets(digest.tidy(out), keep=6) + "\n")

    md.append("_Built from the daily YouTube digests: what the hosts and guests said, not investment advice._\n")
    used = " and ".join(digest.USED_MODELS)
    if used: md.append(f"_Written by {used} (open-source models)._\n")
    return title, "\n".join(md)


def main():
    import send_email
    os.makedirs(OUT, exist_ok=True)
    today = date.today()
    kind = os.getenv("REPORT_PERIOD", "auto")
    kinds = ["month", "quarter"] if kind == "auto" and today.month in (1, 4, 7, 10) else \
            ["month"] if kind == "auto" else [kind]
    to_date = os.getenv("REPORT_TO_DATE") == "1"
    sent = json.load(open(SENT)) if os.path.exists(SENT) else {}
    for k in kinds:
        start, end, label, slug = period(k, today, to_date)
        if not to_date and slug in sent and os.getenv("FORCE") != "1":
            print(f"{slug}: already sent {sent[slug]}"); continue
        res = build(k, start, end, label)
        if not res: print(f"{slug}: no digests in {start} – {end}"); continue
        title, md = res
        name = slug + ("_so_far" if to_date else "")
        open(os.path.join(OUT, name + ".md"), "w", encoding="utf-8").write(md)
        print(md)
        if os.getenv("NO_EMAIL") != "1":
            send_email.send(f"YouTube {title}" + (" (so far)" if to_date else ""), md)
            if not to_date:
                sent[slug] = date.today().isoformat()
                json.dump(sent, open(SENT, "w"), indent=1)


if __name__ == "__main__":
    main()
