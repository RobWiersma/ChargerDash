@echo off
rem Builds dist\ChargerDash.exe: a single-file Windows app, no Python needed to run it.
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ChargerDash ^
  --icon icon.ico --add-data "static;static" app.py || exit /b 1
echo.
echo Built dist\ChargerDash.exe
