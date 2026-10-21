@echo off
rem VoxCut: двойной клик из папки продукта в \\wsl.localhost (задача 1 плана UI)
chcp 65001 >nul
for /f "tokens=2* delims=\" %%a in ("%~dp0") do (set "D=%%a" & set "R=%%b")
set "R=/%R:\=/%"
wsl.exe -d %D% --cd "%R%" -- make ui
if errorlevel 1 pause
