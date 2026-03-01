# start.ps1
python migrate_db.py
# start backend (separate window)
Start-Process -NoNewWindow -FilePath python -ArgumentList 'backend.py'
# start streamlit
Start-Process -NoNewWindow -FilePath streamlit -ArgumentList 'run app.py'
