@echo off
REM Double-click this.
REM
REM It exists because a .ps1 cannot be double-clicked into running: Windows
REM opens it in Notepad, and even from a terminal the default execution policy
REM on client editions refuses it outright ("running scripts is disabled on
REM this system"). A .bat has neither problem, and -ExecutionPolicy Bypass
REM here applies to this one process only -- your machine's setting is not
REM touched.
REM
REM The pause at the end matters just as much: a window launched from Explorer
REM closes the instant the script ends, taking the result with it.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo The installer stopped with an error. The message above says why.
echo.
pause
exit /b %RC%
