@echo off
setlocal
cd /d "%~dp0"

set "PY38=C:\Users\admin\AppData\Local\Programs\Python\Python38\python.exe"
set "VENV_DIR=%~dp0.venv-win7"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "DIST_DIR=%~dp0dist\win7-win11-x64"
set "WORK_DIR=%~dp0build\win7-win11-x64"

echo === Build AMConnectGDT cho Windows 7 / 10 / 11 x64 ===
echo.

if not exist "%VENV_PY%" (
    if not exist "%PY38%" (
        echo [LOI] Khong tim thay Python 3.8.10 x64 tai:
        echo       %PY38%
        echo Cai Python 3.8.10 x64 roi chay lai script.
        exit /b 1
    )
    echo [1/4] Tao virtual environment Python 3.8...
    "%PY38%" -m venv "%VENV_DIR%"
    if errorlevel 1 exit /b 1
) else (
    echo [1/4] Da co virtual environment Python 3.8.
)

echo [2/4] Kiem tra dung Python 3.8...
"%VENV_PY%" -c "import sys; assert sys.version_info[:2] == (3, 8), sys.version"
if errorlevel 1 (
    echo [LOI] Build Windows 7 bat buoc dung Python 3.8.
    exit /b 1
)

echo [3/4] Cai bo thu vien da khoa phien ban...
"%VENV_PY%" -m pip install -r requirements_win7.txt
if errorlevel 1 exit /b 1

echo [4/4] Dong goi executable ^(AMConnectGDT.spec + curl_cffi^)...
"%VENV_PY%" -m PyInstaller ^
    --clean ^
    --noconfirm ^
    --distpath "%DIST_DIR%" ^
    --workpath "%WORK_DIR%" ^
    AMConnectGDT.spec
if errorlevel 1 exit /b 1

if not exist "%DIST_DIR%\AMConnectGDT.exe" (
    echo [LOI] Khong thay %DIST_DIR%\AMConnectGDT.exe
    exit /b 1
)

copy /Y config.env.example "%DIST_DIR%\config.env.example" >nul
copy /Y requirements_win7.txt "%DIST_DIR%\requirements_win7.txt" >nul
if exist README_WIN7_11.md copy /Y README_WIN7_11.md "%DIST_DIR%\README_WIN7_11.md" >nul

echo.
echo [OK] Da tao ban dung chung Windows 7 / 10 / 11 x64:
echo      %DIST_DIR%\AMConnectGDT.exe
echo.
echo Luu y: Windows 32-bit can build rieng bang Python 3.8.10 x86.
endlocal
