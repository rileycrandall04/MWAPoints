
import streamlit as st
import pandas as pd
import datetime as dt
import time

from google_auth_oauthlib.flow import Flow
import gspread
from gspread_formatting import format_cell_range, CellFormat, Color, TextFormat

st.set_page_config(page_title="MWA Points Tracker — Live Preview", layout="wide")

# Force rebuild indicator
st.write("Build refresh:", time.time())

# ---------------- OAuth / Config ----------------
OAUTH_CLIENT_ID = st.secrets["oauth"]["client_id"]
OAUTH_CLIENT_SECRET = st.secrets["oauth"]["client_secret"]
REDIRECT_URI = st.secrets["oauth"]["redirect_uri"]
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ---------------- Helpers: bands & math ----------------
def fmt_hhmm(t):
    return t.strftime("%H:%M") if isinstance(t, dt.time) else ""

def hours_overlap(start_h, end_h, a, b):
    if end_h < start_h:
        end_h += 24
    total = 0.0
    for A, B in [(a, b), (a + 24, b + 24)]:
        lo, hi = max(start_h, A), min(end_h, B)
        if hi > lo:
            total += (hi - lo)
    return total

def split_bands(start_t: dt.time, end_t: dt.time):
    if not (isinstance(start_t, dt.time) and isinstance(end_t, dt.time)):
        return 0.0, 0.0, 0.0, 0.0
    s = start_t.hour + start_t.minute/60
    e = end_t.hour + end_t.minute/60
    total = (e - s) % 24
    day = hours_overlap(s, e, 7, 17)
    eve = hours_overlap(s, e, 17, 23)
    night = hours_overlap(s, e, 23, 24) + hours_overlap(s, e, 0, 7)
    return total, day, eve, night

def cap_ar_points(base_rate, d_hrs, e_hrs, n_hrs):
    return d_hrs*base_rate*1.00 + e_hrs*base_rate*1.10 + n_hrs*base_rate*1.25

def cap_ob_points(d_hrs, e_hrs, n_hrs):
    base=13.0
    return d_hrs*base*1.00 + e_hrs*base*1.10 + n_hrs*base*1.25

def band_breakdown(category: str, start_t: dt.time, end_t: dt.time):
    ttl, d, e, n = split_bands(start_t, end_t)
    lines=[]; time_pts=0.0

    if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
        base=20.0
        d_pts=d*base*1.00; e_pts=e*base*1.10; n_pts=n*base*1.25
        subtotal=d_pts+e_pts+n_pts
        time_pts = max(subtotal, 80.0) if category=="Assigned (General AR)" else subtotal
        if d>0: lines.append(f"Day: {d:.2f} h @1.00× = {d_pts:.2f} pts")
        if e>0: lines.append(f"Evening: {e:.2f} h @1.10× = {e_pts:.2f} pts")
        if n>0: lines.append(f"Night: {n:.2f} h @1.25× = {n_pts:.2f} pts")

    elif category=="Restricted OB (In-house)":
        d_pts=d*13.0*1.00; e_pts=e*13.0*1.10; n_pts=n*13.0*1.25
        time_pts=d_pts+e_pts+n_pts
        if d>0: lines.append(f"Day: {d:.2f} h @1.00× = {d_pts:.2f} pts")
        if e>0: lines.append(f"Evening: {e:.2f} h @1.10× = {e_pts:.2f} pts")
        if n>0: lines.append(f"Night: {n:.2f} h @1.25× = {n_pts:.2f} pts")

    elif category=="Unrestricted Call":
        time_pts = ttl*3.5
        if ttl>0: lines.append(f"Total: {ttl:.2f} h @3.50 pts/hr = {time_pts:.2f} pts")

    elif category=="Cardiac (Subspecialty) – Coverage":
        time_pts=45.0
        lines.append("Cardiac coverage: 45.00 pts")

    return lines, time_pts

def compute_entry_time_points(row):
    cat = row["Category"]
    s = row["Start"]; e = row["End"]
    lines, tp = band_breakdown(cat, s, e)
    tp += 22.0 * float(row.get("TEE Exams", 0) or 0)
    return tp

def entry_time_points(category: str, start_t: dt.time, end_t: dt.time):
    _, d, e, n = split_bands(start_t, end_t)
    ttl = ((end_t.hour + end_t.minute/60) - (start_t.hour + start_t.minute/60)) % 24
    if category=="Unrestricted Call":
        return ttl*3.5
    if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
        pts = cap_ar_points(20.0, d, e, n)
        if category=="Assigned (General AR)":
            pts = max(pts, 80.0)
        return pts
    if category=="Restricted OB (In-house)":
        return cap_ob_points(d, e, n)
    if category=="Cardiac (Subspecialty) – Coverage":
        return 45.0
    return 0.0

def preview_rows(date, category, start_t, end_t, tee, prod, extra, notes):
    rows=[]
    if end_t < start_t:  # overnight -> split
        rows.append({
            "Date": date, "Category": category,
            "Start": start_t, "End": dt.time(23,59),
            "TEE Exams": tee, "Productivity Points": prod, "Extra Points": extra, "Notes": notes
        })
        rows.append({
            "Date": date + dt.timedelta(days=1), "Category": category,
            "Start": dt.time(0,0), "End": end_t,
            "TEE Exams": 0, "Productivity Points": 0.0, "Extra Points": 0.0,
            "Notes": f"(overnight from {date}) " + (notes or "")
        })
    else:
        rows.append({
            "Date": date, "Category": category,
            "Start": start_t, "End": end_t,
            "TEE Exams": tee, "Productivity Points": prod, "Extra Points": extra, "Notes": notes
        })
    return rows

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
        return flow.credentials
    return None

# ---------------- Sheets helpers ----------------
def ensure_user_sheet(gc):
    SPREADSHEET_NAME = "MWA Points Data"
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except Exception:
        sh = gc.create(SPREADSHEET_NAME)
    # Entries
    try:
        ws_entries = sh.worksheet("Entries")
    except Exception:
        ws_entries = sh.add_worksheet(title="Entries", rows=1000, cols=20)
        ws_entries.update("A1:H1", [["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"]])
    # Daily Totals
    try:
        ws_daily = sh.worksheet("Daily Totals")
    except Exception:
        ws_daily = sh.add_worksheet(title="Daily Totals", rows=1000, cols=10)
        ws_daily.update("A1:F1", [["Date","Time Points","Productivity Points","Extra Points","Total Points","Running Monthly Total"]])
    # Monthly Summary
    try:
        ws_msum = sh.worksheet("Monthly Summary")
    except Exception:
        ws_msum = sh.add_worksheet(title="Monthly Summary", rows=200, cols=3)
        ws_msum.update("A1:B1", [["Month","Total Points"]])
    return sh, ws_entries, ws_daily, ws_msum

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

def write_daily_totals(sh, df_entries):
    if df_entries.empty:
        ws = sh.worksheet("Daily Totals")
        ws.clear()
        ws.update("A1:F1", [["Date","Time Points","Productivity Points","Extra Points","Total Points","Running Monthly Total"]])
        return

    dfe = df_entries.copy()
    dfe["Time Points"] = dfe.apply(compute_entry_time_points, axis=1)
    dfe["Prod"] = pd.to_numeric(dfe["Productivity Points"], errors="coerce").fillna(0.0)
    dfe["Extra"] = pd.to_numeric(dfe["Extra Points"], errors="coerce").fillna(0.0)
    daily = dfe.groupby("Date", as_index=False).agg({
        "Time Points":"sum",
        "Prod":"sum",
        "Extra":"sum"
    })
    daily["Total Points"] = daily["Time Points"] + daily["Prod"] + daily["Extra"]

    daily = daily.sort_values("Date")
    daily["YearMonth"] = daily["Date"].apply(lambda d: dt.date(d.year, d.month, 1))
    daily["Running Monthly Total"] = daily.groupby("YearMonth")["Total Points"].cumsum()
    daily = daily.drop(columns=["YearMonth"])

    ws = sh.worksheet("Daily Totals")
    ws.clear()
    ws.update("A1:F1", [["Date","Time Points","Productivity Points","Extra Points","Total Points","Running Monthly Total"]])
    out = []
    for _, r in daily.iterrows():
        out.append([
            r["Date"].strftime("%Y-%m-%d"),
            round(float(r["Time Points"]),2),
            round(float(r["Prod"]),2),
            round(float(r["Extra"]),2),
            round(float(r["Total Points"]),2),
            round(float(r["Running Monthly Total"]),2)
        ])
    if out:
        ws.update(f"A2:F{len(out)+1}", out)

def month_tab_name(d: dt.date):
    return d.strftime("%b %Y")

def ensure_month_sheet(sh, name):
    try:
        ws = sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=1000, cols=12)
        ws.update("A1:I1", [["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes","Entry Total Points"]])
    return ws

def write_month_sheets(sh, df_entries):
    if df_entries.empty:
        return
    dfe = df_entries.copy()
    dfe["Time Points"] = dfe.apply(compute_entry_time_points, axis=1)
    dfe["Prod"] = pd.to_numeric(dfe["Productivity Points"], errors="coerce").fillna(0.0)
    dfe["Extra"] = pd.to_numeric(dfe["Extra Points"], errors="coerce").fillna(0.0)
    dfe["Entry Total Points"] = dfe["Time Points"] + dfe["Prod"] + dfe["Extra"]
    dfe["MonthName"] = dfe["Date"].apply(month_tab_name)

    for mname, chunk in dfe.groupby("MonthName"):
        ws = ensure_month_sheet(sh, mname)
        ws.clear()
        ws.update("A1:I1", [["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes","Entry Total Points"]])

        rows = []
        for _, r in chunk.sort_values("Date").iterrows():
            rows.append([
                r["Date"].strftime("%Y-%m-%d"),
                r["Category"],
                fmt_hhmm(r["Start"]),
                fmt_hhmm(r["End"]),
                int(r["TEE Exams"] or 0),
                round(float(r["Productivity Points"] or 0),2),
                round(float(r["Extra Points"] or 0),2),
                r.get("Notes",""),
                round(float(r["Entry Total Points"]),2)
            ])

        start_row = 2
        if rows:
            ws.update(f"A{start_row}:I{start_row+len(rows)-1}", rows)

        total_points = round(float(chunk["Entry Total Points"].sum()),2)
        total_row = start_row + len(rows)
        ws.update(f"A{total_row}:I{total_row}", [["","MONTH TOTAL","","","","","","", total_points]])

        fmt = CellFormat(
            backgroundColor=Color(red=1.0, green=1.0, blue=0.8),
            textFormat=TextFormat(bold=True)
        )
        format_cell_range(ws, f"A{total_row}:I{total_row}", fmt)

def write_monthly_summary(sh, df_entries):
    ws = sh.worksheet("Monthly Summary")
    ws.clear()
    ws.update("A1:B1", [["Month","Total Points"]])
    if df_entries.empty:
        return
    dfe = df_entries.copy()
    dfe["Time Points"] = dfe.apply(compute_entry_time_points, axis=1)
    dfe["Prod"] = pd.to_numeric(dfe["Productivity Points"], errors="coerce").fillna(0.0)
    dfe["Extra"] = pd.to_numeric(dfe["Extra Points"], errors="coerce").fillna(0.0)
    dfe["Entry Total"] = dfe["Time Points"] + dfe["Prod"] + dfe["Extra"]
    dfe["MonthStart"] = dfe["Date"].apply(lambda d: dt.date(d.year, d.month, 1))
    per_month = dfe.groupby("MonthStart", as_index=False)["Entry Total"].sum().sort_values("MonthStart")

    rows=[]
    for _, r in per_month.iterrows():
        label = r["MonthStart"].strftime("%b %Y")
        rows.append([label, round(float(r["Entry Total"]),2)])
    if rows:
        ws.update(f"A2:B{len(rows)+1}", rows)
    grand_total = round(float(per_month["Entry Total"].sum()),2) if not per_month.empty else 0.0
    total_row = len(rows)+2
    ws.update(f"A{total_row}:B{total_row}", [["Grand Total", grand_total]])
    fmt = CellFormat(
        backgroundColor=Color(red=1.0, green=1.0, blue=0.8),
        textFormat=TextFormat(bold=True)
    )
    format_cell_range(ws, f"A{total_row}:B{total_row}", fmt)

# ---------------- App ----------------
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
    sh, ws_entries, ws_daily, ws_msum = ensure_user_sheet(gc)
except Exception as e:
    st.error(f"Google Sheets/Drive error: {e}")
    st.stop()

# Load all entries
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
        c = st.columns([1,1.5,1,1,1,1,1,2])
        date = c[0].date_input("Date", value=dt.date.today())
        category = c[1].selectbox("Category", CATEGORIES, index=0)
        start_t = c[2].time_input("Start time", value=dt.time(7,0), step=dt.timedelta(minutes=1))
        end_t   = c[3].time_input("End time", value=dt.time(17,0), step=dt.timedelta(minutes=1))
        tee   = c[4].number_input("TEE Exams", min_value=0, step=1, value=0)
        prod  = c[5].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0)
        extra = c[6].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0)
        notes = c[7].text_input("Notes","")

        preview_box = st.empty()

        rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes)
        per_row_msgs=[]; new_time_points=0.0; new_prod_points=0.0

        for r in rows:
            lines, tp_time = band_breakdown(r["Category"], r["Start"], r["End"])
            tp_time += 22.0 * float(r.get("TEE Exams", 0) or 0)
            row_lines = [f"- {r['Date']} | Time points: **{tp_time:.2f}**"] + lines
            per_row_msgs.append("\n".join(row_lines))
            new_time_points += tp_time
            new_prod_points += float(r.get("Productivity Points", 0.0) or 0.0)

        # Current day running total (before adding)
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
        preview_html = "### Preview\n" + "\n\n".join(per_row_msgs)
        preview_html += f"\n\n**New time points:** {new_time_points:.2f}"
        preview_html += f"\n**New production points:** {new_prod_points:.2f}"
        if float(extra or 0.0) != 0.0:
            preview_html += f"\n**New extra points:** {float(extra):.2f}"
        preview_html += f"\n\n**Projected total for {date}: {projected_daily_total:.2f} points**"
        preview_box.markdown(preview_html)

        submitted = st.form_submit_button("Add")
        if submitted:
            # Save new rows
            new_df = pd.DataFrame(rows)
            entries = pd.concat([entries, new_df], ignore_index=True)
            save_entries(ws_entries, entries)

            # Auto-update sheets
            write_daily_totals(sh, entries)
            write_month_sheets(sh, entries)
            write_monthly_summary(sh, entries)

            st.success("Saved entry and updated Daily Totals, Monthly sheets, and Monthly Summary.")
            st.experimental_rerun()

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
            lines, tp = band_breakdown(r["Category"], r["Start"], r["End"])
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
