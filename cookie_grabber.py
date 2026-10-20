"""
Automatyczny ekstraktor ciasteczek sesyjnych Facebooka.
Wykrywa i pobiera ciasteczka bezpośrednio z zainstalowanych przeglądarek
(Firefox, Chrome, Brave, Edge, Opera) - obsługuje Windows, WSL oraz Linux.
"""

import os
import sys
import json
import glob
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

COOKIES_OUT_FILE = Path(__file__).resolve().parent / "facebook_cookies.json"


def _extract_from_firefox_sqlite(db_path: Path) -> List[Dict[str, Any]]:
    """Wyciąga ciasteczka facebook.com z bazy SQLite profilu Firefox."""
    if not db_path.exists():
        return []

    cookies = []
    tmp_fd, tmp_path = tempfile.mkstemp(suffix="_fb_cookies.sqlite")
    os.close(tmp_fd)

    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name, value, host, path, isSecure, isHttpOnly, expiry, sameSite
            FROM moz_cookies 
            WHERE host LIKE '%facebook.com%'
        """)
        rows = cursor.fetchall()
        for r in rows:
            name, value, host, path, is_sec, is_http, expiry, same_site = r
            cookies.append({
                "name": name,
                "value": value,
                "domain": host,
                "path": path,
                "secure": bool(is_sec),
                "httpOnly": bool(is_http),
                "expirationDate": expiry,
                "sameSite": "Lax" if same_site == 1 else ("Strict" if same_site == 2 else "no_restriction"),
            })
        conn.close()
    except Exception:
        pass
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return cookies


def _find_firefox_cookie_dbs() -> List[Tuple[str, Path]]:
    """Zwraca listę znalezionych baz ciasteczek Firefoxa (Linux + Windows/WSL)."""
    candidates = []

    # 1. Ścieżki linuksowe
    home = Path.home()
    linux_patterns = [
        home / ".mozilla/firefox/*/cookies.sqlite",
        home / ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite",
        home / "snap/firefox/common/.mozilla/firefox/*/cookies.sqlite",
    ]
    for pat in linux_patterns:
        for p in glob.glob(str(pat)):
            candidates.append(("Firefox (Linux)", Path(p)))

    # 2. Ścieżki Windows (w tym WSL /mnt/c/Users)
    win_drives = ["/mnt/c", "/c", "C:"]
    for drive in win_drives:
        win_users = Path(drive) / "Users"
        try:
            if win_users.exists():
                for u_dir in win_users.iterdir():
                    try:
                        ff_dir = u_dir / "AppData/Roaming/Mozilla/Firefox/Profiles"
                        if ff_dir.exists():
                            for prof in ff_dir.iterdir():
                                c_file = prof / "cookies.sqlite"
                                if c_file.exists():
                                    candidates.append((f"Firefox (Windows - {u_dir.name})", c_file))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            continue

    # W środowisku czystego Windowsa:
    appdata = os.environ.get("APPDATA")
    if appdata:
        ff_win = Path(appdata) / "Mozilla/Firefox/Profiles"
        if ff_win.exists():
            for prof in ff_win.iterdir():
                c_file = prof / "cookies.sqlite"
                if c_file.exists():
                    candidates.append(("Firefox (Windows)", c_file))

    return candidates


def _extract_via_browser_cookie3() -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Próbuje pobrać ciasteczka za pomocą browser_cookie3 z innych przeglądarek."""
    try:
        import browser_cookie3
    except ImportError:
        return None, []

    browsers = [
        ("Chrome", getattr(browser_cookie3, "chrome", None)),
        ("Brave", getattr(browser_cookie3, "brave", None)),
        ("Edge", getattr(browser_cookie3, "edge", None)),
        ("Chromium", getattr(browser_cookie3, "chromium", None)),
        ("Opera", getattr(browser_cookie3, "opera", None)),
    ]

    for b_name, b_fn in browsers:
        if not b_fn:
            continue
        try:
            cj = b_fn(domain_name=".facebook.com")
            fb_list = []
            for c in cj:
                if "facebook.com" in c.domain:
                    fb_list.append({
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain,
                        "path": c.path,
                        "secure": bool(c.secure),
                        "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
                    })
            if fb_list:
                names = {c["name"] for c in fb_list}
                if "c_user" in names or "xs" in names or "fr" in names:
                    return b_name, fb_list
        except Exception:
            continue

    return None, []


def grab_facebook_cookies(force_refresh: bool = False) -> Tuple[bool, str]:
    """
    Główna funkcja: automatycznie wykrywa i zapisuje ciasteczka Facebooka do facebook_cookies.json.
    Zwraca (sukces: bool, komunikat: str).
    """
    # Jeśli plik już istnieje i nie wymuszamy odświeżenia, sprawdź czy ma c_user
    if not force_refresh and COOKIES_OUT_FILE.exists():
        try:
            with open(COOKIES_OUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                names = {c.get("name") for c in data if isinstance(c, dict)}
                if "c_user" in names and "xs" in names:
                    return True, "Użyto istniejącego, poprawnego pliku facebook_cookies.json."
        except Exception:
            pass

    # 1. Przeszukaj bazy SQLite profilu Firefox
    ff_databases = _find_firefox_cookie_dbs()
    for b_name, db_path in ff_databases:
        cookies = _extract_from_firefox_sqlite(db_path)
        if cookies:
            names = {c["name"] for c in cookies}
            if "c_user" in names:
                with open(COOKIES_OUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False, indent=2)
                return True, f"Pomyślnie przechwycono ciasteczka z przeglądarki {b_name}!"

    # 2. Przeszukaj Chromium/Chrome/Brave/Edge przez browser_cookie3
    b_name, cookies = _extract_via_browser_cookie3()
    if cookies:
        with open(COOKIES_OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        return True, f"Pomyślnie przechwycono ciasteczka z przeglądarki {b_name}!"

    # 3. Jeśli nie znaleziono
    if COOKIES_OUT_FILE.exists():
        return True, "Użyto istniejącego pliku facebook_cookies.json (nie znaleziono nowszych w przeglądarce)."

    return False, (
        "Nie znaleziono aktywnej sesji Facebooka w żadnej przeglądarce (Firefox, Chrome, Brave, Edge).\n"
        "Otwórz przeglądarkę, zaloguj się na https://facebook.com i spróbuj ponownie."
    )


if __name__ == "__main__":
    success, msg = grab_facebook_cookies(force_refresh=True)
    print(f"[{'OK' if success else 'BŁĄD'}] {msg}")
    if success:
        print(f"Zapisano do: {COOKIES_OUT_FILE}")
