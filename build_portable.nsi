; NSIS Script for Facebook OSINT Portable Executable
Unicode True
SilentInstall silent
AutoCloseWindow true
RequestExecutionLevel user

Name "Facebook OSINT & Forensics Suite"
OutFile "Facebook_OSINT.exe"

!include "FileFunc.nsh"

Section "Main"
    ; Lokalizacja cache srodowiska w LocalAppData
    StrCpy $R0 "$LOCALAPPDATA\FacebookOSINT_Runtime"

    ; Jesli wersja runtime nie istnieje, rozpakuj
    IfFileExists "$R0\python_env\python.exe" run_app extract_runtime

extract_runtime:
    SetOutPath "$R0\app"
    File /r "dist_build/app/*.*"

    SetOutPath "$R0\python_env"
    File /r "dist_build/python_env/*.*"

    SetOutPath "$R0"
    File "dist_build/Uruchom_Facebook_OSINT.bat"

run_app:
    ; Zaktualizuj skrypty aplikacji, aby zawsze byly najnowsze
    SetOutPath "$R0\app"
    File /r "dist_build/app/*.*"

    ; Uruchom w katalogu roboczym, z ktorego odpalono Facebook_OSINT.exe ($EXEDIR)
    SetOutPath "$EXEDIR"

    ; Pobierz parametry wiersza polecen
    ${GetParameters} $R1

    ; Uruchomienie konsoli Windows i zatrzymanie na koniec
    ExecWait '"cmd.exe" /c ""$R0\python_env\python.exe" "$R0\app\scrape_all.py" $R1 & pause"'
SectionEnd
