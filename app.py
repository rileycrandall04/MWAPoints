### ERROR BANNER GUARD START ###
try:
    
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
        """
        Returns (lines, time_pts) using Aug 2025 policy:
          - Assigned (General AR): base 20 pts/hr, +10% eve (17–23), +25% night (23–07); min 80 pts per entry
          - Activation from Unrestricted Call: same rate model as Assigned but NO 80-pt minimum
          - Restricted OB (In-house): base 13 pts/hr, +10% eve, +25% night
          - Unrestricted Call: flat 3.5 pts/hr (no multipliers)
          - Cardiac/Liver coverage: flat 45 pts per entry (daily coverage)
        """
        ttl, d, e, n = split_bands(start_t, end_t)
        lines=[]; time_pts=0.0
    
        if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
            base=20.0
            d_pts=d*base*1.00; e_pts=e*base*1.10; n_pts=n*base*1.25
            subtotal=d_pts+e_pts+n_pts
            # Apply 4-hour minimum (80 pts) ONLY for Assigned
            time_pts = max(subtotal, 80.0) if category=="Assigned (General AR)" else subtotal
            if d>0: lines.append(f"Day: {d:.2f} h @20.00 = {d_pts:.2f} pts")
            if e>0: lines.append(f"Evening: {e:.2f} h @22.00 (1.10×) = {e_pts:.2f} pts")
            if n>0: lines.append(f"Night: {n:.2f} h @25.00 (1.25×) = {n_pts:.2f} pts")
            if category=="Assigned (General AR)" and time_pts==80.0 and subtotal<80.0:
                lines.append("⚠️ Minimum 4-hour rule applied (80 pts).")
    
        elif category=="Restricted OB (In-house)":
            base=13.0
            d_pts=d*base*1.00; e_pts=e*base*1.10; n_pts=n*base*1.25
            time_pts=d_pts+e_pts+n_pts
            if d>0: lines.append(f"Day: {d:.2f} h @13.00 = {d_pts:.2f} pts")
            if e>0: lines.append(f"Evening: {e:.2f} h @14.30 (1.10×) = {e_pts:.2f} pts")
            if n>0: lines.append(f"Night: {n:.2f} h @16.25 (1.25×) = {n_pts:.2f} pts")
    
        elif category=="Unrestricted Call":
            time_pts = ttl*3.5
            if ttl>0: lines.append(f"Total: {ttl:.2f} h @3.50 pts/hr = {time_pts:.2f} pts")
    
        elif category=="Cardiac (Subspecialty) – Coverage":
            time_pts=45.0
            lines.append("Cardiac/Liver coverage: 45.00 pts")
    
        return lines, time_pts
    
    
    def compute_entry_time_points(row):
        cat = row["Category"]
        s = row["Start"]; e = row["End"]
        lines, tp = band_breakdown(cat, s, e)
        tp += 22.0 * float(row.get("TEE Exams", 0) or 0)
        return tp
    
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
    
    
    
    
    
    
    
    def _minute_rate_pts(category: str, minute_of_day: int) -> float:
        # minute_of_day: 0..1439
        hour = minute_of_day / 60.0
        # Bands: Day 07-17, Eve 17-23, Night 23-07 (next day)
        if 7 <= hour < 17:
            mult = 1.00
        elif 17 <= hour < 23:
            mult = 1.10
        else:
            mult = 1.25
        if category in ("Assigned (General AR)","Activation from Unrestricted Call"):
            return 20.0 * mult
        if category == "Restricted OB (In-house)":
            return 13.0 * mult
        if category == "Unrestricted Call":
            return 3.5
        # Cardiac coverage is not time-based hourly; handled separately at day level
        return 0.0
    
    def _split_interval_to_day(start_t, end_t):
        # Returns list of (start_min, end_min) within 0..1440 for the same day
        if not (isinstance(start_t, dt.time) and isinstance(end_t, dt.time)):
            return []
        s = start_t.hour*60 + start_t.minute
        e = end_t.hour*60 + end_t.minute
        if e == s:
            return []
        if e > s:
            return [(s, e)]
        else:
            # overnight: take until midnight only for this day (00:00 next day saved in next entry)
            return [(s, 1440)]
    
    def compute_day_time_points(date_obj: dt.date, df_entries: pd.DataFrame):
        """
        For a single date, compute time points by choosing the highest compensating category
        for each minute of the day. Also applies the Assigned 80-pt minimum if any Assigned
        minutes are credited. Adds 45 pts once if Cardiac coverage is present that day.
        Returns: (time_points_total, per_category_minutes, assigned_min_applied: bool)
        """
        minutes_winner = [-1.0] * 1440  # store winning rate per minute
        winner_cat = [None] * 1440
    
        # Track whether any Assigned or Cardiac entries exist
        has_cardiac = False
    
        # Process intervals
        for _, r in df_entries.iterrows():
            cat = str(r.get("Category",""))
            if cat == "Cardiac (Subspecialty) – Coverage":
                has_cardiac = True
                continue
            stime = r.get("Start"); etime = r.get("End")
            for (smin, emin) in _split_interval_to_day(stime, etime):
                for m in range(smin, emin):
                    rate = _minute_rate_pts(cat, m)
                    if rate > minutes_winner[m]:
                        minutes_winner[m] = rate
                        winner_cat[m] = cat
    
        # Accumulate points per category
        per_cat_minutes = {}
        for m, cat in enumerate(winner_cat):
            if cat is None: 
                continue
            per_cat_minutes[cat] = per_cat_minutes.get(cat, 0) + 1
    
        # Convert minutes to points
        total_pts = 0.0
        assigned_pts = 0.0
        assigned_minutes = per_cat_minutes.get("Assigned (General AR)", 0)
        for cat, mins in per_cat_minutes.items():
            # sum minute-wise rate/60
            pts = 0.0
            for m in range(1440):
                if winner_cat[m] == cat:
                    pts += _minute_rate_pts(cat, m) / 60.0
            total_pts += pts
            if cat == "Assigned (General AR)":
                assigned_pts = pts
    
        assigned_min_applied = False
        if assigned_minutes > 0 and assigned_pts < 80.0:
            total_pts += (80.0 - assigned_pts)
            assigned_min_applied = True
    
        if has_cardiac:
            total_pts += 45.0
    
        return total_pts, per_cat_minutes, assigned_min_applied
    
    
    
    
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
        ws = sh.worksheet("Daily Totals")
        ws.clear()
        ws.update("A1:G1", [["Date","Time Points","Productivity Points","Extra Points","TEE Points","Total Points","Running Monthly Total"]])
        if df_entries.empty:
            return
    
        dfe = df_entries.copy()
        # Normalize columns
        dfe["Date"] = pd.to_datetime(dfe["Date"]).dt.date
        for col in ["TEE Exams","Productivity Points","Extra Points"]:
            if col in dfe:
                dfe[col] = pd.to_numeric(dfe[col], errors="coerce").fillna(0.0)
            else:
                dfe[col] = 0.0
    
        out_rows = []
        # Group per date and compute dominance-based time points
        for d, chunk in dfe.groupby("Date"):
            time_pts, _, _ = compute_day_time_points(d, chunk)
            tee_pts = float(chunk["TEE Exams"].sum()) * 22.0
            prod_pts = float(chunk["Productivity Points"].sum())
            extra_pts = float(chunk["Extra Points"].sum())
            total = time_pts + tee_pts + prod_pts + extra_pts
            out_rows.append([d, time_pts, prod_pts, extra_pts, tee_pts, total])
    
        daily = pd.DataFrame(out_rows, columns=["Date","Time Points","Productivity Points","Extra Points","TEE Points","Total Points"])
        daily = daily.sort_values("Date")
        daily["YearMonth"] = daily["Date"].apply(lambda d: dt.date(d.year, d.month, 1))
        daily["Running Monthly Total"] = daily.groupby("YearMonth")["Total Points"].cumsum()
        daily["DateStr"] = daily["Date"].apply(lambda d: d.strftime("%m/%d/%Y"))
    
        # Write to sheet
        out = []
        for _, r in daily.iterrows():
            out.append([
                r["DateStr"],
                round(float(r["Time Points"]),2),
                round(float(r["Productivity Points"]),2),
                round(float(r["Extra Points"]),2),
                round(float(r["TEE Points"]),2),
                round(float(r["Total Points"]),2),
                round(float(r["Running Monthly Total"]),2),
            ])
        if out:
            ws.update(f"A2:G{len(out)+1}", out)
    
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
    
    date = st.date_input("Date", value=dt.date.today(), format="MM/DD/YYYY")
    
        date = st.date_input("Date", dt.date.today(), format="MM/DD/YYYY")
    
    # --- Multi-interval entry ---
    if "intervals" not in st.session_state:
        st.session_state.intervals = [ {"category": CATEGORIES[0], "start": "730", "end": "1700"} ]
    
    if st.button("➕ Add interval"):
        st.session_state.intervals.append({"category": CATEGORIES[0], "start": "", "end": ""})
    
    # Render interval rows
    new_intervals = []
    for i, row in enumerate(st.session_state.intervals):
        st.markdown(f"**Interval {i+1}**")
        cc = st.columns([1.5,1,1,0.3])
        cat = cc[0].selectbox("Category", CATEGORIES, index=CATEGORIES.index(row["category"]) if row["category"] in CATEGORIES else 0, key=f"cat_{i}")
        stext = cc[1].text_input("Start (e.g. 730, 7:30, 5pm)", row.get("start",""), key=f"st_{i}")
        etext = cc[2].text_input("End (e.g. 1700, 5:00pm)", row.get("end",""), key=f"et_{i}")
        if cc[3].button("🗑️", key=f"del_{i}"):
            continue
        new_intervals.append({"category": cat, "start": stext, "end": etext})
    st.session_state.intervals = new_intervals
    
    # Parse helper supporting am/pm and colon optional
    def _parse_time_any(txt):
        txt = (txt or "").strip().lower().replace(" ", "")
        ampm = None
        if txt.endswith("am"):
            ampm = "am"; txt = txt[:-2]
        elif txt.endswith("pm"):
            ampm = "pm"; txt = txt[:-2]
        txt = txt.replace(":", "")
        if not txt.isdigit() or len(txt) < 2 or len(txt) > 4:
            return None
        if len(txt) <= 2:
            hh, mm = int(txt), 0
        else:
            hh, mm = int(txt[:-2]), int(txt[-2:])
        if ampm == "am":
            if hh == 12: hh = 0
        elif ampm == "pm":
            if hh < 12: hh += 12
        if hh > 23 or mm > 59:
            return None
        return dt.time(hh, mm)
    
    # Additional per-entry fields
    cc2 = st.columns([1,1,1,3])
    tee = cc2[0].number_input("TEE Exams", min_value=0, step=1, value=0)
    prod = cc2[1].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0)
    extra = cc2[2].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0)
    notes = cc2[3].text_input("Notes","")
    
    # Convert intervals to DataFrame of this date
    preview_list = []
    for row in st.session_state.intervals:
        stime = _parse_time_any(row["start"])
        etime = _parse_time_any(row["end"])
        if stime and etime:
            preview_list.append({"Date": date, "Category": row["category"], "Start": stime, "End": etime, "TEE Exams": 0, "Productivity Points": 0.0, "Extra Points": 0.0, "Notes": ""})
    
    preview_df = pd.DataFrame(preview_list)
    
    # Compute dominance-based time points for this date (from preview intervals only)
    tpts = 0.0; assigned_min = False
    if not preview_df.empty:
        tpts, per_cat_min, assigned_min = compute_day_time_points(date, preview_df)
    
    # Live Preview
    st.markdown("### Preview")
    if not preview_df.empty:
        st.write(preview_df.assign(Start=preview_df["Start"].apply(lambda t: t.strftime("%H:%M")),
                                   End=preview_df["End"].apply(lambda t: t.strftime("%H:%M"))))
        if assigned_min:
            st.info("Assigned minimum 80 pts applied in dominance computation.")
        st.markdown(f"**New time points (dominance applied):** {tpts:.2f}")
        st.markdown(f"**New production points:** {prod:.2f}")
        if float(extra or 0.0) != 0.0:
            st.markdown(f"**New extra points:** {float(extra):.2f}")
    else:
        st.caption("Add at least one valid interval to see preview.")
    
    # Include existing same-day totals for projected total
    current_daily_total = 0.0
    if not entries.empty:
        same_day = entries[pd.to_datetime(entries["Date"]).dt.date == date]
        # Recompute with dominance for same_day
        if not same_day.empty:
            cur_tpts, _, _ = compute_day_time_points(date, same_day)
            cur_tee = float(same_day.get("TEE Exams", 0).sum()) * 22.0
            cur_prod = float(same_day.get("Productivity Points", 0).sum())
            cur_extra = float(same_day.get("Extra Points", 0).sum())
            current_daily_total = cur_tpts + cur_tee + cur_prod + cur_extra
    
    projected_total = current_daily_total + tpts + prod + float(extra or 0.0) + float(tee*22.0)
    st.markdown(f"**Projected total for {date.strftime('%m/%d/%Y')}: {projected_total:.2f} points**")
    
    # Add button: persist raw intervals (to allow recomputation later) as separate rows
    if st.button("Add"):
        if not preview_df.empty:
            # Attach user-entered TEE/Prod/Extra only to the first row to avoid double counting
            preview_df.loc[:, "TEE Exams"] = 0
            preview_df.loc[:, "Productivity Points"] = 0.0
            preview_df.loc[:, "Extra Points"] = 0.0
            preview_df.loc[preview_df.index[0], "TEE Exams"] = tee
            preview_df.loc[preview_df.index[0], "Productivity Points"] = prod
            preview_df.loc[preview_df.index[0], "Extra Points"] = extra
            
# ---- Compensation Band Breakdown (time-based only) ----
st.markdown("### Compensation Band Breakdown (time rates only)")

band_minutes = {1.00: 0, 1.10: 0, 1.25: 0}
band_points = {1.00: 0.0, 1.10: 0.0, 1.25: 0.0}
band_rates = {1.00: "Base (20 pts/hr AR, 13 pts/hr OB, 3.5 pts/hr Call)",
              1.10: "+10% Differential (Evening/Weekend Daytime)",
              1.25: "+25% Differential (Night/Weekend/Holiday)"}

for d in sorted_dates:
    chunk = preview_df[preview_df["Date"] == d]
    wknd = d.weekday() >= 5
    holiday_flag = holiday_map.get(d, False)
    wknd_hol = wknd or holiday_flag

    minutes_winner = [-1.0] * 1440
    for _, r in chunk.iterrows():
        cat = r["Category"]
        if cat == "Cardiac (Subspecialty) – Coverage":
            continue
        smin = to_minutes(r["Start"])
        emin = to_minutes(r["End"])
        for m in range(smin, emin):
            rate = _minute_rate_pts(cat, m, wknd_hol)
            if rate > minutes_winner[m]:
                minutes_winner[m] = rate

    for m, rate in enumerate(minutes_winner):
        if rate > 0:
            mult = _minute_multiplier(m, wknd_hol)
            if mult in band_minutes:
                band_minutes[mult] += 1
                band_points[mult] += rate / 60.0

rows = []
total_hours = 0
total_pts = 0
for mult in [1.00, 1.10, 1.25]:
    mins = band_minutes[mult]
    pts = band_points[mult]
    hrs = mins / 60.0
    total_hours += hrs
    total_pts += pts
    rows.append({
        "Band": f"{mult:.2f}×",
        "Compensation Rate Used": band_rates[mult],
        "Hours": round(hrs,2),
        "Points": round(pts,2)
    })

rows.append({
    "Band": "Total",
    "Compensation Rate Used": "",
    "Hours": round(total_hours,2),
    "Points": round(total_pts,2)
})

band_df = pd.DataFrame(rows)
st.dataframe(band_df, use_container_width=True, hide_index=True)


# Save
            entries = pd.concat([entries, preview_df], ignore_index=True)
            save_entries(ws_entries, entries)
            write_daily_totals(sh, entries)
            write_month_sheets(sh, entries)
            write_monthly_summary(sh, entries)
            st.success("Saved intervals and updated Daily Totals, Monthly sheets, and Monthly Summary.")
            st.rerun()
    
    
    # --- Live widgets (no form; instant preview) ---
    c = st.columns([1,1.5,1,1,1,1,1,2])
    date = c[0].date_input("Date", value=dt.date.today(), format="MM/DD/YYYY")
    category = c[1].selectbox("Category", CATEGORIES, index=0)
    
    # Free-text time inputs with colon-optional parsing
    def _parse_time_any(txt, label):
        txt = (txt or "").strip().lower().replace(" ", "")
        # Optional am/pm handling
        ampm = None
        if txt.endswith("am"):
            ampm = "am"; txt = txt[:-2]
        elif txt.endswith("pm"):
            ampm = "pm"; txt = txt[:-2]
        txt = txt.replace(":", "")
        if not txt.isdigit() or len(txt) < 2 or len(txt) > 4:
            st.warning(f"Invalid {label} time")
            return None
        if len(txt) <= 2:
            hh, mm = int(txt), 0
        else:
            hh, mm = int(txt[:-2]), int(txt[-2:])
        if ampm == "am":
            if hh == 12: hh = 0
        elif ampm == "pm":
            if hh < 12: hh += 12
        if hh > 23 or mm > 59:
            st.warning(f"Invalid {label} time")
            return None
        return dt.time(hh, mm)
    
    start_text = c[2].text_input("Start time (e.g. 730, 7:30, 715am, 19:05)", "730")
    end_text   = c[3].text_input("End time (e.g. 1700, 5:00pm, 1745)", "1700")
    start_t = _parse_time_any(start_text, "start")
    end_t   = _parse_time_any(end_text, "end")
    
    tee   = c[4].number_input("TEE Exams", min_value=0, step=1, value=0)
    prod  = c[5].number_input("Productivity Points", min_value=0.0, step=1.0, value=0.0)
    extra = c[6].number_input("Extra Points", min_value=0.0, step=1.0, value=0.0)
    notes = c[7].text_input("Notes","")
    
    st.caption("Preview updates live. Times can be typed with or without a colon (AM/PM supported).")
    
    # --- Build preview rows (handles overnight split) ---
    rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes) if (start_t and end_t) else []
    
    per_row_msgs = []
    new_time_points = 0.0
    new_prod_points = 0.0
    for r in rows:
        lines, tp_time = band_breakdown(r["Category"], r["Start"], r["End"])
        tp_time += 22.0 * float(r.get("TEE Exams", 0) or 0)
        # Compose a safe markdown chunk
        line_chunks = [f"- {r['Date'].strftime('%d/%m/%Y')} | Time points: **{tp_time:.2f}**"]
        line_chunks.extend(lines)
        per_row_msgs.append("\n".join(line_chunks))
        new_time_points += tp_time
        new_prod_points += float(r.get("Productivity Points", 0.0) or 0.0)
    
    # Current day running total (before adding)
    current_daily_total = 0.0
    if not entries.empty and start_t and end_t:
        same_day = entries[entries["Date"] == date]
        for _, er in same_day.iterrows():
            _, tptime = band_breakdown(er["Category"], er["Start"], er["End"])
            tptime += 22.0 * float(er.get("TEE Exams", 0) or 0)
            tptime += float(er.get("Productivity Points", 0) or 0)
            tptime += float(er.get("Extra Points", 0) or 0)
            current_daily_total += tptime
    
    projected_daily_total = current_daily_total + new_time_points + new_prod_points + float(extra or 0.0)
    
    # Render preview safely
    if rows:
        st.markdown("### Preview")
        for msg in per_row_msgs:
            st.markdown(msg)
        st.markdown(f"**New time points:** {new_time_points:.2f}")
        st.markdown(f"**New production points:** {new_prod_points:.2f}")
        if float(extra or 0.0) != 0.0:
            st.markdown(f"**New extra points:** {float(extra):.2f}")
        st.markdown(f"**Projected total for {date.strftime('%d/%m/%Y')}: {projected_daily_total:.2f} points**")
    else:
        st.markdown("### Preview\nEnter start/end times to see the breakdown.")
    
    # --- Add button writes to Sheets ---
    disable_add = not (start_t and end_t)
    if st.button("Add", disabled=disable_add):
        new_df = pd.DataFrame(rows)
        entries = pd.concat([entries, new_df], ignore_index=True)
        save_entries(ws_entries, entries)
        write_daily_totals(sh, entries)
        write_month_sheets(sh, entries)
        write_monthly_summary(sh, entries)
        st.success("Saved entry and updated Daily Totals, Monthly sheets, and Monthly Summary.")
        st.rerun()
    
    st.subheader("Your Entries")    def compute_category_breakdown_for_date(date_obj: dt.date, df_entries: pd.DataFrame):
        """
        Returns a list of dicts: {Category, Minutes, Hours, Points} for the selected date.
        Uses the same dominance logic as compute_day_time_points so overlaps count only once
        at the highest-paying category.
        """
        # We need per-minute winner to calculate per-category minutes and points
        # Reuse internal logic by reconstructing the minute-wise winner here
        minutes_winner = [-1.0] * 1440
        winner_cat = [None] * 1440
    
        def _minute_rate_pts_local(category: str, minute_of_day: int) -> float:
            hour = minute_of_day / 60.0
            if 7 <= hour < 17:
                mult = 1.00
            elif 17 <= hour < 23:
                mult = 1.10
            else:
                mult = 1.25
            if category in ("Assigned (General AR)", "Activation from Unrestricted Call"):
                return 20.0 * mult
            if category == "Restricted OB (In-house)":
                return 13.0 * mult
            if category == "Unrestricted Call":
                return 3.5
            # Cardiac coverage is daily fixed and not minute-based
            return 0.0
    
        has_cardiac = False
        for _, r in df_entries.iterrows():
            cat = str(r.get("Category",""))
            if cat == "Cardiac (Subspecialty) – Coverage":
                has_cardiac = True
                continue
            s = r.get("Start"); e = r.get("End")
            if not (isinstance(s, dt.time) and isinstance(e, dt.time)):
                continue
            smin = s.hour*60 + s.minute
            emin = e.hour*60 + e.minute
            if emin == smin:
                continue
            if emin < smin:
                # overnight: consider only until midnight for selected date
                intervals = [(smin, 1440)]
            else:
                intervals = [(smin, emin)]
            for a,b in intervals:
                for m in range(a,b):
                    rate = _minute_rate_pts_local(cat, m)
                    if rate > minutes_winner[m]:
                        minutes_winner[m] = rate
                        winner_cat[m] = cat
    
        per_cat_minutes = {}
        per_cat_points = {}
        for m, cat in enumerate(winner_cat):
            if cat is None:
                continue
            per_cat_minutes[cat] = per_cat_minutes.get(cat, 0) + 1
            per_cat_points[cat] = per_cat_points.get(cat, 0.0) + _minute_rate_pts_local(cat, m) / 60.0
    
        rows = []
        total_hours = 0.0
        total_points = 0.0
        for cat, mins in sorted(per_cat_minutes.items(), key=lambda kv: -kv[1]):
            hrs = round(mins/60.0, 2)
            pts = round(per_cat_points.get(cat, 0.0), 2)
            rows.append({"Category": cat, "Minutes": mins, "Hours": hrs, "Points": pts})
            total_hours += hrs
            total_points += pts
    
        if has_cardiac:
            rows.append({"Category": "Cardiac (Subspecialty) – Coverage", "Minutes": 0, "Hours": 0.0, "Points": 45.0})
            total_points += 45.0
    
        # Apply Assigned minimum if any Assigned minutes credited
        assigned_row = next((r for r in rows if r["Category"]=="Assigned (General AR)"), None)
        if assigned_row and assigned_row["Points"] < 80.0:
            addl = round(80.0 - assigned_row["Points"], 2)
            if addl > 0:
                assigned_row["Points"] = 80.0
                total_points += addl
    
        return rows, round(total_hours,2), round(total_points,2)
    

    ### ERROR BANNER GUARD END ###
except NameError as e:
    import streamlit as st
    st.warning(f"⚠️ NameError: {e}")
except KeyError as e:
    import streamlit as st
    st.warning(f"⚠️ KeyError: {e}")
except Exception as e:
    import streamlit as st
    st.warning(f"⚠️ Unexpected error: {e}")
