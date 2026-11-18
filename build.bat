@echo off
setlocal
cd /d "%~dp0"

echo Building IndeedBot.exe ...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name IndeedBot ^
  --collect-all playwright ^
  --collect-all patchright ^
  --hidden-import=bot.gui ^
  gui_main.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

copy /Y config.yaml dist\config.yaml >nul
copy /Y .env.example dist\.env.example >nul
copy /Y profile.example.txt dist\profile.example.txt >nul
echo Done: dist\IndeedBot.exe
endlocal
