#!/usr/bin/env python3
"""
Suntec Cold-Email Mailer — command-line interface.

Usage:
  python cli.py preview            # dry-run: show what WOULD be sent (no email)
  python cli.py preview --limit 5  # only first 5
  python cli.py send               # real send, respects daily limits + throttle
  python cli.py send --limit 20    # cap this run to 20 emails
  python cli.py status             # show tracking stats
  python cli.py status --recent 30 # show last 30 send records
  python cli.py --config myconf.yaml send

All commands read config.yaml (copy from config.example.yaml first).
"""
import argparse
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402


def cmd_preview(cfg, args):
    print(">> DRY-RUN preview (no emails sent)\n")
    res = engine.run_campaign(cfg, dry_run=True, limit=args.limit)
    for line in res.log:
        if line["msg"] == "DRY-RUN preview":
            print(f"  [{line['lang']}] {line['company']} <{line['email']}> "
                  f"via {line['account']}")
            print(f"      Subject: {line['subject']}")
            print(f"      {line['body_preview'].replace(chr(10),' ')}...")
            print()
    print(f"-- Would send: {res.sent}  |  skipped(already sent): {res.skipped}")
    if res.stopped_reason:
        print(f"-- stopped: {res.stopped_reason}")


def cmd_send(cfg, args):
    print(">> LIVE SEND starting (Ctrl-C to stop safely)\n")

    def progress(line):
        m = line["msg"]
        if m == "SENT":
            print(f"  [SENT] {line['company']} <{line['email']}> "
                  f"via {line['account']}")
        elif m in ("FAILED", "BOUNCED"):
            print(f"  [{m}] {line['email']}: {line.get('error','')[:120]}")
        elif m == "skip (already sent)":
            print(f"  [skip] {line['email']} (already contacted)")
        elif m == "pause":
            print(f"      ...waiting {line['seconds']}s")

    res = engine.run_campaign(cfg, dry_run=False, limit=args.limit,
                              progress=progress)
    print(f"\n-- Sent: {res.sent}  failed: {res.failed}  "
          f"skipped: {res.skipped}")
    if res.stopped_reason:
        print(f"-- stopped: {res.stopped_reason}")


def cmd_status(cfg, args):
    db_path = engine._resolve(HERE, cfg["tracking"]["db_path"])
    if not os.path.exists(db_path):
        print("No tracking DB yet — nothing sent.")
        return
    t = engine.Tracker(db_path)
    s = t.stats()
    print(">> Tracking stats")
    print(f"   sent    : {s.get('sent',0)}")
    print(f"   bounced : {s.get('bounced',0)}")
    print(f"   failed  : {s.get('failed',0)}")
    print(f"   today   : {s.get('today_total',0)}")
    if args.recent:
        print(f"\n>> Last {args.recent} records:")
        for r in t.recent(args.recent):
            print(f"   {r['sent_at']}  [{r['status']}]  "
                  f"{r['company']} <{r['email']}>")


def main():
    ap = argparse.ArgumentParser(description="Suntec Cold-Email Mailer")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preview", help="dry-run, no emails")
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("send", help="real send")
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("status", help="show stats")
    p.add_argument("--recent", type=int, default=0)

    args = ap.parse_args()
    cfg = engine.load_config(args.config)

    if args.command == "preview":
        cmd_preview(cfg, args)
    elif args.command == "send":
        cmd_send(cfg, args)
    elif args.command == "status":
        cmd_status(cfg, args)


if __name__ == "__main__":
    main()
