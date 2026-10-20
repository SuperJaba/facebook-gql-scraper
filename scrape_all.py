#!/usr/bin/env python3
"""
Facebook GQL Ultimate Profile Scraper & OSINT Diff Monitor
Kompleksowy, wielowątkowy skrypt do pobierania danych z Facebooka:
- Wielowątkowe przetwarzanie postów, komentarzy i mediów (ThreadPoolExecutor)
- Sprawdzanie historii i wykrywanie zmian (Diff)
- Pytanie o limity: 10, 25, 50, 100 i All
- Wybór prezentacji: Print na ekran vs Zapis do pliku
- Eksport do formatów: Excel (.xlsx), HTML (.html), PDF (.pdf), JSON (.json)
- Zapis historii w czytelnych folderach (np. 'zuck', 'dawid.mikolaj', 'Dawid_Mikolaj')
"""

import sys
import os
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

import facebook_gql_scraper as fgs
from facebook_gql_scraper.exceptions import (
    FacebookGqlScraperException,
    FacebookAccessException,
    FacebookNotFoundError,
    ProfileNotFoundError,
    PhotoNotAvailableException,
    ReelsNotAvailableException,
    FollowersNotAvailableException,
    FolloweesNotAvailableException,
    FriendFollowersNotAvailableException,
    FriendFolloweesNotAvailableException,
)

from history_manager import (
    get_profile_snapshots,
    save_snapshot,
    compute_diff,
    print_diff_report,
    get_profile_folder_name,
)
from exporter import export_excel, export_html, export_pdf, export_json
from cookie_grabber import grab_facebook_cookies

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent if BASE_DIR.name.lower() == "app" else BASE_DIR
OUTPUT_DIR = ROOT_DIR
COOKIES_FILE = BASE_DIR / "facebook_cookies.json" if (BASE_DIR / "facebook_cookies.json").exists() else ROOT_DIR / "facebook_cookies.json"
MAX_WORKERS = 6  # optymalna liczba równoległych wątków


def parse_target(raw_input: str) -> tuple[str, str]:
    """Rozpoznaje format wejściowy: URL, username lub numeryczne ID."""
    val = raw_input.strip()

    if val.startswith(("http://", "https://")):
        parsed = urlparse(val)
        if "profile.php" in parsed.path:
            qs = parse_qs(parsed.query)
            if "id" in qs and qs["id"]:
                return ("id", qs["id"][0])
        path_segments = [
            seg for seg in parsed.path.strip("/").split("/")
            if seg and seg not in ("people", "p", "profile")
        ]
        if path_segments:
            candidate = path_segments[0]
            if candidate.isdigit():
                return ("id", candidate)
            return ("username", candidate)

    if val.isdigit():
        return ("id", val)

    return ("username", val)


def safe_getattr(obj, attr, default=None):
    try:
        val = getattr(obj, attr, default)
        return val if val is not None else default
    except Exception:
        return default


def format_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def safe_profile_dict(p):
    if not p:
        return None
    return {
        "id": safe_getattr(p, "id"),
        "name": safe_getattr(p, "name"),
        "url": safe_getattr(p, "url"),
        "gender": safe_getattr(p, "gender"),
        "is_verified": safe_getattr(p, "is_verified"),
        "profile_picture_url": safe_getattr(p, "profile_picture_url"),
    }


def _process_single_post(post, fetch_comments=True, fetch_reactors=False, max_comments=30, max_reactors=50):
    """Przetwarza pojedynczy post (pobiera załączniki, komentarze, reakcje) - wywoływane w wątku."""
    post_obj = {
        "id": safe_getattr(post, "id"),
        "creation_time": safe_getattr(post, "creation_time"),
        "creation_time_formatted": format_ts(safe_getattr(post, "creation_time")),
        "url": safe_getattr(post, "url"),
        "title": safe_getattr(post, "title"),
        "text": safe_getattr(post, "text"),
        "reaction_count": safe_getattr(post, "reaction_count"),
        "comments_count": safe_getattr(post, "comments_count"),
        "share_count": safe_getattr(post, "share_count"),
        "top_reactions": safe_getattr(post, "top_reaction_breakdown") or [],
        "reactors_visible": safe_getattr(post, "reactors_visible"),
        "attachments": [],
        "comments": [],
        "reactors": [],
    }

    # Załączniki
    for att in safe_getattr(post, "attachments") or []:
        post_obj["attachments"].append({
            "id": getattr(att, "id", None),
            "image_url": getattr(att, "image_url", None),
            "url": getattr(att, "url", None),
        })

    # Komentarze
    if fetch_comments and post_obj["comments_count"] not in (0, "0", None):
        try:
            c_count = 0
            for comment in post.get_comments():
                c_count += 1
                c_owner = safe_profile_dict(getattr(comment, "owner", None))
                c_entry = {
                    "id": safe_getattr(comment, "id"),
                    "created_time": safe_getattr(comment, "created_time"),
                    "created_time_formatted": format_ts(safe_getattr(comment, "created_time")),
                    "text": safe_getattr(comment, "text"),
                    "reaction_count": safe_getattr(comment, "reaction_count"),
                    "owner": c_owner,
                    "replies": [],
                }
                # Pobierz odpowiedzi tylko jeśli komentarz je posiada
                if getattr(comment, "reply_count", 0) and getattr(comment, "reply_count", 0) > 0:
                    try:
                        for reply in getattr(comment, "get_replies", lambda: [])():
                            r_owner = safe_profile_dict(getattr(reply, "owner", None))
                            c_entry["replies"].append({
                                "id": safe_getattr(reply, "id"),
                                "created_time": safe_getattr(reply, "created_time"),
                                "created_time_formatted": format_ts(safe_getattr(reply, "created_time")),
                                "text": safe_getattr(reply, "text"),
                                "reaction_count": safe_getattr(reply, "reaction_count"),
                                "owner": r_owner,
                            })
                            if len(c_entry["replies"]) >= 5:
                                break
                    except Exception:
                        pass

                post_obj["comments"].append(c_entry)
                if c_count >= max_comments:
                    break
        except Exception:
            pass

    # Reaktory (osoby reagujące)
    if fetch_reactors and post_obj["reactors_visible"]:
        try:
            r_count = 0
            for reactor in post.get_reactors():
                r_count += 1
                post_obj["reactors"].append({
                    "reaction_type_id": getattr(reactor, "reaction_type_id", None),
                    "profile": safe_profile_dict(getattr(reactor, "profile", None)),
                })
                if r_count >= max_reactors:
                    break
        except Exception:
            pass

    return post_obj


def fetch_full_profile(ctx, user, limit_items=50, fetch_comments=True, fetch_reactors=False, 
                       fetch_photos=True, fetch_videos=True, fetch_network=True, verbose=True):
    """Pobiera zrzut profilu z wykorzystaniem wielowątkowości (ThreadPoolExecutor)."""
    t_start = time.time()
    if verbose:
        print("\n[1/4] 📥 Ekstrakcja danych tożsamości, bio i szczegółów profilu...")

    profile_data = {
        "scraper_version": "0.7.0",
        "scraped_at": datetime.now().isoformat(),
        "identity": {
            "id": safe_getattr(user, "id"),
            "name": safe_getattr(user, "name"),
            "alternate_name": safe_getattr(user, "alternate_name"),
            "gender": safe_getattr(user, "gender"),
            "url": safe_getattr(user, "url"),
            "is_verified": safe_getattr(user, "is_verified"),
            "profile_picture_url": safe_getattr(user, "profile_picture_url"),
            "status_text": safe_getattr(user, "status_text"),
            "nicknames": safe_getattr(user, "nicknames") or [],
            "name_pronunciation": safe_getattr(user, "name_pronunciation") or [],
        },
        "stats": {
            "followers_count": safe_getattr(user, "followers_count"),
            "followees_count": safe_getattr(user, "followees_count"),
            "friends_count": safe_getattr(user, "friends_count"),
        },
        "about": {
            "biography": safe_getattr(user, "biography"),
            "favorite_quotes": safe_getattr(user, "favorite_quotes"),
            "category": safe_getattr(user, "category"),
            "description": safe_getattr(user, "description"),
            "current_location": safe_getattr(user, "current_location"),
            "hometown": safe_getattr(user, "hometown"),
            "birthday": safe_getattr(user, "birthday"),
            "relationship": safe_getattr(user, "relationship"),
            "family": safe_getattr(user, "family") or [],
            "languages": safe_getattr(user, "language"),
            "links": safe_getattr(user, "links") or [],
            "works": safe_getattr(user, "works") or [],
            "colleges": safe_getattr(user, "colleges") or [],
            "high_schools": safe_getattr(user, "high_schools") or [],
            "hobbies": safe_getattr(user, "hobbies") or [],
            "interests": safe_getattr(user, "interests") or {},
            "travels": safe_getattr(user, "travels") or [],
            "contacts": safe_getattr(user, "contacts") or [],
            "business": {
                "rating": safe_getattr(user, "business_rating"),
                "address": safe_getattr(user, "business_address"),
                "hours": safe_getattr(user, "business_hours"),
            }
        },
        "posts": [],
        "photos": [],
        "videos": [],
        "network": {
            "friends": [],
            "followers": [],
            "followees": [],
            "friends_followers": [],
            "friends_followees": [],
            "restrictions": [],
        }
    }

    # Pobieranie postów
    limit_desc = f"{limit_items}" if limit_items is not None else "wszystkie (ALL)"
    if verbose:
        print(f"\n[2/4] ⚡ Pobieranie postów w strumieniu (limit: {limit_desc})...")

    raw_posts = []
    seen_post_ids = set()
    consecutive_repeats = 0
    try:
        for p in user.get_posts():
            p_id = safe_getattr(p, "id")
            if p_id:
                if p_id in seen_post_ids:
                    consecutive_repeats += 1
                    if consecutive_repeats >= 3:
                        if verbose:
                            print(f"  ℹ️ Osiągnięto koniec unikalnych postów (oś czasu zapętliła się).")
                        break
                    continue
                else:
                    consecutive_repeats = 0
                    seen_post_ids.add(p_id)

            raw_posts.append(p)
            if verbose and len(raw_posts) % 10 == 0:
                print(f"  ↳ Odebrano nagłówki {len(raw_posts)} postów...")
            if limit_items is not None and len(raw_posts) >= limit_items:
                break
    except KeyboardInterrupt:
        if verbose:
            print(f"\n⚠️ Przerwano pobieranie nagłówków (Ctrl+C). Przejście do przetwarzania {len(raw_posts)} postów...")
    except Exception as pe:
        if verbose:
            print(f"⚠️ Zakończono pobieranie strumienia postów: {pe}")

    if verbose:
        print(f"  ✓ Zakończono zbieranie nagłówków: łącznie {len(raw_posts)} postów.")

    if raw_posts:
        if verbose:
            print(f"  ⚡ Przetwarzanie {len(raw_posts)} postów i komentarzy wielowątkowo ({MAX_WORKERS} wątków)...")

        max_c = 15 if len(raw_posts) > 100 else (20 if (limit_items and limit_items <= 25) else 30)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_post = {
                executor.submit(_process_single_post, p, fetch_comments, fetch_reactors, max_c): p 
                for p in raw_posts
            }
            try:
                for future in as_completed(future_to_post):
                    try:
                        res = future.result()
                        profile_data["posts"].append(res)
                        if verbose:
                            txt_prev = (res['text'] or '').replace('\n', ' ')[:50]
                            print(f"  ✅ Post [{res['id']}] 👍 {res['reaction_count']} | 💬 {res['comments_count']} — \"{txt_prev}...\"")
                    except Exception:
                        pass
            except KeyboardInterrupt:
                if verbose:
                    print(f"\n⚠️ Przerwano przetwarzanie wątków (Ctrl+C). Kontynuowanie z pobranymi danymi...")

    # Sortowanie postów według czasu utworzenia (najnowsze pierwsze)
    profile_data["posts"].sort(key=lambda x: str(x.get("creation_time") or ""), reverse=True)

    # Multimedia i Relacje (Równolegle w wątkach pobocznych!)
    if verbose:
        print("\n[3/4] ⚡ Równoległe pobieranie multimediów i relacji...")

    def _fetch_photos():
        if not fetch_photos: return []
        items = []
        try:
            for ph in user.get_photos():
                items.append({
                    "id": getattr(ph, "id", None),
                    "image_url": getattr(ph, "image_url", None),
                    "url": getattr(ph, "url", None),
                })
                if limit_items is not None and len(items) >= limit_items: break
        except PhotoNotAvailableException:
            profile_data["network"]["restrictions"].append("photos")
        except Exception: pass
        return items

    def _fetch_videos():
        if not fetch_videos: return []
        items = []
        try:
            for v in user.get_videos():
                items.append({
                    "id": getattr(v, "id", None),
                    "length_in_second": getattr(v, "length_in_second", None),
                    "created_time": getattr(v, "created_time", None),
                    "play_count": getattr(v, "play_count", None),
                    "video_url": getattr(v, "video_url", None),
                })
                if limit_items is not None and len(items) >= limit_items: break
        except ReelsNotAvailableException:
            profile_data["network"]["restrictions"].append("videos")
        except Exception: pass
        return items

    def _fetch_friends():
        if not fetch_network: return []
        items = []
        try:
            for p in getattr(user, "get_friends", lambda: [])():
                items.append(safe_profile_dict(p))
                if limit_items is not None and len(items) >= limit_items: break
        except Exception: pass
        return items

    def _fetch_followers():
        if not fetch_network: return []
        items = []
        try:
            for p in user.get_followers():
                items.append(safe_profile_dict(p))
                if limit_items is not None and len(items) >= limit_items: break
        except FollowersNotAvailableException:
            profile_data["network"]["restrictions"].append("followers")
        except Exception: pass
        return items

    def _fetch_followees():
        if not fetch_network: return []
        items = []
        try:
            for p in user.get_followees():
                items.append(safe_profile_dict(p))
                if limit_items is not None and len(items) >= limit_items: break
        except FolloweesNotAvailableException:
            profile_data["network"]["restrictions"].append("followees")
        except Exception: pass
        return items

    with ThreadPoolExecutor(max_workers=5) as executor:
        f_photos = executor.submit(_fetch_photos)
        f_videos = executor.submit(_fetch_videos)
        f_friends = executor.submit(_fetch_friends)
        f_followers = executor.submit(_fetch_followers)
        f_followees = executor.submit(_fetch_followees)

        profile_data["photos"] = f_photos.result()
        profile_data["videos"] = f_videos.result()
        profile_data["network"]["friends"] = f_friends.result()
        profile_data["network"]["followers"] = f_followers.result()
        profile_data["network"]["followees"] = f_followees.result()

    if verbose:
        print(f"  📷 Zdjęcia: {len(profile_data['photos'])} | 🎬 Wideo: {len(profile_data['videos'])}")
        print(f"  🤝 Znajomi: {len(profile_data['network']['friends'])} | 👥 Obserwujący: {len(profile_data['network']['followers'])} | 👤 Obserwowani: {len(profile_data['network']['followees'])}")
        elapsed = time.time() - t_start
        print(f"\n[4/4] 🚀 Gotowe! Całość pobrano w zaledwie {elapsed:.1f} sekund!")

    return profile_data


def print_profile_summary(data: dict):
    """Wyświetla zwięzłe i czytelne podsumowanie profilu w terminalu."""
    ident = data.get("identity") or data
    stats = data.get("stats") or data.get("counts") or {}
    about = data.get("about") or {}
    posts = data.get("posts") or []

    print("\n" + "=" * 70)
    print(f"👤 PROFIL: {ident.get('name')} (ID: {ident.get('id')})")
    print(f"🔗 Link:   {ident.get('url')}")
    print(f"👥 Obserwujący: {stats.get('followers_count') or stats.get('followers') or 'Brak/Ukryte'}")
    print(f"🤝 Znajomi:     {stats.get('friends_count') or stats.get('friends') or 'Brak/Ukryte'}")
    if about.get("relationship", {}).get("type"):
        print(f"❤️ Związek:     {about.get('relationship', {}).get('type','')} {about.get('relationship', {}).get('date','') or ''}")
    if about.get("birthday"):
        print(f"🎂 Urodziny:    {about.get('birthday')}")
    if about.get("biography"):
        print(f"📝 Bio:         {about.get('biography')}")
    if about.get("favorite_quotes"):
        print(f"💬 Cytaty:      {about.get('favorite_quotes')[:100]}...")
    if about.get("current_location") or about.get("hometown"):
        print(f"📍 Lokalizacja: {about.get('current_location') or 'Brak'} | Pochodzenie: {about.get('hometown') or 'Brak'}")
    if about.get("family"):
        fam_list = [f"{f.get('name','')}: {f.get('type','')}" for f in about.get("family", [])]
        print(f"👨‍👩‍👧 Rodzina:     {', '.join(fam_list)}")
    if about.get("works"):
        w_list = [f"{w.get('company','')} ({w.get('desc','')})" for w in about.get("works", [])]
        print(f"💼 Praca:       {', '.join(w_list)}")
    if about.get("colleges"):
        print(f"🎓 Uczelnie:    {', '.join([c.get('name','') for c in about.get('colleges', [])])}")
    if about.get("high_schools"):
        print(f"🏫 Szkoły:      {', '.join([h.get('name','') for h in about.get('high_schools', [])])}")

    print(f"\n📄 Liczba pobranych postów: {len(posts)}")
    for i, p in enumerate(posts[:10], start=1):
        txt = (p.get("text") or "").replace("\n", " ")[:65]
        print(f"  [{i}] {p.get('creation_time_formatted') or ''} 👍 {p.get('reaction_count') or 0} | 💬 {p.get('comments_count') or 0} — \"{txt}...\"")
    if len(posts) > 10:
        print(f"  ... oraz {len(posts) - 10} kolejnych postów.")
    print("=" * 70 + "\n")


def prompt_limits() -> tuple[int | None, str]:
    """Pyta użytkownika o limit pobierania: 10, 25, 50, 100 lub All."""
    print("\n" + "─" * 60)
    print("Wybierz limit pobieranych elementów (posty, multimedia, kontakty):")
    print("  [1] 10  elementów (bardzo szybki skan)")
    print("  [2] 25  elementów")
    print("  [3] 50  elementów (rekomendowany)")
    print("  [4] 100 elementów (dokładny skan)")
    print("  [5] ALL (bez limitu — pobierz wszystko co się da)")
    
    choice = input("👉 Wybierz limit [10 / 25 / 50 / 100 / ALL, domyślnie 50]: ").strip().lower()
    
    if choice in ("1", "10"):
        return 10, "10"
    elif choice in ("2", "25"):
        return 25, "25"
    elif choice in ("4", "100"):
        return 100, "100"
    elif choice in ("5", "all", "a", "wszystko"):
        return None, "ALL"
    else:
        return 50, "50"


def main():
    print("=" * 72)
    print("   Facebook GraphQL Scraper — Ultimate OSINT Profile & Diff Tool   ")
    print("=" * 72)

    # 1. Automatyczne przechwytywanie ciasteczek z przeglądarki
    print("🍪 Sprawdzanie ciasteczek sesyjnych...")
    has_cookies, cookie_msg = grab_facebook_cookies(force_refresh=False)
    if has_cookies:
        print(f"✅ {cookie_msg}")
    else:
        print(f"❌ {cookie_msg}")
        print("\nUpewnij się, że jesteś zalogowany na Facebooku w przeglądarce (Firefox, Chrome, Brave, Edge).")
        sys.exit(1)

    # 2. Pytanie o cel (ID, URL lub username)
    is_forensic_cli = any(arg in sys.argv for arg in ("--forensic", "--forensics", "-f"))
    non_flag_args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if non_flag_args:
        raw_target = non_flag_args[0]
    else:
        raw_target = input("\n👉 Podaj ID, nazwę użytkownika lub link URL do profilu: ").strip()

    if not raw_target:
        print("❌ Nie podano celu. Zakończono.")
        sys.exit(1)

    target_type, target_val = parse_target(raw_target)
    print(f"🎯 Rozpoznano: [{target_type.upper()}] {target_val}")

    # Wybór trybu działania: Standard OSINT vs Cyfrowa Kryminalistyka (Forensics)
    if is_forensic_cli:
        mode_choice = "2"
    else:
        print("\nWybierz tryb działania:")
        print("  [1] ⚡ Profil OSINT (posty, multimedia, historia, eksport do HTML/Excel/PDF/JSON)")
        print("  [2] 🕵️ Cyfrowa Kryminalistyka (DFIR Forensics - SHA-256, łańcuch dowodowy, sociogram, pattern of life)")
        mode_choice = input("👉 Wybór [1/2, domyślnie 1]: ").strip() or "1"

    if mode_choice == "2":
        try:
            from facebook_forensics import run_forensic_acquisition
            f_limit_in = input("👉 Limit postów do analizy śledczej [np. 10, 25, 50, all, domyślnie 50]: ").strip() or "50"
            f_limit = None if f_limit_in.lower() == "all" else int(f_limit_in)
            ex_name = input("👉 Nazwisko/ID śledczego (Examiner) [domyślnie Examiner]: ").strip() or "Examiner"
            run_forensic_acquisition(
                target_str=raw_target,
                examiner_name=ex_name,
                post_limit=f_limit,
                fetch_comments=True,
                fetch_reactors=True,
                download_media=True,
                verbose=True
            )
            return
        except Exception as fe:
            print(f"❌ Błąd modułu śledczego: {fe}")
            return

    # 2. Sprawdzenie historii wcześniejszych zrzutów
    existing_snapshots = get_profile_snapshots(
        user_id=target_val if target_type == "id" else None,
        username=target_val if target_type == "username" else None
    )

    action_mode = "full"  # domyślnie pełne pobranie

    if existing_snapshots:
        print("\n" + "─" * 60)
        print(f"🔔 Znaleziono historię wcześniejszych zrzutów dla tego profilu!")
        print(f"   Znalezione archiwa ({len(existing_snapshots)}):")
        for i, snap in enumerate(existing_snapshots[:3], start=1):
            snap_time = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"     [{i}] {snap.parent.name}/{snap.name} ({snap_time})")

        print("\nWybierz akcję:")
        print("  [1] Pobierz całość od nowa (zaktualizuj historię i pobierz nowe dane)")
        print("  [2] Pokaż zmiany (porównaj obecny stan ze stanem z ostatniego zrzutu)")

        choice_act = input("👉 Wybór [1/2, domyślnie 1]: ").strip() or "1"
        if choice_act == "2":
            action_mode = "diff"
        print("─" * 60)

    # 3. Pytanie o limity (10, 25, 50, 100, All)
    items_limit, limit_label = prompt_limits()
    print(f"⚙️ Ustawiono limit: {limit_label} elementów")

    # Pytanie o pobieranie komentarzy
    com_in = input("👉 Czy pobierać komentarze do każdego posta? [T/n, domyślnie T]: ").strip().lower()
    fetch_comments_choice = com_in not in ("n", "no", "nie")

    # 4. Menu prezentacji wyników (Print vs Zapis do pliku)
    print("\nWybierz sposób prezentacji wyników:")
    print("  [1] Tylko wyświetl na ekranie (Terminal print)")
    print("  [2] Zapisz do pliku")
    print("  [3] Wyświetl na ekranie oraz zapisz do pliku")

    pres_choice = input("👉 Wybór [1/2/3, domyślnie 3]: ").strip() or "3"
    do_print = pres_choice in ("1", "3")
    do_save = pres_choice in ("2", "3")

    file_format = "html"
    custom_name = ""

    if do_save:
        print("\nWybierz format pliku:")
        print("  [1] HTML (nowoczesny interaktywny dashboard)")
        print("  [2] Excel / XLSX (wieloarkuszowy skoroszyt arkusza kalkulacyjnego)")
        print("  [3] PDF (dokument z tabelami i podsumowaniem)")
        print("  [4] JSON (surowe dane)")
        print("  [5] Wszystkie powyższe (ALL)")

        fmt_in = input("👉 Format [1/2/3/4/5 lub html/excel/pdf/json/all, domyślnie 1]: ").strip().lower() or "1"
        fmt_map = {"1": "html", "2": "excel", "3": "pdf", "4": "json", "5": "all"}
        file_format = fmt_map.get(fmt_in, fmt_in if fmt_in in ("html", "excel", "pdf", "json", "all") else "html")

        custom_name = input("👉 Podaj własną nazwę pliku (pozostaw puste dla nazwy automatycznej): ").strip()

    # Inicjalizacja sesji z Facebookiem
    print("\n⏳ Łączenie z Facebook GraphQL API...")
    ctx = fgs.FacebookContext(cookies_file=str(COOKIES_FILE), quiet=True)

    try:
        if target_type == "id":
            user = fgs.Profile.from_id(ctx, id=target_val)
        else:
            try:
                user = fgs.Profile.from_username(ctx, username=target_val)
            except Exception:
                if target_val.isdigit():
                    user = fgs.Profile.from_id(ctx, id=target_val)
                else:
                    raise
    except Exception as e:
        print(f"❌ Nie udało się odnaleźć profilu: {e}")
        sys.exit(1)

    print(f"✅ Profil znaleziony: {user.name} (ID: {user.id})")

    # Tryb 2: DIFF (Pokaż zmiany)
    if action_mode == "diff":
        latest_snap_path = existing_snapshots[0]
        print(f"\n📖 Wczytywanie poprzedniego zrzutu: {latest_snap_path.parent.name}/{latest_snap_path.name}")
        with open(latest_snap_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)

        print(f"🔄 Pobieranie aktualnego stanu z Facebooka (wielowątkowo, limit: {items_limit or 'ALL'})...")
        current_data = fetch_full_profile(ctx, user, limit_items=items_limit, fetch_comments=fetch_comments_choice, verbose=True)

        # Oblicz różnice
        diff = compute_diff(old_data, current_data)

        if do_print:
            print_diff_report(diff)

        # Zapis nowego zrzutu do czytelnego folderu profilu
        saved_snap = save_snapshot(current_data)
        folder_display = get_profile_folder_name(current_data)
        print(f"💾 Nowy zrzut zaktualizowany w historii: profiles_history/{folder_display}/{saved_snap.name}")

        if do_save:
            base_filename = custom_name or f"diff_{folder_display}_{user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            _handle_exports(current_data, base_filename, file_format)

        return

    # Tryb 1: FULL (Pobierz całość)
    data = fetch_full_profile(
        ctx, user,
        limit_items=items_limit,
        fetch_comments=fetch_comments_choice,
        fetch_reactors=False,  # Reakcje liczbowe są już w post.reaction_count / breakdown
        fetch_photos=True,
        fetch_videos=True,
        fetch_network=True,
        verbose=True
    )

    # Zapis w czytelnym folderze w profiles_history
    saved_snap = save_snapshot(data)
    folder_display = get_profile_folder_name(data)
    print(f"\n💾 Zrzut zapisany w bazie historii: profiles_history/{folder_display}/{saved_snap.name}")

    # Prezentacja na ekranie
    if do_print:
        print_profile_summary(data)

    # Zapis do pliku
    if do_save:
        base_filename = custom_name or f"fb_{folder_display}_{user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        _handle_exports(data, base_filename, file_format)


def _handle_exports(data: dict, base_filename: str, file_format: str):
    """Zarządza eksportem do wybranych formatów (HTML, Excel, PDF, JSON)."""
    clean_base = re.sub(r"\.(xlsx|html|pdf|json)$", "", base_filename, flags=re.IGNORECASE)

    print("\n💾 Eksportowanie wyników:")
    formats = ["html", "excel", "pdf", "json"] if file_format == "all" else [file_format]

    for fmt in formats:
        if fmt == "html":
            out_file = OUTPUT_DIR / f"{clean_base}.html"
            export_html(data, out_file)
            print(f"  🌐 [HTML Dashboard]: {out_file.name}")
        elif fmt in ("excel", "xlsx"):
            out_file = OUTPUT_DIR / f"{clean_base}.xlsx"
            export_excel(data, out_file)
            print(f"  📊 [Excel Skoroszyt]: {out_file.name}")
        elif fmt == "pdf":
            out_file = OUTPUT_DIR / f"{clean_base}.pdf"
            export_pdf(data, out_file)
            print(f"  📕 [PDF Raport]:     {out_file.name}")
        elif fmt == "json":
            out_file = OUTPUT_DIR / f"{clean_base}.json"
            export_json(data, out_file)
            print(f"  📄 [JSON Surowy]:    {out_file.name}")

    print(f"\n📂 Pliki zostały pomyślnie zapisane w folderze:\n   {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
