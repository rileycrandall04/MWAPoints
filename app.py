# app.py
# (see previous cell for full implementation notes)
import streamlit as st
st.write("Build refresh:", time.time())
import pandas as pd
import datetime as dt
import time

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import gspread

st.set_page_config(page_title="MWA Points Tracker — Live Preview", layout="wide")

# ---------------- OAuth / Config ----------------
OAUTH_CLIENT_ID = st.secrets["oauth"]["client_id"]
OAUTH_CLIENT_SECRET = st.secrets["oauth"]["client_secret"]
REDIRECT_URI = st.secrets["oauth"]["redirect_uri"]
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets"
]

# ---------------- Helpers: time + bands ----------------
def parse_time_flex(s: str):
    """
    Accept 1-4 digit numeric like '9','930','0715','2300' and return datetime.time.
    Minute precision is preserved. Returns None if invalid.
    """
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    if not s.isdigit():
        return None
    if len(s) == 1:      # 9  -> 09:00
        hh, mm = int(s), 0
    elif len(s) == 2:    # 09 -> 09:00, 23 -> 23:00
        hh, mm = int(s), 0
    elif len(s) == 3:    # 915 -> 09:15
        hh, mm = int(s[0]), int(s[1:])
    elif len(s) == 4:    # 1735 -> 17:35
        hh, mm = int(s[:2]), int(s[2:])
    else:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return dt.time(hh, mm)

def fmt_hhmm(t):
    return t.strftime("%H:%M") if isinstance(t, dt.time) else ""

def hours_overlap(start_h, end_h, a, b):
    """
    Overlap hours between [start_h,end_h) and [a,b), allowing wrap for overnight.
    """
    if end_h < start_h:
        end_h += 24
    total = 0.0
    for A, B in [(a, b), (a + 24, b + 24)]:
        lo, hi = max(start_h, A), min(end_h, B)
        if hi > lo:
            total += (hi - lo)
    return total

def split_bands(start_t: dt.time, end_t: dt.time):
    """
    Return (total, day(07-17), eve(17-23), night(23-07)).
    """
    if not (isinstance(start_t, dt.time) and isinstance(end_t, dt.time)):
        return 0.0, 0.0, 0.0, 0.0
    s = start_t.hour + start_t.minute/60
    e = end_t.hour + end_t.minute/60
    total = (e - s) % 24
    day = hours_overlap(s, e, 7, 17)
    eve = hours_overlap(s, e, 17, 23)
    night = hours_overlap(s, e, 23, 24) + hours_overlap(s, e, 0, 7)
    return total, day, eve, night

# 125% cap logic via band multipliers: base 1.00 (07–17), eve 1.10 (17–23), night 1.25 (23–07)
def cap_ar_points(base_rate, d_hrs, e_hrs, n_hrs):
    return (
        d_hrs * base_rate * 1.00 +
        e_hrs * base_rate * 1.10 +
        n_hrs * base_rate * 1.25
    )

# OB base 13 with the same capped multipliers (13, 14.3, 16.25)
def cap_ob_points(d_hrs, e_hrs, n_hrs):
    base = 13.0
    return (
        d_hrs * base * 1.00 +
        e_hrs * base * 1.10 +
        n_hrs * base * 1.25
    )

def band_breakdown(category: str, start_t: dt.time, end_t: dt.time):
    """
    Return (list_of_compact_lines, time_points_total) for one interval.
    Lines show: "Day: X.XX h @rate = Y.YY pts", one per band used.
    """
    ttl, d, e, n = split_bands(start_t, end_t)
    lines = []
    time_pts = 0.0

    if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
        base = 20.0
        d_pts = d * base * 1.00
        e_pts = e * base * 1.10
        n_pts = n * base * 1.25
        subtotal = d_pts + e_pts + n_pts
        time_pts = max(subtotal, 80.0) if category == "Assigned (General AR)" else subtotal

        if d > 0: lines.append(f"Day: {d:.2f} h @1.00× = {d_pts:.2f} pts")
        if e > 0: lines.append(f"Evening: {e:.2f} h @1.10× = {e_pts:.2f} pts")
        if n > 0: lines.append(f"Night: {n:.2f} h @1.25× = {n_pts:.2f} pts")

    elif category == "Restricted OB (In-house)":
        d_pts = d * 13.0 * 1.00
        e_pts = e * 13.0 * 1.10
        n_pts = n * 13.0 * 1.25
        time_pts = d_pts + e_pts + n_pts

        if d > 0: lines.append(f"Day: {d:.2f} h @1.00× = {d_pts:.2f} pts")
        if e > 0: lines.append(f"Evening: {e:.2f} h @1.10× = {e_pts:.2f} pts")
        if n > 0: lines.append(f"Night: {n:.2f} h @1.25× = {n_pts:.2f} pts")

    elif category == "Unrestricted Call":
        time_pts = ttl * 3.5
        if ttl > 0:
            lines.append(f"Total: {ttl:.2f} h @3.50 pts/hr = {time_pts:.2f} pts")

    elif category == "Cardiac (Subspecialty) – Coverage":
        time_pts = 45.0
        lines.append("Cardiac coverage: 45.00 pts")

    return lines, time_pts

# ---------------- OAuth helpers ----------------
def get_auth_flow(state: str):
    client_config = {
        "web": {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.params = {"access_type":"offline","include_granted_scopes":"true","prompt":"consent"}
    return flow

def login_button():
    state = st.session_state.get("oauth_state") or str(int(time.time()))
    st.session_state["oauth_state"] = state
    flow = get_auth_flow(state)
    auth_url, _ = flow.authorization_url(state=state)
    st.link_button("🔐 Sign in with Google", auth_url, use_container_width=True)

def exchange_code_for_token():
    params = st.query_params
    if "state" in params and "code" in params:
        state = params["state"]
        code = params["code"]
        flow = get_auth_flow(state)
        flow.fetch_token(code=code)
        creds = flow.credentials
        return creds
    return None

# ---------------- Sheets helpers ----------------
def ensure_user_sheet(gc):
    SPREADSHEET_NAME = "MWA Points Data"
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except Exception:
        sh = gc.create(SPREADSHEET_NAME)
    try:
        ws_entries = sh.worksheet("Entries")
    except Exception:
        ws_entries = sh.add_worksheet(title="Entries", rows=1000, cols=20)
        ws_entries.update("A1:H1", [["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"]])
    try:
        ws_holidays = sh.worksheet("Holidays")
    except Exception:
        ws_holidays = sh.add_worksheet(title="Holidays", rows=100, cols=1)
        ws_holidays.update("A1:A1", [["Holiday Dates (YYYY-MM-DD)"]])
    return sh, ws_entries, ws_holidays

def load_entries(ws_entries):
    values = ws_entries.get_all_records()
    if not values:
        return pd.DataFrame(columns=["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"])
    df = pd.DataFrame(values)
    if "Date" in df:
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
    # restore times
    for col in ["Start","End"]:
        if col in df:
            def to_time(x):
                try:
                    s = str(x).strip()
                    hh, mm = [int(t) for t in s.split(":")]
                    return dt.time(hh%24, mm%60)
                except:
                    return None
            df[col] = df[col].apply(to_time)
    for col in ["TEE Exams","Productivity Points","Extra Points"]:
        if col in df: df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    for col in ["Category","Notes"]:
        if col in df: df[col] = df[col].fillna("")
    return df

def save_entries(ws_entries, df: pd.DataFrame):
    header = ["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"]
    ws_entries.clear()
    ws_entries.update("A1:H1", [header])
    out = []
    for _, r in df.iterrows():
        date_str = r["Date"].strftime("%Y-%m-%d") if isinstance(r["Date"], dt.date) else ""
        def fmt(t):
            return t.strftime("%H:%M") if isinstance(t, dt.time) else ""
        out.append([
            date_str,
            r.get("Category",""),
            fmt(r.get("Start")),
            fmt(r.get("End")),
            r.get("TEE Exams",0),
            r.get("Productivity Points",0),
            r.get("Extra Points",0),
            r.get("Notes","")
        ])
    if out:
        ws_entries.update(f"A2:H{len(out)+1}", out)

def entry_time_points(category: str, date: dt.date, start_t: dt.time, end_t: dt.time):
    """
    Return time-based points (excl. TEE/prod/extra) for a single interval (no splitting here).
    """
    _, d, e, n = split_bands(start_t, end_t)
    ttl = ((end_t.hour + end_t.minute/60) - (start_t.hour + start_t.minute/60)) % 24

    if category == "Unrestricted Call":
        return ttl * 3.5
    if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
        base = 20.0
        points = cap_ar_points(base, d, e, n)
        if category == "Assigned (General AR)":
            points = max(points, 80.0)
        return points
    if category == "Restricted OB (In-house)":
        return cap_ob_points(d, e, n)
    if category == "Cardiac (Subspecialty) – Coverage":
        return 45.0
    return 0.0

def preview_rows(date, category, start_t, end_t, tee, prod, extra, notes):
    """
    Generate one or two rows (split at midnight if needed) for preview & save.
    The second row (next day) carries 0 for TEE/production/extra to avoid double counting.
    """
    rows = []
    if end_t < start_t:
        rows.append({
            "Date": date,
            "Category": category,
            "Start": start_t,
            "End": dt.time(23,59),
            "TEE Exams": tee,
            "Productivity Points": prod,
            "Extra Points": extra,
            "Notes": notes
        })
        rows.append({
            "Date": date + dt.timedelta(days=1),
            "Category": category,
            "Start": dt.time(0,0),
            "End": end_t,
            "TEE Exams": 0,
            "Productivity Points": 0.0,
            "Extra Points": 0.0,
            "Notes": f"(overnight from {date}) " + (notes or "")
        })
    else:
        rows.append({
            "Date": date,
            "Category": category,
            "Start": start_t,
            "End": end_t,
            "TEE Exams": tee,
            "Productivity Points": prod,
            "Extra Points": extra,
            "Notes": notes
        })
    return rows

# ---------------- App start ----------------
st.title("MWA Points Tracker — Live Preview")

# Auth
creds = st.session_state.get("creds")
if not creds:
    maybe = exchange_code_for_token()
    if maybe:
        st.session_state["creds"] = maybe
        creds = maybe
if not creds:
    st.info("Sign in with Google to save your data to your own Drive.")
    login_button()
    st.stop()

# Sheets
try:
    gc = gspread.authorize(creds)
    sh, ws_entries, ws_holidays = ensure_user_sheet(gc)
except Exception as e:
    st.error(f"Google Sheets/Drive error: {e}")
    st.stop()

entries = load_entries(ws_entries)

CATEGORIES = [
    "Assigned (General AR)",
    "Activation from Unrestricted Call",
    "Restricted OB (In-house)",
    "Unrestricted Call",
    "Cardiac (Subspecialty) – Coverage",
]

tab_entries, tab_summary, tab_reports = st.tabs(["Entries","Summary","Reports"])

with tab_entries:
    st.subheader("Add Entry")
    with st.form("add_entry_form", clear_on_submit=False):
        c = st.columns([1,1.4,1,1,1,1,1,2])
        date = c[0].date_input("Date", value=dt.date.today())
        category = c[1].selectbox("Category", CATEGORIES, index=0)
        start_str = c[2].text_input("Start (HHMM, e.g., 700 or 1735)", "")
        end_str   = c[3].text_input("End (HHMM, e.g., 945 or 2310)", "")
        tee   = c[4].number_input("TEE Exams", min_value=0, step=1, value=0)
        prod  = c[5].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0)
        extra = c[6].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0)
        notes = c[7].text_input("Notes","")

        # Live preview (before submit)
        start_t = parse_time_flex(start_str) if start_str else None
        end_t   = parse_time_flex(end_str) if end_str else None
        preview_box = st.empty()

        can_preview = True
        preview_msgs = []
        if start_str and start_t is None:
            preview_msgs.append("❌ Start time is invalid. Enter 1–4 digits like 7, 930, 0715, or 2300.")
            can_preview = False
        if end_str and end_t is None:
            preview_msgs.append("❌ End time is invalid. Enter 1–4 digits like 7, 930, 0715, or 2300.")
            can_preview = False

        if start_t and end_t and can_preview:
            rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes)

            per_row_msgs = []
            new_time_points = 0.0
            new_prod_points = 0.0

            for r in rows:
                # Band-by-band compact lines
                lines, tp_time = band_breakdown(r["Category"], r["Start"], r["End"])
                # Add TEE to time points (+22 each)
                tp_time += 22.0 * float(r.get("TEE Exams", 0) or 0)

                # Compose per-row output: header + band rows (each on its own line)
                row_lines = []
                row_lines.append(f"- {r['Date']} | Time points: **{tp_time:.2f}**")
                for ln in lines:
                    row_lines.append(ln)

                per_row_msgs.append("\n".join(row_lines))
                new_time_points += tp_time
                new_prod_points += float(r.get("Productivity Points", 0.0) or 0.0)

            # Current daily total for the chosen date (existing data)
            current_daily_total = 0.0
            if not entries.empty:
                same_day = entries[entries["Date"] == date]
                for _, er in same_day.iterrows():
                    _, tptime = band_breakdown(er["Category"], er["Start"], er["End"])
                    tptime += 22.0 * float(er.get("TEE Exams", 0) or 0)
                    tptime += float(er.get("Productivity Points", 0) or 0)
                    tptime += float(er.get("Extra Points", 0) or 0)
                    current_daily_total += tptime

            projected_daily_total = current_daily_total + new_time_points + new_prod_points + float(extra or 0.0)

            # Render preview
            preview_html = "### Preview\n" + "\n\n".join(per_row_msgs)
            preview_html += f"\n\n**New time points:** {new_time_points:.2f}"
            preview_html += f"\n**New production points:** {new_prod_points:.2f}"
            if float(extra or 0.0) != 0.0:
                preview_html += f"\n**New extra points:** {float(extra):.2f}"
            preview_html += f"\n\n**Projected total for {date}: {projected_daily_total:.2f} points**"
            preview_box.markdown(preview_html)

        elif preview_msgs:
            preview_box.error("\n".join(preview_msgs))

        submitted = st.form_submit_button("Add")
        if submitted:
            if not start_t or not end_t:
                st.error("Both start and end times are required (valid HHMM).")
                st.stop()

            rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes)
            new_df = pd.DataFrame(rows)
            entries = pd.concat([entries, new_df], ignore_index=True)
            save_entries(ws_entries, entries)
            st.success("Saved entry" + (" (split overnight into two rows)" if len(rows)==2 else "") + ".")
            st.experimental_rerun()

    # Show entries table
    st.subheader("Your Entries")
    if entries.empty:
        st.info("No entries yet.")
    else:
        show = entries.copy()
        show["Start"] = show["Start"].apply(fmt_hhmm)
        show["End"]   = show["End"].apply(fmt_hhmm)
        st.dataframe(show, use_container_width=True, hide_index=True)

with tab_summary:
    st.subheader("Daily Summary")
    if entries.empty:
        st.info("No data yet.")
    else:
        comp = []
        for _, r in entries.iterrows():
            _, tp = band_breakdown(r["Category"], r["Start"], r["End"])
            tp += 22.0 * float(r.get("TEE Exams", 0) or 0)
            total = tp + float(r.get("Productivity Points", 0) or 0) + float(r.get("Extra Points", 0) or 0)
            comp.append({"Date": r["Date"], "Total Points": total})
        daily = pd.DataFrame(comp).groupby("Date", as_index=False)["Total Points"].sum().sort_values("Date")
        daily["Running Monthly Total"] = daily["Total Points"].cumsum()
        st.dataframe(daily, use_container_width=True, hide_index=True)

with tab_reports:
    st.subheader("Exports")
    if entries.empty:
        st.info("No data yet.")
    else:
        out_entries = entries.copy()
        out_entries["Date"] = out_entries["Date"].apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
        out_entries["Start"] = out_entries["Start"].apply(fmt_hhmm)
        out_entries["End"]   = out_entries["End"].apply(fmt_hhmm)
        st.download_button("Download Entries CSV", out_entries.to_csv(index=False).encode("utf-8"), "entries.csv", "text/csv")

        comp = []
        for _, r in entries.iterrows():
            _, tp = band_breakdown(r["Category"], r["Start"], r["End"])
            tp += 22.0 * float(r.get("TEE Exams", 0) or 0)
            total = tp + float(r.get("Productivity Points", 0) or 0) + float(r.get("Extra Points", 0) or 0)
            comp.append({"Date": r["Date"], "Total Points": total})
        daily = pd.DataFrame(comp).groupby("Date", as_index=False)["Total Points"].sum().sort_values("Date")
        st.download_button("Download Daily Summary CSV", daily.to_csv(index=False).encode("utf-8"), "daily_summary.csv", "text/csv")
