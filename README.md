# MWA Points Tracker — Streamlit (Google Sign-In + Per-User Google Drive)

## Deploy on Streamlit Cloud
1. Create **Google OAuth** Web credentials and enable **Drive** + **Sheets** APIs.
2. In Streamlit Cloud → **App → Settings → Secrets**, paste:
```
[oauth]
client_id = "YOUR_GOOGLE_CLIENT_ID"
client_secret = "YOUR_GOOGLE_CLIENT_SECRET"
redirect_uri = "https://YOUR-APP-NAME.streamlit.app"
```
3. Deploy app (main file: `app.py`).

## Run locally
```
pip install -r requirements.txt
mkdir -p .streamlit
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
# edit secrets.toml with your OAuth client
streamlit run app.py
```

## Notes
- Each user logs in with Google and data is stored in their own Google Drive (`drive.file` scope).
- Rules included: Assigned (4-hr min), Activation, OB, Unrestricted Call, Cardiac +45/day, TEE +22, overnight handling, rate-band splits.
