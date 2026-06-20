"""
Suntec Cold-Email Mailer — core engine.

Responsibilities:
  - Load config + leads + templates
  - Personalize per lead, auto-pick language by country
  - Throttled, multi-account SMTP send with warmup
  - SQLite tracking (sent/failed/bounced) + resume (never double-send)

This module is UI-agnostic: both cli.py and the Flask web app call into it.
"""
from __future__ import annotations

import csv
import os
import re
import smtplib
import sqlite3
import time
import random
import datetime as dt
import imaplib
import email
from email.header import decode_header
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr, make_msgid
from typing import Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: str = None) -> dict:
    path = path or os.path.join(HERE, "config.yaml")
    if not os.path.exists(path):
        raise SystemExit(
            f"Config not found: {path}\n"
            f"-> copy config.example.yaml to config.yaml and fill it in."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resolve(base_path: str, p: str) -> str:
    """Resolve a path relative to the config/engine dir."""
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(HERE, p))


# --------------------------------------------------------------------------- #
# Tracking DB
# --------------------------------------------------------------------------- #
class Tracker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                company TEXT,
                country TEXT,
                lang TEXT,
                account TEXT,
                status TEXT NOT NULL,          -- sent | failed | bounced | skipped
                error TEXT,
                message_id TEXT,
                sent_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_email ON sends(email);
            CREATE TABLE IF NOT EXISTS daily_counts (
                day TEXT NOT NULL,
                account TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, account)
            );
            """
        )
        self.conn.commit()

    # --- dedupe / resume ---------------------------------------------------- #
    def already_sent(self, email: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM sends WHERE email=? AND status IN ('sent','bounced')",
            (email.lower(),),
        )
        return cur.fetchone() is not None

    def record(self, *, email, company, country, lang, account, status,
               error=None, message_id=None):
        self.conn.execute(
            """INSERT INTO sends(email,company,country,lang,account,status,error,message_id,sent_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                 status=excluded.status, error=excluded.error,
                 message_id=excluded.message_id, sent_at=excluded.sent_at,
                 account=excluded.account, lang=excluded.lang""",
            (email.lower(), company, country, lang, account, status, error,
             message_id, dt.datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # --- daily counters ----------------------------------------------------- #
    def today(self) -> str:
        return dt.date.today().isoformat()

    def sent_today(self, account: str) -> int:
        cur = self.conn.execute(
            "SELECT count FROM daily_counts WHERE day=? AND account=?",
            (self.today(), account),
        )
        row = cur.fetchone()
        return row["count"] if row else 0

    def sent_today_total(self) -> int:
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(count),0) AS c FROM daily_counts WHERE day=?",
            (self.today(),),
        )
        return cur.fetchone()["c"]

    def bump(self, account: str):
        self.conn.execute(
            """INSERT INTO daily_counts(day,account,count) VALUES(?,?,1)
               ON CONFLICT(day,account) DO UPDATE SET count=count+1""",
            (self.today(), account),
        )
        self.conn.commit()

    def stats(self) -> dict:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) c FROM sends GROUP BY status"
        )
        out = {r["status"]: r["c"] for r in cur.fetchall()}
        out["today_total"] = self.sent_today_total()
        return out

    def recent(self, limit=50):
        cur = self.conn.execute(
            "SELECT * FROM sends ORDER BY sent_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def load_template(tdir: str, lang: str, default_lang: str) -> dict:
    """A template file is  <lang>.txt  with first line 'Subject: ...'."""
    path = os.path.join(tdir, f"{lang}.txt")
    if not os.path.exists(path):
        path = os.path.join(tdir, f"{default_lang}.txt")
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    lines = raw.splitlines()
    subject = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.lower().startswith("subject:"):
            subject = ln.split(":", 1)[1].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).lstrip("\n")
    return {"subject": subject, "body": body}


def personalize(text: str, lead: dict, sender: dict) -> str:
    name = (lead.get("ContactName") or "").strip()
    greeting_name = name if name else "Sir/Madam"
    repl = {
        "{ContactName}": greeting_name,
        "{Company}": (lead.get("Company") or "your company").strip(),
        "{Country}": (lead.get("Country") or "your market").strip(),
        "{ProductMatch}": (lead.get("ProductMatch") or "lifting equipment").strip(),
        "{IntentSignal}": (lead.get("IntentSignal") or "").strip(),
        "{FromName}": sender.get("from_name", ""),
        "{CompanyName}": sender.get("company", ""),
        "{ShopURL}": sender.get("shop_url", ""),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #
def load_leads(cfg: dict) -> list[dict]:
    lc = cfg["leads"]
    path = _resolve(HERE, lc["csv_path"])
    if not os.path.exists(path):
        raise SystemExit(f"Leads CSV not found: {path}")
    tiers = set(lc.get("filter_tiers") or [])
    countries = set(lc.get("filter_countries") or [])
    min_score = int(lc.get("filter_min_score") or 0)
    ecol = lc.get("email_column", "Email")

    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            email = (row.get(ecol) or "").strip()
            if lc.get("skip_without_email", True) and not EMAIL_RE.match(email):
                continue
            if tiers and (row.get("Tier") or "").strip() not in tiers:
                continue
            if countries and (row.get("Country") or "").strip() not in countries:
                continue
            try:
                score = int(float(row.get("IntentScore") or 0))
            except ValueError:
                score = 0
            if score < min_score:
                continue
            out.append(row)
    return out


def pick_lang(cfg: dict, country: str) -> str:
    tcfg = cfg["templates"]
    return tcfg.get("country_lang", {}).get(country, tcfg.get("default_lang", "en"))


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #
def _connect(acc: dict):
    if acc.get("ssl"):
        server = smtplib.SMTP_SSL(acc["host"], acc["port"], timeout=30)
    else:
        server = smtplib.SMTP(acc["host"], acc["port"], timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
    server.login(acc["username"], acc["password"])
    return server


def build_message(*, acc, sender, to_email, subject, body, attachment_path=None):
    msg = MIMEMultipart()
    msg["From"] = formataddr((sender.get("from_name", ""), acc["from_email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    if sender.get("reply_to"):
        msg["Reply-To"] = sender["reply_to"]
    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as fh:
            part = MIMEApplication(fh.read(), Name=os.path.basename(attachment_path))
        part["Content-Disposition"] = (
            f'attachment; filename="{os.path.basename(attachment_path)}"'
        )
        msg.attach(part)
    return msg


# --------------------------------------------------------------------------- #
# Daily limit logic (with warmup)
# --------------------------------------------------------------------------- #
def effective_daily_limit(cfg: dict, acc: dict, tracker: Tracker) -> int:
    base = int(acc.get("daily_limit", 40))
    th = cfg.get("throttle", {})
    if not th.get("warmup_enabled"):
        return base
    # warmup based on how many PRIOR distinct days we've already sent
    # (exclude today so the ramp advances per calendar day, not mid-day)
    cur = tracker.conn.execute(
        "SELECT COUNT(DISTINCT day) d FROM daily_counts WHERE day < ?",
        (tracker.today(),),
    )
    days = cur.fetchone()["d"]  # 0 on first ever day
    start = int(th.get("warmup_start", 15))
    step = int(th.get("warmup_step", 10))
    return min(base, start + step * days)


# --------------------------------------------------------------------------- #
# IMAP: Fetching replies
# --------------------------------------------------------------------------- #
def get_recent_emails(cfg: dict, limit: int = 10) -> list[dict]:
    """Fetch recent emails from the first account's inbox."""
    acc = cfg["accounts"][0]
    if "gmail.com" not in acc["host"] and "gmail.com" not in acc["username"]:
        # Fallback/Default for non-gmail might need different IMAP host
        imap_host = acc.get("imap_host", "imap.gmail.com") 
    else:
        imap_host = "imap.gmail.com"

    try:
        mail = imaplib.IMAP4_SSL(imap_host)
        mail.login(acc["username"], acc["password"])
        mail.select("inbox")
        
        # Search for all emails, then take the last N
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return []
            
        msg_ids = messages[0].split()
        latest_ids = msg_ids[-limit:][::-1] # Latest first
        
        results = []
        for mid in latest_ids:
            res, msg_data = mail.fetch(mid, "(RFC822)")
            if res != "OK": continue
            
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject, encoding = decode_header(msg["Subject"] or "")[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8")
                    
                    from_, encoding = decode_header(msg.get("From", ""))[0]
                    if isinstance(from_, bytes):
                        from_ = from_.decode(encoding or "utf-8")
                    
                    date = msg.get("Date", "")
                    
                    # Extract snippet
                    snippet = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                snippet = body[:200].strip()
                                break
                    else:
                        snippet = msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:200].strip()

                    results.append({
                        "id": mid.decode(),
                        "from": from_,
                        "subject": subject,
                        "date": date,
                        "snippet": snippet
                    })
        mail.logout()
        return results
    except Exception as e:
        print(f"IMAP Error: {e}")
        return []
@dataclass
class SendResult:
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_reason: str = ""
    log: list = field(default_factory=list)


def run_campaign(cfg: dict, *, dry_run=False, limit=None, progress=None) -> SendResult:
    """
    progress: optional callable(dict) for live UI updates.
    limit: optional cap on emails this run (besides daily limits).
    """
    sender = cfg["sender"]
    accounts = cfg["accounts"]
    th = cfg.get("throttle", {})
    tdir = _resolve(HERE, cfg["templates"]["dir"])
    default_lang = cfg["templates"].get("default_lang", "en")
    db_path = _resolve(HERE, cfg["tracking"]["db_path"])
    tracker = Tracker(db_path)

    att_cfg = cfg.get("attachment", {})
    attachment_path = (
        _resolve(HERE, att_cfg["path"])
        if att_cfg.get("enabled") and att_cfg.get("path") else None
    )

    leads = load_leads(cfg)
    res = SendResult()

    global_daily = int(th.get("global_daily_limit", 80))
    min_iv = int(th.get("min_interval_seconds", 45))
    max_iv = int(th.get("max_interval_seconds", 180))

    # connect accounts lazily; keep per-account state
    conns: dict[str, object] = {}
    acc_idx = 0

    def log(msg, **extra):
        line = {"msg": msg, **extra}
        res.log.append(line)
        if progress:
            progress(line)

    template_cache: dict[str, dict] = {}

    for lead in leads:
        if limit is not None and res.sent >= limit:
            res.stopped_reason = f"run limit {limit} reached"
            break
        # tracker.sent_today_total() already includes sends bumped this run,
        # so do NOT add res.sent (would double-count and cap 2x too early).
        if tracker.sent_today_total() >= global_daily and not dry_run:
            res.stopped_reason = f"global daily limit {global_daily} reached"
            break

        email = (lead.get("Email") or "").strip()
        company = (lead.get("Company") or "").strip()
        country = (lead.get("Country") or "").strip()
        res.attempted += 1

        if tracker.already_sent(email):
            res.skipped += 1
            log("skip (already sent)", email=email, company=company)
            continue

        # choose an account that still has quota today
        chosen = None
        for _ in range(len(accounts)):
            acc = accounts[acc_idx % len(accounts)]
            acc_idx += 1
            lim = effective_daily_limit(cfg, acc, tracker)
            if tracker.sent_today(acc["label"]) < lim:
                chosen = acc
                break
        if chosen is None:
            res.stopped_reason = "all accounts hit daily limit"
            break

        lang = pick_lang(cfg, country)
        if lang not in template_cache:
            template_cache[lang] = load_template(tdir, lang, default_lang)
        tpl = template_cache[lang]
        subject = personalize(tpl["subject"], lead, sender)
        body = personalize(tpl["body"], lead, sender)

        if dry_run:
            res.sent += 1
            log("DRY-RUN preview", email=email, company=company, country=country,
                lang=lang, account=chosen["label"], subject=subject,
                body_preview=body[:160])
            continue

        # real send
        try:
            if chosen["label"] not in conns:
                conns[chosen["label"]] = _connect(chosen)
            server = conns[chosen["label"]]
            msg = build_message(
                acc=chosen, sender=sender, to_email=email,
                subject=subject, body=body, attachment_path=attachment_path,
            )
            server.sendmail(chosen["from_email"], [email], msg.as_string())
            tracker.record(email=email, company=company, country=country,
                           lang=lang, account=chosen["label"], status="sent",
                           message_id=msg["Message-ID"])
            tracker.bump(chosen["label"])
            res.sent += 1
            log("SENT", email=email, company=company, account=chosen["label"])
        except smtplib.SMTPRecipientsRefused as e:
            tracker.record(email=email, company=company, country=country,
                           lang=lang, account=chosen["label"],
                           status="bounced", error=str(e))
            res.failed += 1
            log("BOUNCED", email=email, error=str(e))
            continue
        except Exception as e:  # noqa
            tracker.record(email=email, company=company, country=country,
                           lang=lang, account=chosen["label"],
                           status="failed", error=str(e))
            res.failed += 1
            log("FAILED", email=email, error=str(e))
            # drop possibly-broken connection so it reconnects next time
            try:
                conns.pop(chosen["label"]).quit()
            except Exception:
                pass
            continue

        # throttle: random pause between sends
        pause = random.randint(min_iv, max_iv)
        log("pause", seconds=pause)
        time.sleep(pause)

    for s in conns.values():
        try:
            s.quit()
        except Exception:
            pass

    return res
