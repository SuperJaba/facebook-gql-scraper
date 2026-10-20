@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Facebook OSINT Scraper - Launcher
cd /d "%~dp0"

:: Jesli skrypt zostal uruchomiony z glownego folderu Downloads, przejdz do podfolderu facebook-osint
if exist "%~dp0facebook-osint\scrape_all.py" (
    cd /d "%~dp0facebook-osint"
)

echo ====================================================================
echo           Facebook OSINT Scraper - Automatyczny Starter
echo ====================================================================
echo.

:: 1. Sprawdzenie Pythona
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [BLAD] Nie znaleziono Pythona w systemie Windows!
    echo Zainstaluj Python z python.org (zaznacz opcje: Add Python to PATH).
    echo.
    pause
    exit /b 1
)

:: 2. Sprawdzenie lub utworzenie srodowiska wirtualnego dla Windows (.venv_win)
if not exist ".venv_win\Scripts\python.exe" (
    echo [1/3] Tworzenie srodowiska wirtualnego Windows (.venv_win)...
    python -m venv .venv_win
    echo [2/3] Instalowanie wymaganych bibliotek (tylko za pierwszym razem)...
    .venv_win\Scripts\python.exe -m pip install --upgrade pip
    .venv_win\Scripts\pip.exe install -e .
    .venv_win\Scripts\pip.exe install -r requirements.txt
    echo [3/3] Instalacja zakonczona sukcesem!
    echo.
)

:: 3. Uruchomienie aplikacji
echo Startowanie Facebook OSINT Scraper...
echo.
.venv_win\Scripts\python.exe scrape_all.py %*

echo.
echo ====================================================================
echo Zakonczono dzialanie programu.
echo Nacisnij dowolny klawisz, aby zamknac...
echo ====================================================================
pause >nul
