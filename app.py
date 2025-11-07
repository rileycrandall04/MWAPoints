
import streamlit as st
import pandas as pd
import datetime as dt
import json, time, io
from urllib.parse import urlencode, urlparse, parse_qs

# Google auth libs
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import gspread

st.set_page_config(page_title="MWA Points Tracker", layout="wide")

# ---------------------------- CONFIG / SECRETS ----------------------------
# Put these into Streamlit Cloud Secrets:
# [oauth]
# client_id = "YOUR_GOOGLE_OAUTH_CLIENT_ID"
# client_secret = "YOUR_GOOGLE_OAUTH_CLIENT_SECRET"
# redirect_uri = "https://YOUR-STREAMLIT-APP-URL"   # no trailing slash
#
# We request minimal scopes so each user's data stays in THEIR Drive:
# - drive.file lets the app create/open files it creates
# - spreadsheets for reading/writing their Google Sheets
OAUTH_CLIENT_ID = st.secrets["oauth"]["client_id"]
OAUTH_CLIENT_SECRET = st.secrets["oauth"]["client_secret"]
REDIRECT_URI = st.secrets["oauth"]["redirect_uri"]
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets"
]

APP_FOLDER = "MWA Points Tracker"
SPREADSHEET_NAME = "MWA Points Data"

# ---------------------------- HELPER FUNCTIONS ----------------------------
def parse_time_str(s: str):
    if not s: return None
    try:
        h, m = [int(x) for x in s.split(":")]
        return dt.time(h%24, m%60)
    except:
        return None

def hours_mod(start: dt.time, end: dt.time) -> float:
    if not start or not end: return 0.0
    s = start.hour + start.minute/60
    e = end.hour + end.minute/60
    return (e - s) % 24

def overlap_hours(start: float, end: float, a: float, b: float) -> float:
    if end < start: end += 24
    total = 0.0
    for A,B in [(a,b),(a+24,b+24)]:
        lo, hi = max(start,A), min(end,B)
        if hi > lo: total += (hi-lo)
    return total

def band_split(start_t, end_t):
    if not isinstance(start_t, dt.time) or not isinstance(end_t, dt.time):
        return 0.0,0.0,0.0,0.0
    s = start_t.hour + start_t.minute/60
    e = end_t.hour + end_t.minute/60
    total = hours_mod(start_t, end_t)
    day = overlap_hours(s,e,7,17)
    eve = overlap_hours(s,e,17,23)
    night = overlap_hours(s,e,23,24) + overlap_hours(s,e,0,7)
    return total, day, eve, night

def is_we_or_holiday(date_obj, holidays_set):
    return (date_obj.isoweekday() >= 6) or (date_obj in holidays_set)

def compute_points(row, holidays_set):
    date = row.get("Date")
    category = row.get("Category")
    start = row.get("Start")
    end = row.get("End")
    tee = float(row.get("TEE Exams", 0) or 0)
    prod = float(row.get("Productivity Points", 0) or 0)
    extra = float(row.get("Extra Points", 0) or 0)

    ttl, day_h, eve_h, night_h = band_split(start,end)
    weho = is_we_or_holiday(date, holidays_set) if isinstance(date, dt.date) else False

    auto = 0.0
    if category == "Unrestricted Call":
        auto = ttl * 3.5
    elif category in ("Assigned (General AR)","Activation from Unrestricted Call"):
        if weho: auto = day_h*22 + eve_h*25 + night_h*25
        else:    auto = day_h*20 + eve_h*22 + night_h*25
        if category == "Assigned (General AR)":
            auto = max(auto, 80.0)  # 4-hr min per entry
    elif category == "Restricted OB (In-house)":
        if weho: auto = day_h*14.3 + eve_h*16.25 + night_h*16.25
        else:    auto = day_h*13 + eve_h*14.3 + night_h*16.25
    elif category == "Cardiac (Subspecialty) – Coverage":
        auto = 45.0

    entry_total = auto + 22.0*tee + prod + extra
    return auto, entry_total, ttl, day_h, eve_h, night_h

def hhmm(x: float):
    h = int(x); m = int(round((x-h)*60))
    if m==60: h,m = h+1,0
    return f"{h}:{m:02d}"

# ---------------------------- GOOGLE OAUTH ----------------------------
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
    params = st.query_params  # Streamlit Cloud
    if "state" in params and "code" in params:
        state = params["state"]
        code = params["code"]
        flow = get_auth_flow(state)
        flow.fetch_token(code=code)
        creds = flow.credentials
        return creds
    return None

def creds_to_gc(creds: Credentials):
    return gspread.authorize(creds)

# ---------------------------- DRIVE / SHEETS (per-user) ----------------------------
def ensure_user_sheet(gc: gspread.Client):
    # Create or open a spreadsheet named SPREADSHEET_NAME in user's Drive.
    # Use 'drive.file' scope so file is owned by the user.
    # Try open; if not found, create.
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except Exception:
        sh = gc.create(SPREADSHEET_NAME)
    # Ensure worksheets exist
    ws_entries = None
    ws_holidays = None
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
    # types
    if "Date" in df:
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
    for col in ["Start","End"]:
        if col in df:
            def to_time(s):
                try:
                    hh,mm = [int(x) for x in str(s).split(":")]
                    return dt.time(hh%24, mm%60)
                except: return None
            df[col] = df[col].apply(to_time)
    for col in ["TEE Exams","Productivity Points","Extra Points"]:
        if col in df: df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    for col in ["Category","Notes"]:
        if col in df: df[col] = df[col].fillna("")
    return df

def save_entries(ws_entries, df: pd.DataFrame):
    # Write header + data (as strings / HH:MM for times)
    header = ["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"]
    ws_entries.clear()
    ws_entries.update("A1:H1", [header])
    out = []
    for _, r in df.iterrows():
        date_str = r["Date"].strftime("%Y-%m-%d") if pd.notnull(r["Date"]) else ""
        def fmt(t):
            return t.strftime("%H:%M") if isinstance(t, dt.time) else ""
        row = [
            date_str,
            r.get("Category",""),
            fmt(r.get("Start")),
            fmt(r.get("End")),
            r.get("TEE Exams",0),
            r.get("Productivity Points",0),
            r.get("Extra Points",0),
            r.get("Notes","")
        ]
        out.append(row)
    if out:
        ws_entries.update(f"A2:H{len(out)+1}", out)

def load_holidays(ws_holidays):
    vals = ws_holidays.col_values(1)[1:]  # skip header
    result = set()
    for s in vals:
        try:
            result.add(dt.datetime.strptime(s.strip(), "%Y-%m-%d").date())
        except: pass
    return result

def add_holiday(ws_holidays, date_obj: dt.date):
    last_row = len(ws_holidays.col_values(1)) + 1
    ws_holidays.update_cell(last_row, 1, date_obj.strftime("%Y-%m-%d"))

# ---------------------------- APP START ----------------------------
st.title("MWA Points Tracker — Cloud Dashboard")

# 1) LOGIN
creds = None
if "creds" in st.session_state:
    creds = st.session_state.creds

if not creds:
    creds = exchange_code_for_token()
    if creds:
        st.session_state.creds = creds

if not creds:
    st.info("Sign in with Google to save your data in your own Google Drive.")
    login_button()
    st.stop()

# 2) OPEN USER DRIVE FILES
try:
    gc = creds_to_gc(creds)
    sh, ws_entries, ws_holidays = ensure_user_sheet(gc)
except Exception as e:
    st.error(f"Google Sheets/Drive error: {e}")
    st.stop()

# 3) LOAD DATA
entries = load_entries(ws_entries)
holidays = load_holidays(ws_holidays)

CATEGORIES = [
    "Assigned (General AR)",
    "Activation from Unrestricted Call",
    "Restricted OB (In-house)",
    "Unrestricted Call",
    "Cardiac (Subspecialty) – Coverage",
]

st.success("Signed in with Google. Your data will save to your own Drive.")

# ---------------------------- DASHBOARD LAYOUT ----------------------------
tab_entries, tab_summary, tab_analytics, tab_reports, tab_settings = st.tabs(
    ["Entries","Summary","Analytics","Reports","Settings"]
)

with tab_entries:
    st.subheader("Add Entry")
    with st.form("add_entry_form", clear_on_submit=True):
        c = st.columns([1,1.4,1,1,1,1,1,2])
        date = c[0].date_input("Date", value=dt.date.today())
        category = c[1].selectbox("Category", CATEGORIES, index=0)
        start = c[2].text_input("Start (HH:MM)", "")
        end   = c[3].text_input("End (HH:MM)", "")
        tee   = c[4].number_input("TEE Exams", min_value=0, step=1, value=0)
        prod  = c[5].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0)
        extra = c[6].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0)
        notes = c[7].text_input("Notes","")
        submitted = st.form_submit_button("Add")
        if submitted:
            new = pd.DataFrame([{
                "Date": date,
                "Category": category,
                "Start": parse_time_str(start),
                "End": parse_time_str(end),
                "TEE Exams": tee,
                "Productivity Points": prod,
                "Extra Points": extra,
                "Notes": notes
            }])
            entries = pd.concat([entries, new], ignore_index=True)
            save_entries(ws_entries, entries)
            st.experimental_rerun()

    st.subheader("Your Entries")
    if entries.empty:
        st.info("No entries yet.")
    else:
        # Compute per-entry results
        comp = []
        for _, r in entries.iterrows():
            a, total, ttl, d,e,n = compute_points(r, holidays)
            comp.append({
                "Auto Points": round(a,2),
                "Entry Points Total": round(total,2),
                "Hours": ttl, "Day": d, "Evening": e, "Night": n
            })
        comp = pd.DataFrame(comp)
        show = pd.concat([entries.reset_index(drop=True), comp], axis=1)

        # Pretty times/hours
        show["Start"] = show["Start"].apply(lambda t: t.strftime("%H:%M") if isinstance(t, dt.time) else "")
        show["End"]   = show["End"].apply(lambda t: t.strftime("%H:%M") if isinstance(t, dt.time) else "")
        show["Hours (hh:mm)"]   = show["Hours"].apply(hhmm)
        show["Day (hh:mm)"]     = show["Day"].apply(hhmm)
        show["Evening (hh:mm)"] = show["Evening"].apply(hhmm)
        show["Night (hh:mm)"]   = show["Night"].apply(hhmm)
        show = show.drop(columns=["Hours","Day","Evening","Night"])

        st.dataframe(show, use_container_width=True, hide_index=True)

with tab_summary:
    st.subheader("Daily Summary")
    if entries.empty:
        st.info("No data yet.")
    else:
        comp = []
        for _, r in entries.iterrows():
            a, total, ttl, d,e,n = compute_points(r, holidays)
            comp.append({"Date": r["Date"], "Entry Points Total": total})
        comp = pd.DataFrame(comp)
        daily = comp.groupby("Date", as_index=False)["Entry Points Total"].sum()
        daily = daily.sort_values("Date")
        daily["Running Monthly Total"] = daily["Entry Points Total"].cumsum()
        st.dataframe(daily, use_container_width=True, hide_index=True)

with tab_analytics:
    import altair as alt
    st.subheader("Analytics")
    if entries.empty:
        st.info("No data yet.")
    else:
        comp = []
        for _, r in entries.iterrows():
            a, total, ttl, d,e,n = compute_points(r, holidays)
            comp.append({**r, "Total": total, "Hours": ttl})
        df = pd.DataFrame(comp)
        df["Date"] = pd.to_datetime(df["Date"])
        chart1 = alt.Chart(df).mark_bar().encode(
            x="yearmonth(Date):T",
            y="sum(Total):Q",
            color="Category:N",
            tooltip=["yearmonth(Date):T","sum(Total):Q"]
        ).properties(height=280)
        st.altair_chart(chart1, use_container_width=True)

with tab_reports:
    st.subheader("Exports")
    if entries.empty:
        st.info("No data yet.")
    else:
        # Build CSVs
        out_entries = entries.copy()
        out_entries["Date"] = out_entries["Date"].apply(lambda d: d.strftime("%Y-%m-%d") if isinstance(d, dt.date) else "")
        out_entries["Start"] = out_entries["Start"].apply(lambda t: t.strftime("%H:%M") if isinstance(t, dt.time) else "")
        out_entries["End"]   = out_entries["End"].apply(lambda t: t.strftime("%H:%M") if isinstance(t, dt.time) else "")
        st.download_button("Download Entries CSV", out_entries.to_csv(index=False).encode("utf-8"), "entries.csv", "text/csv")
        # Summary CSV
        comp = []
        for _, r in entries.iterrows():
            a, total, ttl, d,e,n = compute_points(r, holidays)
            comp.append({"Date": r["Date"], "Entry Points Total": total})
        comp = pd.DataFrame(comp)
        daily = comp.groupby("Date", as_index=False)["Entry Points Total"].sum().sort_values("Date")
        st.download_button("Download Daily Summary CSV", daily.to_csv(index=False).encode("utf-8"), "daily_summary.csv", "text/csv")

with tab_settings:
    st.subheader("Holidays")
    col1, col2 = st.columns([1,3])
    hdate = col1.date_input("Add holiday", value=None)
    if col1.button("Add"):
        if hdate:
            add_holiday(ws_holidays, hdate)
            st.experimental_rerun()
    if holidays:
        st.write("Current holidays:")
        st.write(", ".join(sorted([d.strftime("%Y-%m-%d") for d in holidays])))
    if st.button("Sign out"):
        for k in ["creds","oauth_state"]:
            if k in st.session_state: del st.session_state[k]
        st.success("Signed out.")
        st.stop()
