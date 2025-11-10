
import streamlit as st
import pandas as pd
import datetime as dt
import time
from typing import List, Dict, Tuple, Optional

from google_auth_oauthlib.flow import Flow
import gspread
from gspread_formatting import format_cell_range, CellFormat, Color, TextFormat

st.set_page_config(page_title="MWA Points Tracker — Live Preview", layout="wide")
st.caption(f"Optimized build @ {int(time.time())}")

OAUTH_CLIENT_ID = st.secrets["oauth"]["client_id"]
OAUTH_CLIENT_SECRET = st.secrets["oauth"]["client_secret"]
REDIRECT_URI = st.secrets["oauth"]["redirect_uri"]
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

CATEGORIES = [
    "Assigned (General AR)",
    "Activation from Unrestricted Call",
    "Restricted OB (In-house)",
    "Unrestricted Call",
    "Cardiac (Subspecialty) - Coverage",
]
AR_BASE = 20.0  # points/hour baseline for Assigned & Activation

def fmt_hhmm(t: Optional[dt.time]) -> str:
    return t.strftime("%H:%M") if isinstance(t, dt.time) else ""

def parse_time_any(txt: str) -> Optional[dt.time]:
    """Parse '730', '7:30', '715am', '5pm', '19:05' -> datetime.time or None"""
    txt = (txt or "").strip().lower().replace(" ", "")
    if not txt:
        return None
    
    # Check for AM/PM
    ampm = None
    if txt.endswith("am"):
        ampm = "am"
        txt = txt[:-2]
    elif txt.endswith("pm"):
        ampm = "pm"
        txt = txt[:-2]
    
    # Remove colons
    txt = txt.replace(":", "")
    
    # Must be digits only at this point
    if not txt.isdigit():
        return None
    
    # Parse based on length
    if len(txt) == 1:
        # Single digit: treat as hour (e.g., "5" -> 5:00)
        hh, mm = int(txt), 0
    elif len(txt) == 2:
        # Two digits: could be "05" (5:00) or "30" (0:30)
        # Treat as hours if <= 23, otherwise invalid
        val = int(txt)
        if val <= 23:
            hh, mm = val, 0
        else:
            return None
    elif len(txt) == 3:
        # Three digits: "730" -> 7:30, "030" -> 0:30
        hh, mm = int(txt[0]), int(txt[1:])
    elif len(txt) == 4:
        # Four digits: "0730" -> 7:30, "2359" -> 23:59, "0000" -> 0:00
        hh, mm = int(txt[:2]), int(txt[2:])
    else:
        return None
    
    # Apply AM/PM conversion
    if ampm == "am":
        if hh == 12:
            hh = 0
    elif ampm == "pm":
        if hh < 12:
            hh += 12
    
    # Validate ranges
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    
    return dt.time(hh, mm)

def to_minutes(t: dt.time) -> int:
    return t.hour*60 + t.minute

def minutes_to_time(m: int) -> dt.time:
    m = max(0, min(1439, int(m)))
    return dt.time(m//60, m%60)

def minute_band(minute_of_day: int) -> str:
    # Return '1.00x', '1.10x', or '1.25x' based on weekday windows.
    # Night is 23:00-07:00.
    h = minute_of_day / 60.0
    if 7 <= h < 17: return "1.00x"
    if 17 <= h < 23: return "1.10x"
    return "1.25x"  # 23-07

def _minute_multiplier(minute_of_day: int, weekend_or_holiday: bool) -> float:
    # Weekend/holiday: 1.10 for 07-17, 1.25 otherwise.
    if weekend_or_holiday:
        h = minute_of_day / 60.0
        return 1.10 if 7 <= h < 17 else 1.25
    # Weekday multipliers
    m = minute_band(minute_of_day)
    return 1.00 if m == "1.00x" else (1.10 if m == "1.10x" else 1.25)

def _minute_rate_pts(category: str, m: int, wknd_hol: bool) -> float:
    mult = _minute_multiplier(m, wknd_hol)
    if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
        return AR_BASE * mult
    if category == "Restricted OB (In-house)":
        return 13.0 * mult
    if category == "Unrestricted Call":
        return 3.5  # flat
    return 0.0  # Cardiac is an adder at day level

def _split_across_midnights(start_dt: dt.datetime, end_dt: dt.datetime):
    """
    Yield (date, start_minute, end_minute) slices per calendar day, end-exclusive.
    For shifts that don't cross midnight, yields a single tuple.
    For shifts that cross midnight, yields multiple tuples (one per day).
    """
    cur = start_dt
    
    # If the shift doesn't cross midnight, just return the single interval
    if start_dt.date() == end_dt.date():
        yield (start_dt.date(), to_minutes(start_dt.time()), to_minutes(end_dt.time()))
        return
    
    # First day: from start time to midnight
    yield (cur.date(), to_minutes(cur.time()), 1440)
    
    # Move to next day
    cur = (cur + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Middle days: full 24-hour periods (if any)
    while cur.date() < end_dt.date():
        yield (cur.date(), 0, 1440)
        cur = (cur + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Last day: from midnight to end time
    if to_minutes(end_dt.time()) > 0:
        yield (end_dt.date(), 0, to_minutes(end_dt.time()))

def compute_day_time_points(date_obj: dt.date, df_entries: pd.DataFrame):
    # Dominance per minute for a single date.
    # Returns: total_time_points, per_category_minutes, assigned_min_applied, band_points
    if df_entries is None or df_entries.empty:
        return 0.0, {}, False, {"1.00x":0.0, "1.10x":0.0, "1.25x":0.0}

    wknd = date_obj.weekday() >= 5
    is_holiday = bool(df_entries.get("Holiday", pd.Series([False])).astype(bool).any())
    wknd_hol = wknd or is_holiday

    minutes_winner = [-1.0] * 1440
    winner_cat = [None] * 1440

    for _, r in df_entries.iterrows():
        cat = str(r.get("Category", ""))
        if cat == "Cardiac (Subspecialty) - Coverage":
            continue
        stime = r.get("Start"); etime = r.get("End")
        if not (isinstance(stime, dt.time) and isinstance(etime, dt.time)):
            continue
        smin = to_minutes(stime); emin = to_minutes(etime)
        if emin <= smin:
            continue
        for m in range(smin, emin):
            rate = _minute_rate_pts(cat, m, wknd_hol)
            if rate > minutes_winner[m]:
                minutes_winner[m] = rate
                winner_cat[m] = cat

    per_cat_minutes = {}
    band_points = {"1.00x":0.0, "1.10x":0.0, "1.25x":0.0}
    total_pts = 0.0
    assigned_pts = 0.0

    for m, cat in enumerate(winner_cat):
        if cat is None:
            continue
        per_cat_minutes[cat] = per_cat_minutes.get(cat, 0) + 1
        pts_min = _minute_rate_pts(cat, m, wknd_hol) / 60.0
        total_pts += pts_min
        if wknd_hol:
            band = "1.10x" if 7 <= m/60.0 < 17 else "1.25x"
        else:
            band = minute_band(m)
        if cat in ("Assigned (General AR)", "Activation from Unrestricted Call", "Restricted OB (In-house)", "Unrestricted Call"):
            band_points[band] += pts_min
        if cat == "Assigned (General AR)":
            assigned_pts += pts_min

    assigned_min_applied = False
    if per_cat_minutes.get("Assigned (General AR)", 0) > 0 and assigned_pts < 80.0:
        total_pts += (80.0 - assigned_pts)
        assigned_min_applied = True
        band_points["1.00x"] += max(0.0, 80.0 - assigned_pts)

    if (df_entries["Category"] == "Cardiac (Subspecialty) - Coverage").any():
        total_pts += 45.0  # daily adder

    for k in list(band_points.keys()):
        band_points[k] = round(float(band_points[k]), 2)

    return round(total_pts, 2), per_cat_minutes, assigned_min_applied, band_points

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

def load_entries(ws_entries) -> pd.DataFrame:
    """Load entries with caching to reduce API calls"""
    
    # Initialize session state for cache if not present
    if "sheet_data" not in st.session_state:
        st.session_state["sheet_data"] = None
        st.session_state["last_refresh"] = None
    
    # Show refresh button
    col1, col2 = st.columns([1, 3])
    if col1.button("🔄 Refresh Data"):
        try:
            with st.spinner("Loading from Google Sheets..."):
                st.session_state["sheet_data"] = ws_entries.get_all_records()
                st.session_state["last_refresh"] = dt.datetime.now()
            st.success("Data refreshed!")
        except Exception as e:
            st.error(f"Failed to refresh: {e}")
            if "quota" in str(e).lower():
                st.warning("⚠️ Google Sheets API quota exceeded. Please wait a minute and try again.")
    
    # Show last refresh time
    if st.session_state["last_refresh"]:
        col2.caption(f"Last refreshed: {st.session_state['last_refresh'].strftime('%I:%M:%S %p')}")
    
    # If no data loaded yet, show info message
    if st.session_state["sheet_data"] is None:
        st.info("👆 Press 'Refresh Data' to load your entries from Google Sheets.")
        return pd.DataFrame(columns=[
            "Date","Holiday","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"
        ])
    
    values = st.session_state["sheet_data"]
    if not values:
        return pd.DataFrame(columns=[
            "Date","Holiday","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"
        ])
    
    df = pd.DataFrame(values)
    
    # Parse dates
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    
    # Parse times
    for col in ["Start", "End"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: parse_time_any(str(x)) if pd.notna(x) else None)
    
    # Parse numeric columns
    for col in ["TEE Exams", "Productivity Points", "Extra Points"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    
    # Handle boolean
    if "Holiday" in df.columns:
        df["Holiday"] = df["Holiday"].astype(bool)
    
    # Handle text columns
    for col in ["Category", "Notes"]:
        if col in df.columns:
            df[col] = df[col].fillna("")
    
    return df

def ensure_user_sheet(gc):
    SPREADSHEET_NAME = "MWA Points Tracker"
    
    # Open or create spreadsheet
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except Exception:
        sh = gc.create(SPREADSHEET_NAME)

    def get_or_create(name: str, header: Optional[List[str]] = None, rows=4000, cols=20):
        """Get existing worksheet or create new one"""
        ws = None
        
        # Try to get existing worksheet
        try:
            ws = sh.worksheet(name)
        except Exception:
            pass  # Worksheet doesn't exist
        
        # If worksheet doesn't exist, create it
        if ws is None:
            try:
                ws = sh.add_worksheet(title=name, rows=rows, cols=cols)
            except Exception as e:
                # If it fails because sheet exists, try getting it again
                if "already exists" in str(e).lower():
                    try:
                        ws = sh.worksheet(name)
                    except Exception:
                        raise Exception(f"Could not get or create worksheet '{name}': {e}")
                else:
                    raise
        
        # Ensure headers are present (only if we have header to set)
        if ws and header:
            try:
                vals = ws.get_all_values()
                # If sheet is empty or first row is empty, add headers
                if not vals or not vals[0] or all(not cell for cell in vals[0]):
                    end_col = chr(64 + len(header))
                    ws.update(f"A1:{end_col}1", [header])
            except Exception:
                # If we can't read/write headers, continue anyway
                pass
        
        return ws

    ws_entries = get_or_create("Entries", header=[
        "Date","Holiday","Category","Start","End",
        "TEE Exams","Productivity Points","Extra Points","Notes"
    ])
    ws_daily = get_or_create("Daily Totals", header=[
        "Date","Holiday","Time Points","Productivity Points","Extra Points","TEE Points","Total Points"
    ], rows=2000, cols=10)
    ws_msum = get_or_create("Monthly Summary", header=["Month","Total Points"], rows=300, cols=3)
    
    return sh, ws_entries, ws_daily, ws_msum

def save_entries(ws_entries, df: pd.DataFrame):
    header = ["Date","Holiday","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes"]
    ws_entries.clear()
    ws_entries.update("A1:I1", [header])
    if df.empty:
        return
    out = []
    for _, r in df.iterrows():
        def fmt(t): return t.strftime("%H:%M") if isinstance(t, dt.time) else ""
        out.append([
            r["Date"].strftime("%Y-%m-%d") if isinstance(r["Date"], dt.date) else "",
            bool(r.get("Holiday", False)),
            r.get("Category",""),
            fmt(r.get("Start")),
            fmt(r.get("End")),
            int(r.get("TEE Exams",0) or 0),
            float(r.get("Productivity Points",0) or 0.0),
            float(r.get("Extra Points",0) or 0.0),
            r.get("Notes","")
        ])
    ws_entries.update(f"A2:I{len(out)+1}", out)

def write_daily_totals(sh, df_entries: pd.DataFrame):
    ws = sh.worksheet("Daily Totals")
    ws.clear()
    ws.update("A1:G1", [[
        "Date","Holiday","Time Points","Productivity Points","Extra Points","TEE Points","Total Points"
    ]])
    if df_entries.empty:
        return
    dfe = df_entries.copy()
    dfe["Date"] = pd.to_datetime(dfe["Date"]).dt.date
    for col in ["TEE Exams","Productivity Points","Extra Points"]:
        if col in dfe:
            dfe[col] = pd.to_numeric(dfe[col], errors="coerce").fillna(0.0)
        else:
            dfe[col] = 0.0
    out_rows = []
    for d, chunk in dfe.groupby("Date"):
        time_pts, _, _, _ = compute_day_time_points(d, chunk)
        tee_pts = float(chunk["TEE Exams"].sum()) * 22.0
        prod_pts = float(chunk["Productivity Points"].sum())
        extra_pts = float(chunk["Extra Points"].sum())
        total = time_pts + tee_pts + prod_pts + extra_pts
        holiday_flag = bool(chunk.get("Holiday", pd.Series([False])).astype(bool).any())
        out_rows.append([d.strftime("%Y-%m-%d"), holiday_flag, round(time_pts,2), round(prod_pts,2), round(extra_pts,2), round(tee_pts,2), round(total,2)])
    if out_rows:
        ws.update(f"A2:G{len(out_rows)+1}", out_rows)

def month_tab_name(d: dt.date) -> str:
    return d.strftime("%b %Y")

def ensure_month_sheet(sh, name: str):
    try:
        ws = sh.worksheet(name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=1000, cols=12)
        ws.update("A1:J1", [[
            "Date","Holiday","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes","Entry Total Points"
        ]])
    return ws

def _entry_time_points_basic(row, date_is_weekend_or_holiday: bool) -> float:
    s = row["Start"]; e = row["End"]
    if not (isinstance(s, dt.time) and isinstance(e, dt.time)):
        return 0.0
    smin = to_minutes(s); emin = to_minutes(e)
    if emin <= smin:
        return 0.0
    pts = 0.0
    for m in range(smin, emin):
        pts += _minute_rate_pts(row["Category"], m, date_is_weekend_or_holiday) / 60.0
    return round(pts, 2)

def write_month_sheets(sh, df_entries: pd.DataFrame):
    if df_entries.empty:
        return
    dfe = df_entries.copy()
    dfe["Entry Base Time Points"] = dfe.apply(
        lambda r: _entry_time_points_basic(r, (r["Holiday"] or (r["Date"].weekday()>=5))), axis=1
    )
    dfe["Prod"] = pd.to_numeric(dfe["Productivity Points"], errors="coerce").fillna(0.0)
    dfe["Extra"] = pd.to_numeric(dfe["Extra Points"], errors="coerce").fillna(0.0)
    dfe["Entry Total Points"] = dfe["Entry Base Time Points"] + dfe["Prod"] + dfe["Extra"]
    dfe["MonthName"] = dfe["Date"].apply(month_tab_name)

    for mname, chunk in dfe.groupby("MonthName"):
        ws = ensure_month_sheet(sh, mname)
        ws.clear()
        ws.update("A1:J1", [[
            "Date","Holiday","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes","Entry Total Points"
        ]])
        rows = []
        for _, r in chunk.sort_values("Date").iterrows():
            rows.append([
                r["Date"].strftime("%Y-%m-%d"),
                bool(r["Holiday"]),
                r["Category"],
                fmt_hhmm(r["Start"]), fmt_hhmm(r["End"]),
                int(r["TEE Exams"] or 0),
                round(float(r["Productivity Points"] or 0),2),
                round(float(r["Extra Points"] or 0),2),
                r.get("Notes",""),
                round(float(r["Entry Total Points"]),2)
            ])
        start_row = 2
        if rows:
            ws.update(f"A{start_row}:J{start_row+len(rows)-1}", rows)
        total_points = round(float(chunk["Entry Total Points"].sum()),2)
        total_row = start_row + len(rows)
        ws.update(f"A{total_row}:J{total_row}", [["","MONTH TOTAL","","","","","","","", total_points]])
        fmt = CellFormat(backgroundColor=Color(red=1.0, green=1.0, blue=0.8), textFormat=TextFormat(bold=True))
        format_cell_range(ws, f"A{total_row}:J{total_row}", fmt)

def write_monthly_summary(sh, df_entries: pd.DataFrame):
    ws = sh.worksheet("Monthly Summary")
    ws.clear()
    ws.update("A1:B1", [["Month","Total Points"]])
    if df_entries.empty:
        return
    per_day = []
    for d, chunk in df_entries.groupby("Date"):
        tpts, _, _, _ = compute_day_time_points(d, chunk)
        tee = float(chunk["TEE Exams"].sum()) * 22.0
        prod = float(chunk["Productivity Points"].sum())
        extra = float(chunk["Extra Points"].sum())
        per_day.append({"Date": d, "Total": tpts + tee + prod + extra})
    dfd = pd.DataFrame(per_day)
    if dfd.empty:
        return
    dfd["MonthStart"] = dfd["Date"].apply(lambda d: dt.date(d.year, d.month, 1))
    per_month = dfd.groupby("MonthStart", as_index=False)["Total"].sum().sort_values("MonthStart")
    rows = []
    for _, r in per_month.iterrows():
        rows.append([r["MonthStart"].strftime("%b %Y"), round(float(r["Total"]),2)])
    if rows:
        ws.update(f"A2:B{len(rows)+1}", rows)
    grand_total = round(float(per_month["Total"].sum()),2) if not per_month.empty else 0.0
    total_row = len(rows)+2
    ws.update(f"A{total_row}:B{total_row}", [["Grand Total", grand_total]])
    fmt = CellFormat(backgroundColor=Color(red=1.0, green=1.0, blue=0.8), textFormat=TextFormat(bold=True))
    format_cell_range(ws, f"A{total_row}:B{total_row}", fmt)

# ---------------- App ----------------
st.title("MWA Points Tracker — Live Preview")

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

try:
    gc = gspread.authorize(st.session_state["creds"])
    sh, ws_entries, ws_daily, ws_msum = ensure_user_sheet(gc)
except Exception as e:
    st.error(f"Google Sheets/Drive error: {e}")
    st.stop()

entries = load_entries(ws_entries)

tab_entries, tab_summary = st.tabs(["Entries","Summary"])

with tab_entries:
    st.subheader("Add Time Intervals")

    cc_head = st.columns([1,1.2,1.2,1])
    tee = cc_head[0].number_input("TEE Exams (22 pts each)", min_value=0, step=1, value=0, key="tee_add")
    prod = cc_head[1].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0, key="prod_add")
    extra = cc_head[2].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0, key="extra_add")
    notes = cc_head[3].text_input("Notes", "", key="notes_add")

    if "intervals_v5" not in st.session_state:
        st.session_state.intervals_v5 = [ {
            "category": CATEGORIES[0],
            "start_date": dt.date.today(),
            "start_time": "",
            "end_date": dt.date.today(),
            "end_time": "",
        } ]
    if "interval_ids_v5" not in st.session_state:
        st.session_state.interval_ids_v5 = [f"int_{int(time.time()*1000)}"]

    if st.button("➕ Add interval"):
        st.session_state.intervals_v5.append({
            "category": CATEGORIES[0],
            "start_date": dt.date.today(),
            "start_time": "",
            "end_date": dt.date.today(),
            "end_time": "",
        })
        st.session_state.interval_ids_v5.append(f"int_{int(time.time()*1000)}")

    new_intervals = []
    new_ids = []
    for idx, row in enumerate(st.session_state.intervals_v5):
        iid = st.session_state.interval_ids_v5[idx]
        st.markdown(f"**Interval {idx+1}**")
        c = st.columns([1.6,1,1,1,1,0.25])
        cat = c[0].selectbox("Category", CATEGORIES, index=CATEGORIES.index(row["category"]) if row["category"] in CATEGORIES else 0, key=f"cat_{iid}")
        sdate = c[1].date_input("Start Date", value=row.get("start_date", dt.date.today()), format="MM/DD/YYYY", key=f"sdate_{iid}")
        edate = c[3].date_input("End Date", value=row.get("end_date", sdate), format="MM/DD/YYYY", key=f"edate_{iid}")
        key_st = f"stime_{iid}"; key_et = f"etime_{iid}"
        if key_st not in st.session_state: st.session_state[key_st] = row.get("start_time","")
        if key_et not in st.session_state: st.session_state[key_et] = row.get("end_time","")
        start_str = c[2].text_input("Start Time (e.g. 730, 7:30, 5pm)", value=row.get("start_time",""), key=key_st)
        end_str = c[4].text_input("End Time (e.g. 1700, 5pm)", value=row.get("end_time",""), key=key_et)
        if c[5].button("🗑️", key=f"del_{iid}"): 
            continue
        new_intervals.append({
            "category": cat,
            "start_date": sdate,
            "end_date": edate,
            "start_time": start_str,
            "end_time": end_str,
        })
        new_ids.append(iid)
    st.session_state.intervals_v5 = new_intervals
    st.session_state.interval_ids_v5 = new_ids

    for row in st.session_state.intervals_v5:
        stime = parse_time_any(row["start_time"])
        etime = parse_time_any(row["end_time"])
        sdate = row["start_date"]; edate = row["end_date"]
        if not (stime and etime and isinstance(sdate, dt.date) and isinstance(edate, dt.date)):
            continue
        start_dt = dt.datetime.combine(sdate, stime)
        end_dt = dt.datetime.combine(edate, etime)
        if edate == sdate and end_dt <= start_dt:
            end_dt = end_dt + dt.timedelta(days=1)
        if end_dt - start_dt > dt.timedelta(hours=24):
            errors.append(f"Interval starting {sdate} {stime.strftime('%H:%M')} exceeds 24 hours — skipped.")
            continue
        for d, smin, emin in _split_across_midnights(start_dt, end_dt):
            affected_dates.add(d)
            if emin <= smin: continue
            preview_rows.append({
                "Date": d,
                "Holiday": False,
                "Category": row["category"],
                "Start": minutes_to_time(smin),
                "End": minutes_to_time(emin if emin < 1440 else 1439),
                "TEE Exams": 0, "Productivity Points": 0.0, "Extra Points": 0.0,
                "Notes": notes
            })

    preview_df = pd.DataFrame(preview_rows)
    if not preview_df.empty:
        preview_df = preview_df.sort_values(["Date","Start"]).reset_index(drop=True)

    sorted_dates = sorted(affected_dates)
    holiday_map = {}
    adders_day_index = 0

    st.markdown("### Preview")
    if errors:
        for e in errors:
            st.warning(e)

    if len(sorted_dates) == 0:
        st.caption("Add at least one valid interval to see preview.")
    else:
        if len(sorted_dates) == 1:
            holiday_day1 = st.checkbox(f"Holiday for {sorted_dates[0].strftime('%m/%d/%Y')}", value=False, key="holiday_d1")
            holiday_map[sorted_dates[0]] = holiday_day1
        else:
            cols = st.columns([1,1,1])
            holiday_day1 = cols[0].checkbox(f"Holiday Day 1 ({sorted_dates[0].strftime('%m/%d/%Y')})", value=False, key="holiday_d1")
            holiday_day2 = cols[1].checkbox(f"Holiday Day 2 ({sorted_dates[1].strftime('%m/%d/%Y')})", value=False, key="holiday_d2")
            adders_to_second = cols[2].checkbox("Apply one-time adders to Day 2", value=False, key="adders_day2")
            holiday_map[sorted_dates[0]] = holiday_day1
            holiday_map[sorted_dates[1]] = holiday_day2
            adders_day_index = 1 if adders_to_second else 0

        if not preview_df.empty:
            preview_df["Holiday"] = preview_df["Date"].map(lambda d: holiday_map.get(d, False))

        if not preview_df.empty:
            show = preview_df.copy()
            show["Start"] = show["Start"].apply(fmt_hhmm)
            show["End"] = show["End"].apply(fmt_hhmm)
            st.dataframe(show, use_container_width=True, hide_index=True)

            per_date_info = []
            for d in sorted_dates:
                chunk = preview_df[preview_df["Date"] == d]
                tpts, _, assigned_min, band_pts = compute_day_time_points(d, chunk)
                per_date_info.append((d, tpts, assigned_min, band_pts))

            for d, tpts, assigned_min, band_pts in per_date_info:
                if assigned_min:
                    st.info(f"{d.strftime('%m/%d/%Y')}: Assigned minimum 80 pts applied.")
                st.markdown(f"**New time points (dominance) for {d.strftime('%m/%d/%Y')}: {tpts:.2f}**")
                st.caption(f"Per-band (time-based only): 1.00x={band_pts['1.00x']:.2f} | 1.10x={band_pts['1.10x']:.2f} | 1.25x={band_pts['1.25x']:.2f}")

            if float(prod or 0.0) != 0.0 or float(extra or 0.0) != 0.0 or int(tee or 0) > 0:
                target_date = sorted_dates[min(adders_day_index, len(sorted_dates)-1)]
                adders_total = float(prod or 0.0) + float(extra or 0.0) + float(int(tee or 0)*22.0)
                st.markdown(f"**One-time adders will be applied to {target_date.strftime('%m/%d/%Y')}: {adders_total:.2f} pts**")

            if not entries.empty:
                existing_by_date = {}
                for d in sorted_dates:
                    same_day = entries[pd.to_datetime(entries["Date"]).dt.date == d]
                    if not same_day.empty:
                        cur_tpts, _, _, _ = compute_day_time_points(d, same_day)
                        cur_tee = float(same_day.get("TEE Exams", 0).sum()) * 22.0
                        cur_prod = float(same_day.get("Productivity Points", 0).sum())
                        cur_extra = float(same_day.get("Extra Points", 0).sum())
                        existing_by_date[d] = cur_tpts + cur_tee + cur_prod + cur_extra
                    else:
                        existing_by_date[d] = 0.0
            else:
                existing_by_date = {d:0.0 for d in sorted_dates}

            st.markdown("#### Projected totals by date (including currently saved entries)")
            for idx, (d, tpts, _, _) in enumerate(per_date_info):
                add_one_time = 0.0
                if idx == adders_day_index:
                    add_one_time = float(prod or 0.0) + float(extra or 0.0) + float(int(tee or 0)*22.0)
                projected = existing_by_date.get(d,0.0) + tpts + add_one_time
                st.markdown(f"- **{d.strftime('%m/%d/%Y')}** -> {projected:.2f} points")

    if st.button("Add to Sheet"):
        if not preview_df.empty:
            preview_df.loc[:, "TEE Exams"] = 0
            preview_df.loc[:, "Productivity Points"] = 0.0
            preview_df.loc[:, "Extra Points"] = 0.0
            if len(sorted_dates) > 0:
                chosen_date = sorted_dates[min(adders_day_index, len(sorted_dates)-1)]
                idxs = preview_df.index[preview_df["Date"] == chosen_date].tolist()
                if idxs:
                    target_idx = idxs[0]
                    preview_df.loc[target_idx, "TEE Exams"] = int(tee or 0)
                    preview_df.loc[target_idx, "Productivity Points"] = float(prod or 0.0)
                    preview_df.loc[target_idx, "Extra Points"] = float(extra or 0.0)

            entries_out = pd.concat([entries, preview_df], ignore_index=True)
            save_entries(ws_entries, entries_out)
            write_daily_totals(sh, entries_out)
            write_month_sheets(sh, entries_out)
            write_monthly_summary(sh, entries_out)
            st.success("Saved intervals and updated Daily Totals, Month sheets, and Monthly Summary.")
            st.rerun()
        else:
            st.warning("Enter at least one valid interval before adding.")

with tab_summary:
    st.subheader("Daily & Monthly Summary")

    col = st.columns([1,1.2,1.2])
    dsel = col[0].date_input("Pick a date", value=dt.date.today(), format="MM/DD/YYYY", key="summary_date")
    if not entries.empty:
        day_df = entries[pd.to_datetime(entries["Date"]).dt.date == dsel]
    else:
        day_df = pd.DataFrame(columns=entries.columns if not entries.empty else ["Date","Category","Start","End","TEE Exams","Productivity Points","Extra Points","Notes","Holiday"])

    if not day_df.empty:
        tpts, per_cat_min, assigned_min, band_pts = compute_day_time_points(dsel, day_df)
        tee_pts = float(day_df.get("TEE Exams", 0).sum()) * 22.0
        prod_pts = float(day_df.get("Productivity Points", 0).sum())
        extra_pts = float(day_df.get("Extra Points", 0).sum())
        total = tpts + tee_pts + prod_pts + extra_pts
        st.markdown(f"**Time Points (dominance): {tpts:.2f}**")
        st.caption(f"Per-band (time-based only): 1.00x={band_pts['1.00x']:.2f} | 1.10x={band_pts['1.10x']:.2f} | 1.25x={band_pts['1.25x']:.2f}")
        st.markdown(f"**TEE:** {tee_pts:.2f} | **Productivity:** {prod_pts:.2f} | **Extra:** {extra_pts:.2f}")
        st.markdown(f"**Daily Total:** {total:.2f}")
    else:
        st.info("No entries for the selected date yet.")

    if not entries.empty:
        per_day = []
        for d, chunk in entries.groupby("Date"):
            tpts, _, _, _ = compute_day_time_points(d, chunk)
            tee = float(chunk["TEE Exams"].sum()) * 22.0
            prod = float(chunk["Productivity Points"].sum())
            extra = float(chunk["Extra Points"].sum())
            per_day.append({"Date": d, "Total": tpts + tee + prod + extra})
        dfd = pd.DataFrame(per_day)
        dfd["Month"] = dfd["Date"].apply(lambda d: d.strftime("%b %Y"))
        per_month = dfd.groupby("Month", as_index=False)["Total"].sum().sort_values("Month")
        st.subheader("Monthly Summary")
        st.dataframe(per_month, use_container_width=True, hide_index=True)
    else:
        st.info("No monthly data to summarize yet.")
