; NSIS Script for Facebook OSINT Scraper Installer
Unicode True
!include "MUI2.nsh"

Name "Facebook OSINT Scraper"
OutFile "Instalator_Facebook_OSINT.exe"
InstallDir "$LOCALAPPDATA\FacebookOSINT"
InstallDirRegKey HKCU "Software\FacebookOSINT" ""
RequestExecutionLevel user

; Interface Configuration
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\orange-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\orange-uninstall.ico"

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\Uruchom_Facebook_OSINT.bat"
!define MUI_FINISHPAGE_RUN_TEXT "Uruchom Facebook OSINT Scraper teraz"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Languages
!insertmacro MUI_LANGUAGE "Polish"

Section "Główny program" SecMain
    SetOutPath "$INSTDIR"

    ; Skopiuj skrypt uruchomieniowy
    File "dist_build/Uruchom_Facebook_OSINT.bat"

    ; Skopiuj aplikację
    SetOutPath "$INSTDIR\app"
    File /r "dist_build/app/*.*"

    ; Skopiuj środowisko Python z bibliotekami
    SetOutPath "$INSTDIR\python_env"
    File /r "dist_build/python_env/*.*"

    ; Skrót na Pulpicie
    SetOutPath "$INSTDIR"
    CreateShortcut "$DESKTOP\Facebook OSINT Scraper.lnk" "$INSTDIR\Uruchom_Facebook_OSINT.bat" "" "$INSTDIR\python_env\python.exe" 0 SW_SHOWNORMAL "" "Facebook OSINT Profile Scraper"

    ; Menu Start
    CreateDirectory "$SMPROGRAMS\Facebook OSINT"
    CreateShortcut "$SMPROGRAMS\Facebook OSINT\Facebook OSINT Scraper.lnk" "$INSTDIR\Uruchom_Facebook_OSINT.bat" "" "$INSTDIR\python_env\python.exe" 0
    CreateShortcut "$SMPROGRAMS\Facebook OSINT\Odinstaluj.lnk" "$INSTDIR\Uninstall.exe"

    ; Zapisz klucz instalacji i deinstalator
    WriteRegStr HKCU "Software\FacebookOSINT" "" $INSTDIR
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FacebookOSINT" "DisplayName" "Facebook OSINT Scraper"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FacebookOSINT" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FacebookOSINT" "DisplayIcon" "$INSTDIR\python_env\python.exe"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
    Delete "$DESKTOP\Facebook OSINT Scraper.lnk"
    RMDir /r "$SMPROGRAMS\Facebook OSINT"
    RMDir /r "$INSTDIR"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\FacebookOSINT"
    DeleteRegKey HKCU "Software\FacebookOSINT"
SectionEnd
