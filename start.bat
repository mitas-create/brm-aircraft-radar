@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  BRM Aero Aircraft Radar - spousteni
echo ============================================
echo.

if not exist tools\cloudflared.exe (
    echo CHYBA: nenasel jsem tools\cloudflared.exe
    echo Stahni si ho z https://github.com/cloudflare/cloudflared/releases/latest
    echo ^(soubor cloudflared-windows-amd64.exe^) a uloz jako tools\cloudflared.exe
    pause
    exit /b 1
)

echo Spoustim lokalni server...
start "BRM Radar - SERVER (nezavirat)" cmd /k python lkku_radar_server.py

echo Cekam, nez se server rozjede...
timeout /t 3 /nobreak >nul

echo Spoustim verejny odkaz (Cloudflare Tunnel)...
start "BRM Radar - VEREJNY ODKAZ (nezavirat)" cmd /k tools\cloudflared.exe tunnel --url http://localhost:8765

echo.
echo Hotovo! Otevrela se 2 nova okna:
echo   1) SERVER       - musi zustat spustene po celou dobu
echo   2) VEREJNY ODKAZ - najdes v nem radek https://xxxxx.trycloudflare.com
echo                      Ten odkaz posli komukoliv, kdo ma appku pouzivat.
echo.
echo POZOR:
echo  - Pri kazdem spusteni tohoto souboru se verejny odkaz ZMENI.
echo  - Obe nova okna nechavej otevrena.
echo  - Pocitac nesmi jit spat ani se vypnout ^(Nastaveni - Napajeni a spanek - Nikdy^).
echo.
pause
