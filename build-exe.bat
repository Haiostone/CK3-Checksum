@echo off
REM Builds dist\ck3-checksums.exe. Needs Python on PATH.
setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)

echo Installing PyInstaller...
python -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto :failed

echo.
echo Building...
python -m PyInstaller ^
    --onefile ^
    --console ^
    --name ck3-checksums ^
    --clean ^
    --noconfirm ^
    --exclude-module tkinter ^
    --exclude-module unittest ^
    --exclude-module pydoc ^
    --exclude-module email ^
    --exclude-module xml ^
    main.py
if errorlevel 1 goto :failed

echo.
echo Done: %CD%\dist\ck3-checksums.exe
pause
exit /b 0

:failed
echo.
echo Build failed - see the messages above.
pause
exit /b 1
