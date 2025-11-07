# app.py
# (see previous cell for full implementation notes)

import streamlit as st
import pandas as pd
import datetime as dt
import time

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import gspread

st.set_page_config(page_title='MWA Points Tracker â€” Live Preview', layout='wide')

# OAuth / Config
OAUTH_CLIENT_ID = st.secrets['oauth']['client_id']
OAUTH_CLIENT_SECRET = st.secrets['oauth']['client_secret']
REDIRECT_URI = st.secrets['oauth']['redirect_uri']
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/spreadsheets'
]

def parse_time_flex(s):
    if s is None: return None
    s = str(s).strip()
    if s == '': return None
    if not s.isdigit(): return None
    if len(s)==1: hh,mm=int(s),0
    elif len(s)==2: hh,mm=int(s),0
    elif len(s)==3: hh,mm=int(s[0]),int(s[1:])
    elif len(s)==4: hh,mm=int(s[:2]),int(s[2:])
    else: return None
    if not (0<=hh<=23 and 0<=mm<=59): return None
    return dt.time(hh,mm)

def fmt_hhmm(t):
    return t.strftime('%H:%M') if isinstance(t, dt.time) else ''

def hours_overlap(start_h, end_h, a, b):
    if end_h < start_h: end_h += 24
    total = 0.0
    for A,B in [(a,b),(a+24,b+24)]:
        lo, hi = max(start_h,A), min(end_h,B)
        if hi>lo: total += (hi-lo)
    return total

def split_bands(start_t, end_t):
    if not (isinstance(start_t, dt.time) and isinstance(end_t, dt.time)):
        return 0.0,0.0,0.0,0.0
    s = start_t.hour + start_t.minute/60
    e = end_t.hour + end_t.minute/60
    total = (e - s) % 24
    day = hours_overlap(s,e,7,17)
    eve = hours_overlap(s,e,17,23)
    nit = hours_overlap(s,e,23,24)+hours_overlap(s,e,0,7)
    return total, day, eve, nit

def cap_ar_points(base, d,e,n):
    return d*base*1.00 + e*base*1.10 + n*base*1.25

def cap_ob_points(d,e,n):
    base=13.0
    return d*base*1.00 + e*base*1.10 + n*base*1.25

def get_auth_flow(state):
    client_config = {'web': {
        'client_id': OAUTH_CLIENT_ID,
        'client_secret': OAUTH_CLIENT_SECRET,
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
        'redirect_uris': [REDIRECT_URI],
    }}
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.params={'access_type':'offline','include_granted_scopes':'true','prompt':'consent'}
    return flow

def login_button():
    state = st.session_state.get('oauth_state') or str(int(time.time()))
    st.session_state['oauth_state']=state
    auth_url,_ = get_auth_flow(state).authorization_url(state=state)
    st.link_button('ðŸ” Sign in with Google', auth_url, use_container_width=True)

def exchange_code_for_token():
    params = st.query_params
    if 'state' in params and 'code' in params:
        flow = get_auth_flow(params['state'])
        flow.fetch_token(code=params['code'])
        return flow.credentials
    return None

def ensure_user_sheet(gc):
    name='MWA Points Data'
    try: sh = gc.open(name)
    except: sh = gc.create(name)
    try: ws_entries = sh.worksheet('Entries')
    except:
        ws_entries = sh.add_worksheet(title='Entries', rows=1000, cols=20)
        ws_entries.update('A1:H1', [['Date','Category','Start','End','TEE Exams','Productivity Points','Extra Points','Notes']])
    try: ws_holidays = sh.worksheet('Holidays')
    except:
        ws_holidays = sh.add_worksheet(title='Holidays', rows=100, cols=1)
        ws_holidays.update('A1:A1', [['Holiday Dates (YYYY-MM-DD)']])
    return sh, ws_entries, ws_holidays

def load_entries(ws_entries):
    vals = ws_entries.get_all_records()
    if not vals:
        return pd.DataFrame(columns=['Date','Category','Start','End','TEE Exams','Productivity Points','Extra Points','Notes'])
    df = pd.DataFrame(vals)
    if 'Date' in df: df['Date']=pd.to_datetime(df['Date']).dt.date
    for col in ['Start','End']:
        if col in df:
            def to_time(x):
                try:
                    hh,mm = [int(t) for t in str(x).split(':')]
                    return dt.time(hh%24, mm%60)
                except: return None
            df[col]=df[col].apply(to_time)
    for col in ['TEE Exams','Productivity Points','Extra Points']:
        if col in df: df[col]=pd.to_numeric(df[col], errors='coerce').fillna(0)
    for col in ['Category','Notes']:
        if col in df: df[col]=df[col].fillna('')
    return df

def save_entries(ws_entries, df):
    header=['Date','Category','Start','End','TEE Exams','Productivity Points','Extra Points','Notes']
    ws_entries.clear(); ws_entries.update('A1:H1',[header])
    out=[]
    for _,r in df.iterrows():
        d = r['Date'].strftime('%Y-%m-%d') if isinstance(r['Date'], dt.date) else ''
        fmt=lambda t: t.strftime('%H:%M') if isinstance(t, dt.time) else ''
        out.append([d, r.get('Category',''), fmt(r.get('Start')), fmt(r.get('End')), r.get('TEE Exams',0),
                    r.get('Productivity Points',0), r.get('Extra Points',0), r.get('Notes','')])
    if out: ws_entries.update(f'A2:H{len(out)+1}', out)

def entry_time_points(category, date, start_t, end_t):
    ttl,d,e,n = split_bands(start_t, end_t)
    if category=='Unrestricted Call':
        return ttl*3.5
    if category in ('Assigned (General AR)','Activation from Unrestricted Call'):
        pts = cap_ar_points(20.0, d,e,n)
        if category=='Assigned (General AR)': pts = max(pts, 80.0)
        return pts
    if category=='Restricted OB (In-house)':
        return cap_ob_points(d,e,n)
    if category=='Cardiac (Subspecialty) â€“ Coverage':
        return 45.0
    return 0.0

def preview_rows(date, category, start_t, end_t, tee, prod, extra, notes):
    rows=[]
    if end_t < start_t:
        rows.append({'Date':date,'Category':category,'Start':start_t,'End':dt.time(23,59),
                     'TEE Exams':tee,'Productivity Points':prod,'Extra Points':extra,'Notes':notes})
        rows.append({'Date':date+dt.timedelta(days=1),'Category':category,'Start':dt.time(0,0),'End':end_t,
                     'TEE Exams':0,'Productivity Points':0.0,'Extra Points':0.0,'Notes':f'(overnight from {date}) '+(notes or '')})
    else:
        rows.append({'Date':date,'Category':category,'Start':start_t,'End':end_t,
                     'TEE Exams':tee,'Productivity Points':prod,'Extra Points':extra,'Notes':notes})
    return rows

st.title('MWA Points Tracker â€” Live Preview')

creds = st.session_state.get('creds')
if not creds:
    maybe = exchange_code_for_token()
    if maybe:
        st.session_state['creds']=maybe
        creds = maybe
if not creds:
    st.info('Sign in with Google to save your data to your own Drive.'); login_button(); st.stop()

gc = gspread.authorize(creds)
sh, ws_entries, ws_holidays = ensure_user_sheet(gc)

entries = load_entries(ws_entries)

CATEGORIES=[
    'Assigned (General AR)',
    'Activation from Unrestricted Call',
    'Restricted OB (In-house)',
    'Unrestricted Call',
    'Cardiac (Subspecialty) â€“ Coverage',
]

tab_entries, tab_summary = st.tabs(['Entries','Summary'])

with tab_entries:
    st.subheader('Add Entry')
    with st.form('add_entry_form', clear_on_submit=False):
        c = st.columns([1,1.4,1,1,1,1,1,2])
        date = c[0].date_input('Date', value=dt.date.today())
        category = c[1].selectbox('Category', CATEGORIES, index=0)
        start_str = c[2].text_input('Start (HHMM, e.g., 700 or 1735)', '')
        end_str   = c[3].text_input('End (HHMM, e.g., 945 or 2310)', '')
        tee   = c[4].number_input('TEE Exams', min_value=0, step=1, value=0)
        prod  = c[5].number_input('Productivity Points', min_value=0.0, step=1.0, value=0.0)
        extra = c[6].number_input('Extra Points', min_value=0.0, step=1.0, value=0.0)
        notes = c[7].text_input('Notes','')

        start_t = parse_time_flex(start_str) if start_str else None
        end_t   = parse_time_flex(end_str) if end_str else None

        preview_box = st.empty()
        can_preview=True
        msgs=[]
        if start_str and start_t is None: msgs.append('âŒ Start time invalid (use 9, 930, 0715, 2300).'); can_preview=False
        if end_str and end_t is None: msgs.append('âŒ End time invalid (use 9, 930, 0715, 2300).'); can_preview=False

        if start_t and end_t and can_preview:
            rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes)
            per_row_msgs=[]; new_time=0.0; new_prod=0.0
            for r in rows:
                tp = entry_time_points(r['Category'], r['Date'], r['Start'], r['End'])
                tp += 22.0*float(r.get('TEE Exams',0) or 0)
                per_row_msgs.append(f"- {r['Date']} | {fmt_hhmm(r['Start'])}â€“{fmt_hhmm(r['End'])} â†’ time pts **{tp:.2f}**")
                new_time += tp; new_prod += float(r.get('Productivity Points',0.0) or 0.0)
            current_daily=0.0
            if not entries.empty:
                same = entries[entries['Date']==date]
                for _,r in same.iterrows():
                    tp = entry_time_points(r['Category'], r['Date'], r['Start'], r['End'])
                    tp += 22.0*float(r.get('TEE Exams',0) or 0)
                    tp += float(r.get('Productivity Points',0) or 0)
                    tp += float(r.get('Extra Points',0) or 0)
                    current_daily += tp
            projected = current_daily + new_time + new_prod + float(extra or 0.0)
            preview_box.markdown("### Preview\n"+ "\n".join(per_row_msgs) + f"\n\n**New time points:** {new_time:.2f}\n**New production points:** {new_prod:.2f}" + (f"\n**New extra points:** {float(extra):.2f}" if extra else "") + f"\n\n**Projected total for {date}: {projected:.2f} points**")
        elif msgs:
            preview_box.error("\n".join(msgs))

        submitted = st.form_submit_button('Add')
        if submitted:
            if not start_t or not end_t:
                st.error('Both start and end times are required (valid HHMM).'); st.stop()
            rows = preview_rows(date, category, start_t, end_t, tee, prod, extra, notes)
            new_df = pd.DataFrame(rows)
            entries = pd.concat([entries, new_df], ignore_index=True)
            save_entries(ws_entries, entries)
            st.success('Saved entry' + (' (split overnight into two rows)' if len(rows)==2 else '') + '.')
            st.experimental_rerun()

    st.subheader('Your Entries')
    if entries.empty:
        st.info('No entries yet.')
    else:
        show = entries.copy()
        show['Start']=show['Start'].apply(fmt_hhmm)
        show['End']=show['End'].apply(fmt_hhmm)
        st.dataframe(show, use_container_width=True, hide_index=True)

with tab_summary:
    st.subheader('Daily Summary')
    if entries.empty:
        st.info('No data yet.')
    else:
        comp=[]
        for _,r in entries.iterrows():
            tp = entry_time_points(r['Category'], r['Date'], r['Start'], r['End'])
            tp += 22.0*float(r.get('TEE Exams',0) or 0)
            total = tp + float(r.get('Productivity Points',0) or 0) + float(r.get('Extra Points',0) or 0)
            comp.append({'Date': r['Date'], 'Total Points': total})
        daily = pd.DataFrame(comp).groupby('Date', as_index=False)['Total Points'].sum().sort_values('Date')
        daily['Running Monthly Total'] = daily['Total Points'].cumsum()
        st.dataframe(daily, use_container_width=True, hide_index=True)
