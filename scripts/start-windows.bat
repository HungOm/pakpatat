@echo off
setlocal enabledelayedexpansion
REM ---------------------------------------------------------------------------
REM Päkpätät -- Windows launcher
REM
REM Double-click this file. That is the whole instruction.
REM
REM If Python is already on this computer it is used as-is. If it is NOT, a
REM private copy is downloaded into this folder (.python\) and used only by this
REM app -- nothing is installed system-wide, no PATH is changed, and deleting
REM this folder removes every trace. That matters because the previous version
REM of this launcher told people to install Python and tick "Add Python to
REM PATH", which is exactly the step that goes wrong.
REM
REM First run needs internet and takes a few minutes. Later runs open instantly.
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."

set "PYVER=3.12.8"
set "PYTAG=312"
set "PYDIR=.python"

REM Branded splash, printed before anything slow, so a double-click shows
REM something within a second instead of a bare cursor while pip resolves.
REM
REM ASCII only and no diaeresis in the art: cmd.exe defaults to code page 437,
REM which renders "Pakpatat" fine but turns the a-umlaut in "Pakpatat" into a
REM mojibake box. chcp 65001 below switches to UTF-8 so the proper spelling can
REM be shown on the line that matters; the owl stays plain ASCII either way.
chcp 65001 >nul 2>&1
echo.
echo         ,___,   ,___,
echo         [O,O]   [O,O]
echo         /^)__^)   /^)__^)
echo     ----"--"-----"--"----
echo.
echo     Päkpätät  -  K'Cho for owl
echo     It answers what it knows. It says so when it doesn't.
echo.
echo     Independent tool. Not affiliated with UNHCR.
echo -----------------------------------------------------------
echo.

REM --- 1. Use a Python that is already installed, if there is one -----------
set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "PY=python"
)

REM --- 2. Otherwise fetch a private copy: no installer, no admin rights -----
set "OWNPY="
if not defined PY (
    if exist "%PYDIR%\python.exe" (
        set "OWNPY=1"
    ) else (
        echo Python was not found on this computer.
        echo Downloading a private copy for this app only ^(about 11 MB^).
        echo Nothing is installed system-wide.
        echo.
        set "ARCH=amd64"
        if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=arm64"
        set "PYZIP=%TEMP%\rm-python-%PYVER%.zip"
        curl -L --fail -o "!PYZIP!" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-!ARCH!.zip"
        if errorlevel 1 goto :nonet
        mkdir "%PYDIR%" 2>nul
        tar -xf "!PYZIP!" -C "%PYDIR%" || powershell -NoProfile -Command "Expand-Archive -Path '!PYZIP!' -DestinationPath '%PYDIR%' -Force"
        if not exist "%PYDIR%\python.exe" goto :failed

        REM The embeddable build ships with site-packages switched off, so
        REM anything pip installs would be invisible to it. Switch it back on.
        > "%PYDIR%\python%PYTAG%._pth" echo python%PYTAG%.zip
        >>"%PYDIR%\python%PYTAG%._pth" echo .
        >>"%PYDIR%\python%PYTAG%._pth" echo Lib\site-packages
        >>"%PYDIR%\python%PYTAG%._pth" echo import site

        echo Adding the package installer...
        curl -L --fail -o "%TEMP%\rm-get-pip.py" "https://bootstrap.pypa.io/get-pip.py"
        if errorlevel 1 goto :nonet
        "%PYDIR%\python.exe" "%TEMP%\rm-get-pip.py" --no-warn-script-location
        if errorlevel 1 goto :failed
        set "OWNPY=1"
    )
)

REM --- 3. Decide which interpreter runs the app -----------------------------
if defined OWNPY (
    set "RUN=%PYDIR%\python.exe"
    set "ENVDIR=%PYDIR%"
) else (
    if not exist ".venv" (
        echo First-time setup: creating a private Python environment...
        %PY% -m venv .venv
        if errorlevel 1 goto :failed
    )
    set "RUN=.venv\Scripts\python.exe"
    set "ENVDIR=.venv"
)

REM --- 4. Install the components (first run only) ---------------------------
if not exist "!ENVDIR!\.installed" (
    echo Installing components ^(a few minutes, one time only^)...
    "!RUN!" -m pip install --upgrade pip --quiet
    "!RUN!" -m pip install -r requirements.txt --quiet
    if errorlevel 1 goto :failed
    echo.> "!ENVDIR!\.installed"
    echo Components installed.
)

REM --- 5. Build the offline search index (first run only) -------------------
REM
REM Ask config.py where the index actually lives rather than hardcoding a path.
REM An earlier version checked ".index\meta.json", which config.py has never
REM written -- the real default is "data\index\meta.json", and PAKPATAT_DATA
REM can move it anywhere. The check therefore never passed, so EVERY launch
REM silently re-embedded the whole corpus instead of opening instantly.
set "INDEXMETA="
for /f "delims=" %%I in ('"!RUN!" -c "from pakpatat import config; print(config.INDEX_META)" 2^>nul') do set "INDEXMETA=%%I"
if not defined INDEXMETA set "INDEXMETA=data\index\meta.json"
if not exist "!INDEXMETA!" (
    echo.
    echo Building the offline search index ^(one time, downloads about 220 MB^)...
    "!RUN!" build_index.py
    if errorlevel 1 goto :failed
)

REM --- 6. Settings file -----------------------------------------------------
if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
)

REM Same five checks the splash runs. Shown here too because this window is
REM where the suggested fix commands can actually be typed.
echo.
"!RUN!" -m pakpatat.preflight

echo.
echo To keep every question on this computer, install Ollama from
echo https://ollama.com/download -- the app starts it for you after that.
echo Otherwise open Settings inside the app and choose an online provider.
echo.
echo Starting Päkpätät...
"!RUN!" app.py
goto :eof

:nonet
echo.
echo Could not download what is needed. Please check the internet connection
echo and try again. If this computer is behind a company or campus firewall,
echo it may be blocking python.org.
echo.
pause
exit /b 1

:failed
echo.
echo Setup failed. Please send the message above to whoever supports this tool.
echo.
pause
exit /b 1
