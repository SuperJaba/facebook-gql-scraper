# Facebook OSINT Scraper — Launcher PowerShell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -Path $PSScriptRoot

Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host "          Facebook OSINT Scraper — Automatyczny Starter             " -ForegroundColor Cyan
Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Sprawdzenie Pythona
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "[BŁĄD] Nie znaleziono interpretera Python w systemie!" -ForegroundColor Red
    Write-Host "Zainstaluj Python z python.org (zaznacz opcję 'Add Python to PATH')." -ForegroundColor Yellow
    Read-Host "Naciśnij Enter, aby zakończyć..."
    exit 1
}

# 2. Tworzenie środowiska wirtualnego dla Windows (.venv_win)
if (-not (Test-Path ".venv_win\Scripts\python.exe")) {
    Write-Host "[1/3] Tworzenie środowiska wirtualnego Windows (.venv_win)..." -ForegroundColor Green
    python -m venv .venv_win
    Write-Host "[2/3] Instalowanie wymaganych bibliotek..." -ForegroundColor Green
    & .venv_win\Scripts\python.exe -m pip install --upgrade pip
    & .venv_win\Scripts\pip.exe install -e .
    & .venv_win\Scripts\pip.exe install -r requirements.txt
    Write-Host "[3/3] Instalacja zakończona sukcesem!" -ForegroundColor Green
    Write-Host ""
}

# 3. Uruchomienie aplikacji
Write-Host "Startowanie Facebook OSINT Scraper..." -ForegroundColor Cyan
Write-Host ""
& .venv_win\Scripts\python.exe scrape_all.py $args

Write-Host ""
Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host "Program zakończył działanie." -ForegroundColor Cyan
Read-Host "Naciśnij Enter, aby zamknąć okno..."
