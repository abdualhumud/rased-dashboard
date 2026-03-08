#!/usr/bin/env python3
"""
Jira Intelligence Agent - Daily Email Report
Runs via GitHub Actions on a daily schedule (cloud - no machine needed).
Fetches live sprint data, generates HTML email with Deadline column, sends via SMTP.
"""

import os, json, base64, smtplib, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── CONFIG (GitHub Secrets injected as env vars) ──────────────────────────────
JIRA_BASE  = os.environ.get("JIRA_BASE_URL", "https://rased.atlassian.net")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_TOKEN = os.environ["JIRA_TOKEN"]
BOARD_ID   = os.environ.get("JIRA_BOARD_ID", "112")
REPORT_TO  = os.environ["REPORT_EMAIL"]
SMTP_USER  = os.environ["SMTP_USER"]
SMTP_PASS  = os.environ["SMTP_PASS"]
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))

# Riyadh = UTC+3
RIYADH_TZ = timezone(timedelta(hours=3))
now = datetime.now(RIYADH_TZ)

creds   = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
HEADERS = {"Authorization": f"Basic {creds}", "Accept": "application/json"}

def jira_get(path):
    req = Request(f"{JIRA_BASE}{path}", headers=HEADERS)
    try:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except URLError as e:
        print(f"ERROR: {e}"); sys.exit(1)

# ── 1. ACTIVE SPRINT ──────────────────────────────────────────────────────────
print("Fetching active sprint...")
sprint = jira_get(f"/rest/agile/1.0/board/{BOARD_ID}/sprint?state=active")["values"][0]
sprint_id, sprint_name = sprint["id"], sprint["name"]

def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(RIYADH_TZ)

sprint_start = parse_dt(sprint["startDate"])
sprint_end   = parse_dt(sprint["endDate"])
days_elapsed   = max(0, (now - sprint_start).days)
days_remaining = max(0, (sprint_end   - now).days)
total_days     = max(1, (sprint_end - sprint_start).days)
time_pct       = round(days_elapsed / total_days * 100)
print(f"Sprint: {sprint_name} | Day {days_elapsed}/{total_days}")

# ── 2. SPRINT ISSUES ─────────────────────────────────────────────────────────
print("Fetching issues...")
fields = "summary,status,assignee,priority,updated,duedate,issuetype,resolution,resolutiondate"
issues = jira_get(f"/rest/agile/1.0/sprint/{sprint_id}/issue?maxResults=200&fields={fields}")["issues"]
total_issues = len(issues)
print(f"Loaded {total_issues} issues")

# ── 3. CLASSIFY ───────────────────────────────────────────────────────────────
DONE_STATUSES    = {"Done","Released Into Live","Closed","Resolved"}
BLOCKED_STATUSES = {"Blocked","On Hold"}
TODO_STATUSES    = {"Open","To Do","Backlog","Ready To Develop","deferred","Not applicable"}
STALLED_THRESHOLD = now - timedelta(hours=24)

done=[];blocked=[];overdue=[];stalled=[];not_started=[];completed_today=[]

for issue in issues:
    f  = issue["fields"]
    key       = issue["key"]
    summary   = f.get("summary","")
    status    = f["status"]["name"]
    assignee  = f["assignee"]["displayName"] if f.get("assignee") else "Unassigned"
    priority  = f["priority"]["name"]         if f.get("priority") else "Medium"
    due_str   = f.get("duedate")
    upd_str   = f.get("updated")
    res_str   = f.get("resolutiondate")

    updated = parse_dt(upd_str) if upd_str else None
    resdate = parse_dt(res_str) if res_str else None

    # Format deadline cell
    deadline_html = "<span style='color:#9ca3af;'>&#8212;</span>"
    duedate_dt = None
    if due_str:
        duedate_dt = datetime.fromisoformat(due_str).replace(tzinfo=RIYADH_TZ)
        label = duedate_dt.strftime("%b %d, %Y")
        if duedate_dt.date() < now.date():
            deadline_html = f"<span style='color:#dc2626;font-weight:700;'>&#9888; {label}</span>"
        elif duedate_dt.date() <= (now + timedelta(days=3)).date():
            deadline_html = f"<span style='color:#ea580c;font-weight:600;'>&#9200; {label}</span>"
        else:
            deadline_html = f"<span style='color:#16a34a;'>{label}</span>"

    obj = dict(key=key, summary=summary, status=status, assignee=assignee,
               priority=priority, deadline_html=deadline_html,
               duedate_dt=duedate_dt, updated=updated, days_flag=None,
               flag_text="", flag_color="")

    if status in DONE_STATUSES:
        done.append(obj)
        if (resdate and resdate > now - timedelta(hours=24)) or \
           (updated  and updated  > now - timedelta(hours=24)):
            completed_today.append(obj)
        continue

    if status in BLOCKED_STATUSES:
        obj.update(days_flag=round((now-updated).total_seconds()/86400,1) if updated else "N/A",
                   flag_text="BLOCKED", flag_color="#ea580c")
        blocked.append(obj); continue

    if duedate_dt and duedate_dt < now:
        obj.update(days_flag=(now-duedate_dt).days,
                   flag_text="OVERDUE", flag_color="#dc2626")
        overdue.append(obj); continue

    if status in TODO_STATUSES:
        obj.update(days_flag=round((now-updated).total_seconds()/86400,1) if updated else "N/A",
                   flag_text="NOT STARTED", flag_color="#ca8a04")
        not_started.append(obj); continue

    if updated and updated < STALLED_THRESHOLD:
        obj.update(days_flag=round((now-updated).total_seconds()/3600),
                   flag_text="STALLED", flag_color="#ea580c")
        stalled.append(obj)

done_count     = len(done)
completion_pct = round(done_count / max(1, total_issues) * 100)
health_score   = max(0, min(100, round(completion_pct / max(1, time_pct) * 70)))
print(f"Done:{done_count} Overdue:{len(overdue)} Blocked:{len(blocked)} Stalled:{len(stalled)} NotStarted:{len(not_started)}")

# ── 4. BUILD HTML EMAIL ───────────────────────────────────────────────────────
health_color = "#22c55e" if health_score>=70 else "#eab308" if health_score>=50 else "#ef4444"
health_label = "GOOD"    if health_score>=70 else "FAIR"    if health_score>=50 else "CRITICAL"
date_str = now.strftime("%B %d, %Y")

PR_COLOR = {"Highest":"#dc2626","High":"#ea580c"}

def make_row(item):
    bg   = {"#dc2626":"#fff5f5","#ea580c":"#fffbeb","#ca8a04":"#fefce8"}.get(item["flag_color"],"#fff")
    prc  = PR_COLOR.get(item["priority"],"#6b7280")
    summ = item["summary"][:60] + ("…" if len(item["summary"])>60 else "")
    df   = item["days_flag"] if item["days_flag"] is not None else "—"
    return f"""
<tr style="background:{bg};">
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">
    <a href="{JIRA_BASE}/browse/{item['key']}" style="color:#2563eb;font-weight:600;text-decoration:none;">{item['key']}</a></td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-size:13px;">{summ}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{item['assignee']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{item['status']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">{item['deadline_html']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{df}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;"><span style="color:{prc};font-weight:600;">{item['priority']}</span></td>
  <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;">
    <span style="background:{item['flag_color']};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">{item['flag_text']}</span></td>
</tr>"""

overdue_sorted = sorted(overdue, key=lambda x: x["days_flag"] if isinstance(x["days_flag"],(int,float)) else 0, reverse=True)
exception_rows = "".join(make_row(i) for i in overdue_sorted + blocked + stalled + not_started)

wins_html = "".join(
    f'<li style="margin-bottom:6px;"><a href="{JIRA_BASE}/browse/{i["key"]}" '
    f'style="color:#2563eb;font-weight:600;">{i["key"]}</a> — '
    f'{i["summary"][:80]} <span style="color:#6b7280;">({i["assignee"]})</span></li>'
    for i in completed_today
) or f'<p style="color:#b45309;font-weight:500;">No tasks completed in last 24h. Sprint {time_pct}% through, only {completion_pct}% complete.</p>'

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Segoe UI,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
<tr><td align="center" style="padding:20px;">
<table width="700" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">

<!-- HEADER -->
<tr><td style="background:linear-gradient(135deg,#1a1a2e,#16213e);padding:28px 32px;">
<table width="100%"><tr>
<td>
  <span style="color:#818cf8;font-size:11px;font-weight:700;background:rgba(129,140,248,.15);padding:4px 10px;border-radius:4px;">RASED V2 / RNT</span>
  <h1 style="color:#fff;margin:8px 0 4px;font-size:20px;">Daily Jira Sprint Report</h1>
  <p style="color:#94a3b8;margin:0;font-size:13px;">{date_str} &nbsp;|&nbsp; <strong style="color:#818cf8;">&#9729; GitHub Actions Cloud</strong></p>
</td>
<td align="right" style="vertical-align:top;">
<table cellspacing="8"><tr>
  <td style="background:rgba(255,255,255,.08);padding:8px 14px;border-radius:8px;text-align:center;">
    <div style="color:#94a3b8;font-size:10px;">SPRINT</div>
    <div style="color:#fff;font-size:13px;font-weight:700;">{sprint_name}</div>
    <div style="color:#94a3b8;font-size:10px;">Day {days_elapsed}/{total_days}</div></td>
  <td style="background:rgba(255,255,255,.08);padding:8px 14px;border-radius:8px;text-align:center;">
    <div style="color:#94a3b8;font-size:10px;">HEALTH</div>
    <div style="color:{health_color};font-size:22px;font-weight:800;">{health_score}</div>
    <div style="color:{health_color};font-size:10px;font-weight:600;">{health_label}</div></td>
  <td style="background:rgba(255,255,255,.08);padding:8px 14px;border-radius:8px;text-align:center;">
    <div style="color:#94a3b8;font-size:10px;">DONE</div>
    <div style="color:#fff;font-size:22px;font-weight:800;">{completion_pct}%</div>
    <div style="color:#94a3b8;font-size:10px;">{done_count}/{total_issues}</div></td>
  <td style="background:rgba(255,255,255,.08);padding:8px 14px;border-radius:8px;text-align:center;">
    <div style="color:#94a3b8;font-size:10px;">TIME</div>
    <div style="color:#eab308;font-size:22px;font-weight:800;">{time_pct}%</div>
    <div style="color:#94a3b8;font-size:10px;">{days_remaining} days left</div></td>
</tr></table></td>
</tr></table></td></tr>

<!-- EXCEPTIONS TABLE -->
<tr><td style="padding:24px 32px;">
<h2 style="color:#1e293b;font-size:16px;margin:0 0 16px;border-bottom:2px solid #ef4444;padding-bottom:8px;">
  &#9888; Exceptions and Delays</h2>
<table width="100%" cellpadding="0" cellspacing="0"
  style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;font-size:13px;">
<tr style="background:#1e293b;">
  <th style="padding:10px 12px;color:#fff;text-align:left;">Ticket</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">Summary</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">Assignee</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">Status</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">&#128197; Deadline</th>
  <th style="padding:10px 12px;color:#fff;text-align:center;">Days</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">Priority</th>
  <th style="padding:10px 12px;color:#fff;text-align:left;">Flag</th>
</tr>
{exception_rows}
</table>
<p style="color:#6b7280;font-size:11px;margin-top:8px;">
  Overdue: <strong>{len(overdue)}</strong> &nbsp;|&nbsp;
  Blocked: <strong>{len(blocked)}</strong> &nbsp;|&nbsp;
  Stalled: <strong>{len(stalled)}</strong> &nbsp;|&nbsp;
  Not Started: <strong>{len(not_started)}</strong>
</p>
</td></tr>

<!-- LEGEND -->
<tr><td style="padding:0 32px 16px;">
<p style="font-size:11px;color:#6b7280;margin:0;">
  <strong>Deadline colours:</strong> &nbsp;
  <span style="color:#dc2626;font-weight:700;">&#9888; Red</span> = past due &nbsp;|&nbsp;
  <span style="color:#ea580c;font-weight:600;">&#9200; Orange</span> = due within 3 days &nbsp;|&nbsp;
  <span style="color:#16a34a;">Green</span> = on track &nbsp;|&nbsp;
  <span style="color:#9ca3af;">&#8212;</span> = no due date set
</p>
</td></tr>

<!-- WINS -->
<tr><td style="padding:0 32px 24px;">
<h2 style="color:#1e293b;font-size:16px;margin:0 0 12px;border-bottom:2px solid #22c55e;padding-bottom:8px;">
  &#9989; Recent Wins (Completed Today)</h2>
<ul style="margin:0;padding-left:20px;">{wins_html}</ul>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#f8fafc;padding:20px 32px;border-top:1px solid #e5e7eb;">
<p style="margin:0;font-size:12px;color:#6b7280;">
  Generated by <strong>Jira Intelligence Agent v1.0.0</strong> &nbsp;|&nbsp;
  &#9729; GitHub Actions Cloud &nbsp;|&nbsp;
  {now.strftime("%Y-%m-%d %H:%M")} AST (Riyadh)</p>
<p style="margin:4px 0 0;font-size:12px;">
  <a href="{JIRA_BASE}/jira/software/c/projects/RNT/boards/112" style="color:#2563eb;">
    Open Sprint Board in Jira</a>
</p>
</td></tr>

</table></td></tr></table>
</body></html>"""

# ── 5. SEND EMAIL ─────────────────────────────────────────────────────────────
subject = (f"[Daily Jira] {sprint_name} | Day {days_elapsed}/{total_days} | "
           f"{completion_pct}% Done | Health {health_score}/100 — {date_str}")

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"]    = f"Jira Intelligence Agent <{SMTP_USER}>"
msg["To"]      = REPORT_TO
msg.attach(MIMEText(html, "html"))

print(f"Sending to {REPORT_TO} via {SMTP_HOST}:{SMTP_PORT}...")
with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
    s.starttls()
    s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(SMTP_USER, REPORT_TO, msg.as_string())
print("EMAIL SENT successfully!")

# Save HTML artifact for debugging
with open("email_report.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Saved email_report.html as artifact.")
