#!/usr/bin/env python3
"""
Jira Intelligence Agent - On-Demand Nudge Email
Sends a personalised alert to a specific team member showing only
their at-risk / pending tasks with deadlines.
Runs via GitHub Actions workflow_dispatch (cloud - no machine needed).
"""

import os, json, base64, smtplib, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── CONFIG (GitHub Secrets injected as env vars) ──────────────────────────────
JIRA_BASE   = os.environ.get("JIRA_BASE_URL", "https://rased.atlassian.net")
JIRA_EMAIL  = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN  = os.environ.get("JIRA_TOKEN", "")
BOARD_ID    = os.environ.get("JIRA_BOARD_ID", "112")
PM_EMAIL    = os.environ.get("PM_EMAIL", "abdualhumud@elm.sa")

ASSIGNEE    = os.environ.get("NUDGE_PERSON", "").strip()
RECIPIENT   = os.environ.get("NUDGE_EMAIL", "").strip()
TO_EMAIL    = RECIPIENT if RECIPIENT else PM_EMAIL   # default: PM gets preview copy

SMTP_USER   = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS   = os.environ.get("SMTP_PASS", "").strip()
SMTP_HOST   = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.environ.get("SMTP_PORT", "587"))

# ── Validate required config ────────────────────────────────────────────────
if not JIRA_EMAIL or not JIRA_TOKEN:
    print("ERROR: JIRA_EMAIL and JIRA_TOKEN secrets are required.")
    sys.exit(1)
if not ASSIGNEE:
    print("ERROR: NUDGE_PERSON input is required (team member name).")
    sys.exit(1)

# Riyadh = UTC+3
RIYADH_TZ = timezone(timedelta(hours=3))
now = datetime.now(RIYADH_TZ)

creds  = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
HEADERS = {
    "Authorization": f"Basic {creds}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

DONE_STATUSES    = {"Done", "Released Into Live", "Closed", "Resolved"}
BLOCKED_STATUSES = {"Blocked", "On Hold"}


def jira_get(path):
    url = f"{JIRA_BASE}{path}"
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def parse_dt(s):
    if not s:
        return None
    try:
        # Jira returns ISO8601 with timezone offset
        s = s[:19]  # strip timezone, keep YYYY-MM-DDTHH:MM:SS
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=RIYADH_TZ)
    except Exception:
        return None


# ── STEP 1: Active sprint ─────────────────────────────────────────────────────
print(f"Fetching active sprint from board {BOARD_ID}…")
try:
    sprint_data = jira_get(f"/rest/agile/1.0/board/{BOARD_ID}/sprint?state=active")
except Exception as e:
    print(f"ERROR: Failed to fetch sprints from Jira: {e}")
    sys.exit(1)

sprints = sprint_data.get("values", [])
if not sprints:
    print("ERROR: No active sprint found on this board.")
    sys.exit(1)

sprint      = sprints[0]
sprint_id   = sprint["id"]
sprint_name = sprint["name"]
sprint_end  = parse_dt(sprint.get("endDate", ""))
days_left   = max(0, (sprint_end - now).days) if sprint_end else "?"
print(f"Sprint: {sprint_name} | {days_left} days remaining")


# ── STEP 2: All sprint issues ─────────────────────────────────────────────────
print("Fetching sprint issues…")
fields = "summary,status,assignee,priority,updated,duedate,issuetype"
data   = jira_get(f"/rest/agile/1.0/sprint/{sprint_id}/issue?maxResults=200&fields={fields}")
all_issues = data.get("issues", [])
print(f"Total sprint issues: {len(all_issues)}")


# ── STEP 3: Filter by assignee (case-insensitive partial match) ───────────────
search = ASSIGNEE.lower()
my_issues = []
for issue in all_issues:
    name = (issue["fields"].get("assignee") or {}).get("displayName", "")
    if not name:
        continue
    name_lower = name.lower()
    first_name_lower = name.split()[0].lower() if name.split() else ""
    if search in name_lower or first_name_lower in search:
        my_issues.append(issue)

print(f"Issues for '{ASSIGNEE}': {len(my_issues)}")
if not my_issues:
    print(f"No issues found for '{ASSIGNEE}'. Exiting.")
    sys.exit(0)


# ── STEP 4: Classify ──────────────────────────────────────────────────────────
overdue, blocked, at_risk, pending, done = [], [], [], [], []

for issue in my_issues:
    f           = issue["fields"]
    key         = issue["key"]
    summary     = f.get("summary", "")
    status_name = f.get("status", {}).get("name", "")
    priority    = (f.get("priority") or {}).get("name", "Medium")
    duedate_str = f.get("duedate")
    updated_str = f.get("updated")

    updated_dt   = parse_dt(updated_str)
    duedate_dt   = None
    deadline_lbl = "Not set"
    urgency      = ""
    days_until   = None

    if duedate_str:
        duedate_dt   = datetime.strptime(duedate_str, "%Y-%m-%d").replace(tzinfo=RIYADH_TZ)
        deadline_lbl = duedate_dt.strftime("%b %-d, %Y")
        days_until   = (duedate_dt - now).days
        if duedate_dt.date() < now.date():
            urgency = "OVERDUE"
        elif days_until <= 3:
            urgency = "DUE SOON"

    days_in_status = round((now - updated_dt).total_seconds() / 86400) if updated_dt else None

    obj = dict(
        key=key, summary=summary, status=status_name, priority=priority,
        deadline_lbl=deadline_lbl, urgency=urgency, days_until=days_until,
        days_in_status=days_in_status,
    )

    if status_name in DONE_STATUSES:
        done.append(obj)
    elif status_name in BLOCKED_STATUSES:
        blocked.append(obj)
    elif urgency == "OVERDUE":
        overdue.append(obj)
    elif urgency == "DUE SOON":
        at_risk.append(obj)
    else:
        pending.append(obj)

urgent_count = len(overdue) + len(blocked) + len(at_risk)
total_open   = len(my_issues) - len(done)
first_name   = ASSIGNEE.split()[0]
date_fmt     = now.strftime("%B %-d, %Y")


# ── STEP 5: Build HTML ────────────────────────────────────────────────────────
def make_row(item, badge_bg, badge_text):
    pr_color  = "#dc2626" if item["priority"] == "Highest" else ("#ea580c" if item["priority"] == "High" else "#6b7280")
    due_color = "#dc2626" if item["urgency"] == "OVERDUE" else ("#ea580c" if item["urgency"] == "DUE SOON" else "#16a34a")
    due_bold  = "font-weight:700;" if item["urgency"] else ""
    ds_lbl    = f"{item['days_in_status']}d" if item["days_in_status"] is not None else "N/A"
    summ      = item["summary"][:70] + ("…" if len(item["summary"]) > 70 else "")
    return f"""
<tr style="border-bottom:1px solid #e5e7eb;">
  <td style="padding:9px 12px;"><a href="{JIRA_BASE}/browse/{item['key']}" style="color:#2563eb;font-weight:700;text-decoration:none;">{item['key']}</a></td>
  <td style="padding:9px 12px;font-size:13px;color:#374151;">{summ}</td>
  <td style="padding:9px 12px;font-size:12px;color:#6b7280;">{item['status']}</td>
  <td style="padding:9px 12px;font-size:12px;color:{due_color};{due_bold}">{item['deadline_lbl']}</td>
  <td style="padding:9px 12px;"><span style="color:{pr_color};font-weight:600;font-size:12px;">{item['priority']}</span></td>
  <td style="padding:9px 12px;"><span style="background:{badge_bg};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;">{badge_text}</span></td>
</tr>"""


rows = ""
for item in overdue: rows += make_row(item, "#dc2626", "OVERDUE")
for item in blocked: rows += make_row(item, "#ea580c", "BLOCKED")
for item in at_risk: rows += make_row(item, "#d97706", "DUE SOON")
for item in pending: rows += make_row(item, "#6366f1", "PENDING")

banner_color = "#dc2626" if urgent_count > 5 else ("#ea580c" if urgent_count > 0 else "#22c55e")
banner_text  = (f"You have {urgent_count} urgent items requiring immediate attention"
                if urgent_count > 0 else "All your active tasks are on track")

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Segoe UI,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
<tr><td align="center" style="padding:24px 16px;">
<table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10);">

<!-- HEADER -->
<tr><td style="background:linear-gradient(135deg,#1e1b4b,#312e81);padding:28px 32px;">
  <p style="color:#a5b4fc;font-size:11px;font-weight:700;background:rgba(165,180,252,0.12);padding:4px 10px;border-radius:4px;display:inline-block;margin:0 0 10px;">RASED V2 SPRINT ALERT</p>
  <h1 style="color:#fff;margin:0 0 4px;font-size:22px;font-weight:800;">Hi {first_name} &#128075;</h1>
  <p style="color:#c7d2fe;margin:0;font-size:14px;">Your task summary for <strong>{sprint_name}</strong> &mdash; {date_fmt}</p>
</td></tr>

<!-- URGENCY BANNER -->
<tr><td style="background:{banner_color};padding:12px 32px;">
  <p style="margin:0;color:#fff;font-size:13px;font-weight:600;">{banner_text}</p>
</td></tr>

<!-- STATS ROW -->
<tr><td style="padding:20px 32px 8px;">
<table width="100%" cellpadding="0" cellspacing="0"><tr>
  <td style="text-align:center;padding:12px;background:#fef2f2;border-radius:10px;">
    <div style="font-size:28px;font-weight:800;color:#dc2626;">{len(overdue)}</div>
    <div style="font-size:11px;color:#b91c1c;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Overdue</div>
  </td>
  <td width="8"></td>
  <td style="text-align:center;padding:12px;background:#fff7ed;border-radius:10px;">
    <div style="font-size:28px;font-weight:800;color:#ea580c;">{len(blocked)}</div>
    <div style="font-size:11px;color:#c2410c;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Blocked</div>
  </td>
  <td width="8"></td>
  <td style="text-align:center;padding:12px;background:#fffbeb;border-radius:10px;">
    <div style="font-size:28px;font-weight:800;color:#d97706;">{len(at_risk)}</div>
    <div style="font-size:11px;color:#b45309;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Due in 3d</div>
  </td>
  <td width="8"></td>
  <td style="text-align:center;padding:12px;background:#f0fdf4;border-radius:10px;">
    <div style="font-size:28px;font-weight:800;color:#16a34a;">{len(done)}</div>
    <div style="font-size:11px;color:#15803d;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Done</div>
  </td>
  <td width="8"></td>
  <td style="text-align:center;padding:12px;background:#f0f9ff;border-radius:10px;">
    <div style="font-size:28px;font-weight:800;color:#0369a1;">{days_left}</div>
    <div style="font-size:11px;color:#075985;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Days Left</div>
  </td>
</tr></table>
</td></tr>

<!-- TASK TABLE -->
<tr><td style="padding:16px 32px 24px;">
<h2 style="color:#111827;font-size:15px;margin:0 0 14px;font-weight:700;">Your Open Tasks ({total_open} total)</h2>
<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-size:13px;">
<thead>
<tr style="background:#f9fafb;">
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">Ticket</th>
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">Summary</th>
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">Status</th>
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">&#128197; Deadline</th>
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">Priority</th>
  <th style="padding:9px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #e5e7eb;color:#374151;">Flag</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
<p style="color:#9ca3af;font-size:11px;margin-top:8px;">
  <span style="color:#dc2626;font-weight:600;">Red = past due</span> &nbsp;|&nbsp;
  <span style="color:#ea580c;font-weight:600;">Orange = due within 3 days</span> &nbsp;|&nbsp;
  <span style="color:#16a34a;font-weight:600;">Green = on track</span>
</p>
</td></tr>

<!-- CTA -->
<tr><td style="padding:0 32px 28px;">
  <a href="{JIRA_BASE}/jira/software/c/projects/RNT/boards/{BOARD_ID}"
     style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;text-decoration:none;font-weight:700;padding:12px 28px;border-radius:8px;font-size:14px;">
    Open My Jira Board &rarr;
  </a>
  <p style="color:#9ca3af;font-size:12px;margin-top:12px;">Please update your tickets and unblock stalled items before the sprint ends in <strong>{days_left} days</strong>.</p>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#f9fafb;padding:16px 32px;border-top:1px solid #e5e7eb;">
  <p style="margin:0;font-size:11px;color:#9ca3af;">Sent by <strong>Jira Intelligence Agent</strong> on behalf of the Rased V2 PM | {now.strftime('%Y-%m-%d %H:%M')} Riyadh</p>
</td></tr>

</table></td></tr></table>
</body></html>"""

# Save artifact
with open("nudge_report.html", "w", encoding="utf-8") as fh:
    fh.write(html)
print("Nudge HTML saved to nudge_report.html")


# ── STEP 6: Send via SMTP ─────────────────────────────────────────────────────
first_name = ASSIGNEE.split()[0] if ASSIGNEE else ASSIGNEE
subject = f"Personal Task Update: {first_name} — {urgent_count} Urgent Items | {sprint_name} | {days_left} days left"

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"]    = f"Jira Intelligence Agent <{SMTP_USER}>" if SMTP_USER else TO_EMAIL
msg["To"]      = TO_EMAIL
msg["CC"]      = PM_EMAIL
msg.attach(MIMEText(html, "html"))

recipients = list({TO_EMAIL, PM_EMAIL})  # deduplicate

smtp_mode = "SSL" if SMTP_PORT == 465 else "STARTTLS"
print(f"Sending nudge to: {TO_EMAIL} (CC: {PM_EMAIL}) via {SMTP_HOST}:{SMTP_PORT} ({smtp_mode})...")
try:
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()
    if SMTP_USER and SMTP_PASS:
        server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(SMTP_USER or TO_EMAIL, recipients, msg.as_string())
    server.quit()
    print(f"EMAIL SENT for {ASSIGNEE} ({urgent_count} urgent items)")
except smtplib.SMTPAuthenticationError as e:
    print(f"SMTP AUTH ERROR: {e.smtp_code} {e.smtp_error}")
    sys.exit(1)
except smtplib.SMTPException as e:
    print(f"SMTP ERROR: {type(e).__name__}: {e}")
    sys.exit(1)
except OSError as e:
    print(f"NETWORK ERROR: Could not connect to {SMTP_HOST}:{SMTP_PORT}: {e}")
    sys.exit(1)
