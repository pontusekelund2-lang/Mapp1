@echo off
chcp 65001 >nul
title DropAgent
cd /d "%~dp0"

call :findpython
if defined PY goto havepython

echo.
echo  Python saknas. Installerar Python automatiskt (tar 1-3 minuter)...
echo.
where winget >nul 2>nul
if errorlevel 1 goto manualpython
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpython
if defined PY goto havepython

:manualpython
echo.
echo  Kunde inte installera Python automatiskt.
echo  1. Webblasaren oppnar python.org - klicka "Download Python".
echo  2. Kryssa i "Add python.exe to PATH" och klicka "Install Now".
echo  3. Dubbelklicka pa start.bat igen.
echo.
start https://www.python.org/downloads/
pause
exit /b 1

:havepython
if not exist ".venv\Scripts\python.exe" (
  echo  Forsta starten: installerar DropAgent. Det tar 1-2 minuter...
  %PY% -m venv .venv
  if errorlevel 1 ( echo  Kunde inte skapa .venv & pause & exit /b 1 )
  call :shortcut
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo  Installationen misslyckades. Kontrollera internetanslutningen och forsok igen.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" app.py
pause
exit /b 0

:findpython
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if defined PY ( set "PY="%PY%"" & goto :eof )
where py >nul 2>nul && ( py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=py -3" )
if defined PY goto :eof
where python >nul 2>nul && ( python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set "PY=python" )
goto :eof

:shortcut
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\DropAgent.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.Save()" >nul 2>nul
if not errorlevel 1 echo  En genvag "DropAgent" har lagts pa skrivbordet.
goto :eof
