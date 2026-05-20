# Streamlit UI — Backup / Reference Implementation

This Streamlit app is the original UI for the upgrade tool. It is **kept as a
working backup** alongside the new `upgrade-api/` + `upgrade-web/` stack.

- **Run with:** `streamlit run upgrade-frontend/app.py`
- **State files:** `projects.json`, `run_state/*.json`, `output/` — shared with
  the new UI. You can switch between Streamlit and the Next.js UI at any time
  without migrating data.
- **Status:** feature-frozen. Bug fixes only. New features land in `upgrade-web/`.
