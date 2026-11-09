@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "P=%~dp0"
if /i "%P:~0,16%"=="\\wsl.localhost\" goto wsl_path
if /i "%P:~0,7%"=="\\wsl$\" goto wsl_path
wsl.exe --cd "%~dp0." --exec python3 -m voxcut.bootstrap
set "RC=%errorlevel%"
goto finished
:wsl_path
for /f "tokens=2* delims=\" %%a in ("%~dp0") do (set "D=%%a" & set "R=%%b")
set "R=/%R:\=/%"
wsl.exe -d "%D%" --cd "%R%" --exec python3 -m voxcut.bootstrap
set "RC=%errorlevel%"
:finished
if not "%RC%"=="0" pause
exit /b %RC%
