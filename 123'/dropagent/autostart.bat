@echo off
chcp 65001 >nul
rem Gor sa att DropAgent startar av sig sjalv nar du loggar in pa datorn.
rem Angra: tryck Win+R, skriv shell:startup, tryck Enter och ta bort genvagen DropAgent.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\DropAgent.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (
  echo  Kunde inte lagga till autostart.
) else (
  echo  Klart! DropAgent startar nu automatiskt nar du loggar in.
  echo  Angra: tryck Win+R, skriv shell:startup och ta bort genvagen DropAgent.
)
pause
