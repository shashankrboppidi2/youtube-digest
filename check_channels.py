"""Check candidate channels before adding them to channels.txt (run by .github/workflows/check-channels.yml).

For each @handle / URL / UC id in $CHANNELS (comma or space separated): the channel name, how many uploads in the last
30 days, and the latest titles, so you can judge whether it posts often and on topic.
"""
import os, re
from datetime import datetime, timezone, timedelta
import digest

now = datetime.now(timezone.utc)
for spec in [c for c in re.split(r"[,\s]+", os.getenv("CHANNELS", "")) if c]:
    try:
        cid = digest.channel_id(spec, {})
        vids = digest.feed(cid)
    except Exception as e:
        print(f"\n### {spec}: NOT FOUND ({str(e)[:120]})"); continue
    recent = [v for v in vids if datetime.fromisoformat(v["published"]) > now - timedelta(days=30)]
    name = vids[0]["channel"] if vids else cid
    print(f"\n### {spec} — {name} — {len(recent)} uploads in 30 days (of the {len(vids)} latest listed)")
    for v in vids[:6]:
        print(f"  {v['published'][:10]}  {v['title']}")
