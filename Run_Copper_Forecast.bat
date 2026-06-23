@echo off
cd /d "%~dp0"
echo Setting up (first run takes a minute)...
if not exist ".venv" python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
echo Launching dashboard in your browser...
streamlit run app.py
pause
