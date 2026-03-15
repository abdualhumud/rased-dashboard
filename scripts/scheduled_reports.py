#!/usr/bin/env python3
"""
Jira Intelligence Agent - Automation Scheduler
Reads scheduler-config.json, determines which schedules should fire today,
and runs the appropriate report (full sprint or per-person nudge).

Runs via GitHub Actions cron (daily at 09:00 AM Riyadh Sun-Thu).
"""

import os, json, sys, subprocess, base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

RIYADH_TZ = timezone(timedelta(hours=3))
now = datetime.now(RIYADH_TZ)
# Python weekday: Mon=0..Sun=6 → convert to our short names
DAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
today_day = DAY_MAP[now.weekday()]
date_str = now.strftime("%Y-%m-%d %H:%M %Z")

print("=" * 60)
print(f"  Jira Automation Scheduler")
print(f"  Date : {date_str}")
print(f"  Day  : {today_day}")
print("=" * 60)

# ── Load config ──────────────────────────────────────────────────────────────
config_path = Path(__file__).parent.parent / "scheduler-config.json"
if not config_path.exists():
    print(f"ERROR: Config not found at {config_path}")
    sys.exit(1)

with open(config_path) as f:
    config = json.load(f)

schedules = config.get("schedules", [])
print(f"\n  Found {len(schedules)} schedule(s)\n")

# ── Condition evaluation helpers ─────────────────────────────────────────────
def _check_has_overdue(person_name):
    """Quick Jira check: does this person have any overdue issues in active sprint?"""
    try:
        jira_base = os.environ.get("JIRA_BASE_URL", "https://rased.atlassian.net")
        jira_email = os.environ.get("JIRA_EMAIL", "")
        jira_token = os.environ.get("JIRA_TOKEN", "")
        board_id = os.environ.get("JIRA_BOARD_ID", "112")
        if not jira_email or not jira_token:
            print("        WARN: No Jira creds for condition check, assuming true")
            return True
        creds = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}
        # Get active sprint
        req = Request(f"{jira_base}/rest/agile/1.0/board/{board_id}/sprint?state=active", headers=headers)
        with urlopen(req, timeout=15) as r:
            sprints = json.loads(r.read()).get("values", [])
        if not sprints:
            return False
        sid = sprints[0]["id"]
        # Check for overdue issues assigned to this person
        today = now.strftime("%Y-%m-%d")
        jql = f'sprint={sid} AND assignee="{person_name}" AND duedate < "{today}" AND status not in (Done,"Released Into Live",Closed,Resolved)'
        from urllib.parse import quote
        req = Request(f"{jira_base}/rest/api/3/search?jql={quote(jql)}&maxResults=1&fields=key", headers=headers)
        with urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
        return result.get("total", 0) > 0
    except Exception as e:
        print(f"        WARN: Condition check failed ({e}), assuming true")
        return True

def evaluate_condition(condition, person_name):
    """Evaluate schedule condition. Returns True if schedule should fire."""
    if condition == "always":
        return True
    if condition == "has-overdue":
        return _check_has_overdue(person_name)
    print(f"        WARN: Unknown condition '{condition}', assuming true")
    return True

# ── Process each schedule ────────────────────────────────────────────────────
fired = 0
skipped = 0
failed = 0
results = []

for sched in schedules:
    sid = sched.get("id", "unknown")
    enabled = sched.get("enabled", False)
    days = sched.get("days", [])
    scope = sched.get("scope", "full")
    email = sched.get("recipientEmail", "")
    name = sched.get("recipientName", "")
    condition = sched.get("condition", "always")

    # ── Skip checks ──────────────────────────────────────────────────────
    if not enabled:
        print(f"  SKIP  {sid} (disabled)")
        skipped += 1
        results.append({"id": sid, "status": "skipped", "reason": "disabled"})
        continue

    if today_day not in days:
        print(f"  SKIP  {sid} (not today: {today_day} not in {days})")
        skipped += 1
        results.append({"id": sid, "status": "skipped", "reason": f"day={today_day}"})
        continue

    if not email:
        print(f"  SKIP  {sid} (no recipientEmail)")
        skipped += 1
        results.append({"id": sid, "status": "skipped", "reason": "no email"})
        continue

    # ── Evaluate condition ─────────────────────────────────────────────
    if not evaluate_condition(condition, name):
        print(f"  SKIP  {sid} (condition '{condition}' not met for {name})")
        skipped += 1
        results.append({"id": sid, "status": "skipped", "reason": f"condition={condition}"})
        continue

    # ── Fire the schedule ────────────────────────────────────────────────
    print(f"\n  FIRE  {sid}")
    print(f"        To: {email} | Scope: {scope} | Condition: {condition}")

    # Build environment with overrides
    env = {**os.environ}
    env["OVERRIDE_EMAIL"] = email

    # Use unique artifact filenames per schedule to avoid collisions
    env["ARTIFACT_SUFFIX"] = f"_{sid}"

    try:
        if scope == "full":
            # Run the daily report script
            script = str(Path(__file__).parent / "daily_report.py")
            print(f"        Running: daily_report.py -> {email}")
            result = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, env=env, timeout=120
            )
        elif scope == "self":
            # Run the nudge script for this person
            env["NUDGE_PERSON"] = name
            env["NUDGE_EMAIL"] = email
            script = str(Path(__file__).parent / "send_nudge.py")
            print(f"        Running: send_nudge.py -> {name} ({email})")
            result = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, env=env, timeout=120
            )
        else:
            print(f"        ERROR: Unknown scope '{scope}'")
            failed += 1
            results.append({"id": sid, "status": "failed", "reason": f"unknown scope: {scope}"})
            continue

        # Print subprocess output
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"        | {line}")

        if result.returncode == 0:
            print(f"        OK (exit 0)")
            fired += 1
            results.append({"id": sid, "status": "sent", "to": email})
        else:
            print(f"        FAILED (exit {result.returncode})")
            if result.stderr:
                for line in result.stderr.strip().split("\n")[:5]:
                    print(f"        ! {line}")
            failed += 1
            results.append({"id": sid, "status": "failed", "reason": f"exit {result.returncode}"})

    except subprocess.TimeoutExpired:
        print(f"        TIMEOUT (>120s)")
        failed += 1
        results.append({"id": sid, "status": "failed", "reason": "timeout"})
    except Exception as e:
        print(f"        EXCEPTION: {e}")
        failed += 1
        results.append({"id": sid, "status": "failed", "reason": str(e)})

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  SUMMARY: {fired} sent | {skipped} skipped | {failed} failed")
print("=" * 60)

for r in results:
    status_icon = {"sent": "+", "skipped": "-", "failed": "!"}
    icon = status_icon.get(r["status"], "?")
    detail = r.get("to", r.get("reason", ""))
    print(f"  [{icon}] {r['id']}: {r['status']} ({detail})")

# Exit with error if any schedule failed
if failed > 0:
    sys.exit(1)
