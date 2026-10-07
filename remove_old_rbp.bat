@echo off
echo ============================================================
echo  Remove OLD duplicate Replay Buffer Pro (Program Files copy)
echo  Run this AFTER closing OBS Studio, as Administrator.
echo ============================================================
pause
del "C:\Program Files\obs-studio\obs-plugins\64bit\replay-buffer-pro.dll"
rd /s /q "C:\Program Files\obs-studio\data\obs-plugins\replay-buffer-pro"
echo.
echo Done. Start OBS and check the log for a single plugin load.
pause