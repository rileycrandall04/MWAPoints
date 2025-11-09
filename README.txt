
MWA Points Tracker — Fixed Version
----------------------------------

This build fixes the issue where adding multiple intervals caused start or end times to reset to 00:00 or copy another interval's time.

✅ Each interval now has a unique stable ID, so Streamlit keeps distinct start/end inputs.
✅ Works with up to 10+ intervals.
✅ All original features (cross-midnight handling, 2-day holiday toggles, one-time adders, compensation band breakdown) remain unchanged.

Run with:
  streamlit run app_fixed.py
