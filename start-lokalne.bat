@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  BRM Aero Aircraft Radar
echo ============================================
echo.
echo Spoustim appku... prohlizec se otevre sam.
echo Toto okno nechavej otevrene, dokud appku pouzivas.
echo Ukoncis ji zavrenim tohoto okna nebo klavesami Ctrl+C.
echo.

python lkku_radar_server.py

echo.
echo Appka byla ukoncena.
pause
