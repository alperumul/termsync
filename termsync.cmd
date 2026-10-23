@echo off
REM Use this source checkout's venv regardless of the caller's directory.
REM No interpreter fallback: the wrong Python could silently miss captures.
setlocal
set "TERMSYNC_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%TERMSYNC_PYTHON%" (
    echo termsync.cmd: venv python not found at "%TERMSYNC_PYTHON%" -- follow the source-checkout setup in README.md. Refusing to fall back to a bare "python" on PATH. 1>&2
    exit /b 1
)

if "%~1"=="" goto restore
"%TERMSYNC_PYTHON%" -m termsync %*
exit /b %ERRORLEVEL%

:restore
"%TERMSYNC_PYTHON%" -m termsync restore
exit /b %ERRORLEVEL%
