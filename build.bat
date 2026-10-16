@echo off
setlocal
cd /d "%~dp0"

REM ---- find Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during installation.
    goto :fail
)
echo Using: %PY%
%PY% --version
if errorlevel 1 goto :fail

REM ---- virtual environment ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
set "VPY=.venv\Scripts\python.exe"

echo Installing dependencies...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%VPY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

REM ---- native core (Rust): optional, makes PST export several times faster ----
where cargo >nul 2>nul
if errorlevel 1 (
    echo Rust not found: building without the native core ^(PST export will be slower^).
    echo To enable it, install Rust from https://rustup.rs and run build.bat again.
) else (
    echo Building native core ^(Rust^)...
    "%VPY%" -m pip install ./native
    if errorlevel 1 goto :fail
)

echo Building exe...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "Shinra Tensei" --hidden-import win32timezone --hidden-import shinra_core app.py
if errorlevel 1 goto :fail

if not exist "dist\Shinra Tensei.exe" goto :fail
echo.
echo ===== DONE: %~dp0dist\Shinra Tensei.exe =====
pause
exit /b 0

:fail
echo.
echo ===== BUILD FAILED, see messages above =====
pause
exit /b 1
