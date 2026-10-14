@echo off
setlocal
:: Script build AMConnectGDT.exe cho Windows
:: Chạy file này 1 lần trên máy Windows để tạo ra AMConnectGDT.exe

cd /d "%~dp0"

echo === Build AMnote GDT ===
echo.

:: Kiểm tra Python
where python >nul 2>&1
if errorlevel 1 (
    echo [LOI] Chua cai Python. Tai tai: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Cai thu vien...
python -m pip install -r requirements_agent.txt
if errorlevel 1 (
    echo [LOI] Khong cai duoc thu vien trong requirements_agent.txt.
    pause
    exit /b 1
)
python -m pip install pyinstaller
if errorlevel 1 (
    echo [LOI] Khong cai duoc PyInstaller.
    pause
    exit /b 1
)
echo.

echo [2/3] Build AMConnectGDT.exe (version 1.0.7)...
python -m PyInstaller --clean --noconfirm AMConnectGDT.spec
if errorlevel 1 (
    echo [LOI] Build that bai. Xem log ben tren.
    pause
    exit /b 1
)
echo.

echo [3/3] Ket qua:
if exist dist\AMConnectGDT.exe (
    copy /Y config.env.example dist\config.env.example >nul
    echo [OK] File tao thanh cong: dist\AMConnectGDT.exe
    echo.
    echo Gui cho IT 2 file:
    echo   dist\AMConnectGDT.exe
    echo   config.env.example  ^(doi ten thanh config.env va dieu chinh khi can^)
) else (
    echo [LOI] Build that bai. Xem log ben tren.
)

echo.
pause
endlocal
