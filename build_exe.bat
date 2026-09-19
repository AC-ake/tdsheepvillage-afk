@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Build tdsheepvillage-afk 1.2.1 release
echo ============================================
python --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH.
  pause
  exit /b 1
)
python make_release.py
if errorlevel 1 (
  echo [ERROR] build failed, check the messages above.
) else (
  echo [DONE] dist\tdsheepvillage-afk1.2.1.zip
)
pause
