"""E-mail today's digest (digests/digest_YYYY-MM-DD.md) at 7am Eastern. Run by .github/workflows/email.yml.

GitHub cron is UTC-only, so the workflow fires at 10:57 and 11:57 UTC; this script sends on the first run at or
after 06:45 New York time and records the date in digests/sent.json, so daylight saving time doesn't matter.
A manual run (FORCE=1) always sends.

Env: SMTP_USER, SMTP_PASS (Gmail app password), MAIL_TO (comma-separated), SMTP_HOST, SMTP_PORT, FORCE
"""
import os, re, json, smtplib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import markdown

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "digests")
SENT_FILE = os.path.join(OUT, "sent.json")
REPO_URL = "https://github.com/" + os.getenv("GITHUB_REPOSITORY", "shashankrboppidi2/youtube-digest")

STYLE = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
         "font-size:15px;line-height:1.5;color:#1f2328;max-width:680px;margin:0 auto;padding:8px")

def to_html(md):
    md = re.sub(r"(?m)^(?!\s*[-*] )(.+)\n(?=\s*[-*] )", r"\1\n\n", md)   # a list right under a line needs a blank line
    html = markdown.markdown(md, extensions=["sane_lists"])
    html = html.replace("<h1>", '<h1 style="font-size:22px;margin:0 0 12px">')
    html = html.replace("<h2>", '<h2 style="font-size:17px;margin:28px 0 4px;padding-top:16px;border-top:1px solid #d0d7de">')
    html = html.replace("<a ", '<a style="color:#0969da" ')
    return f'<div style="{STYLE}">{html}</div>'

def main():
    force = os.getenv("FORCE") == "1"
    ny = datetime.now(ZoneInfo("America/New_York"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")   # the digest job names files by UTC date
    sent = json.load(open(SENT_FILE)) if os.path.exists(SENT_FILE) else {}
    if not force:
        if (ny.hour, ny.minute) < (6, 45): print(f"{ny:%H:%M} New York time, too early; skipping"); return
        if today in sent: print(f"{today} already sent; skipping"); return

    host, user, pw, to = (os.getenv("SMTP_HOST", "smtp.gmail.com"), os.getenv("SMTP_USER"),
                          os.getenv("SMTP_PASS"), os.getenv("MAIL_TO"))
    if not all([user, pw, to]): raise SystemExit("SMTP_USER, SMTP_PASS and MAIL_TO secrets are required")

    path = os.path.join(OUT, f"digest_{today}.md")
    if os.path.exists(path):
        md = open(path, encoding="utf-8").read()
        n = len([h for h in re.findall(r"^## (.+)$", md, re.M) if h != "No transcript available"])
        subject = f"YouTube digest {today}" + (f" — {n} video{'s' if n != 1 else ''}" if n else "")
    else:
        md = (f"# YouTube digest — {today}\n\nNo digest was written today: either no new videos, or the "
              f"overnight run failed. Runs: [{REPO_URL}/actions]({REPO_URL}/actions)")
        subject = f"YouTube digest {today} — nothing today"

    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["To"] = subject, user, to
    m.attach(MIMEText(md, "plain", "utf-8"))
    m.attach(MIMEText(to_html(md), "html", "utf-8"))
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls(); s.login(user, pw); s.sendmail(user, [a.strip() for a in to.split(",")], m.as_string())
    print(f"sent '{subject}' to {to}")
    if not force:
        sent[today] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        os.makedirs(OUT, exist_ok=True)
        json.dump(dict(sorted(sent.items())[-60:]), open(SENT_FILE, "w"), indent=1)

if __name__ == "__main__":
    main()
