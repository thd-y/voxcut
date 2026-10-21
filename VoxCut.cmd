@echo off
rem Проба (план UI, задача 1): двойной клик с \\wsl.localhost\
chcp 65001 >nul
echo dp0 = %~dp0
echo CD  = %CD%
for /f "tokens=2* delims=\" %%a in ("%~dp0") do (set "D=%%a" & set "R=%%b")
set "R=/%R:\=/%"
echo distro = %D%
echo linux  = %R%
wsl.exe -d %D% --cd "%R%" -- pwd
echo exit = %ERRORLEVEL%
pause
