#!/usr/bin/env python3
"""
Facebook Digital Forensics & Evidence Preservation Suite
=======================================================
Zaawansowane narzędzie do cyfrowej informatyki śledczej (DFIR / OSINT) dla platformy Facebook:
1. Akwizycja tożsamości, pełnego biogramu, cytatów, historii edukacji, pracy, rodziny, kontaktów.
2. Pełny graf powiązań: znajomi, obserwujący, obserwowani.
3. Rekonstrukcja osi czasu: posty, timestampy UNIX epoch, permalinki, metryki zaangażowania.
4. Dogłębne badanie interakcji: komentarze (wraz z pod-odpowiedziami do głębokości 2) oraz reaktory (kto i jak zareagował).
5. Sociogram powiązań (Associate Matrix): wykrywanie najczęstszych interaktorów celu, ranking zaangażowania.
6. Analiza behawioralna (Pattern of Life): rozkład aktywności w cyklu 24h (doba) i 7 dni tygodnia.
7. Zabezpieczenie multimediów (Media Vault): lokalne pobranie zdjęć, miniatur i awatarów z Facebook CDN.
8. Integralność dowodowa: sumy kontrolne SHA-256 i MD5 każdego artefaktu, nagłówki ETag z CDN.
9. Oficjalny raport śledczy HTML (Forensic Report) + Manifest Dowodowy (evidence_manifest.json) + autonomiczny skrypt weryfikacji.
"""

import sys
import os
import json
import csv
import hashlib
import time
import socket
import platform
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Zapewnienie natychmiastowego wyswietlania kazdej linii w terminalu (brak buforowania)
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

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
)

try:
    from cookie_grabber import grab_facebook_cookies
except Exception:
    grab_facebook_cookies = None

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent if BASE_DIR.name.lower() == "app" else BASE_DIR
COOKIES_FILE = BASE_DIR / "facebook_cookies.json" if (BASE_DIR / "facebook_cookies.json").exists() else ROOT_DIR / "facebook_cookies.json"
MAX_WORKERS = 8

REACTION_MAP = {
    "1635855486666999": ("Like", "👍 Lubię to"),
    "940715332675988": ("Love", "❤️ Super"),
    "1678524932434102": ("Love", "❤️ Super"),
    "227877478001712": ("Care", "🤗 Trzymaj się"),
    "1444140082405626": ("Care", "🤗 Trzymaj się"),
    "941836445999081": ("Haha", "😆 Ha ha"),
    "1103986926362873": ("Wow", "😮 Wow"),
    "953360548123281": ("Sad", "😢 Przykro mi"),
    "937964426315277": ("Angry", "😡 Wrr"),
    "478547315650144": ("Angry", "😡 Wrr"),
}


def calc_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def calc_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def calc_file_hashes(filepath: Path) -> tuple[str, str, int]:
    """Zwraca (sha256, md5, size_bytes) dla wskazanego pliku."""
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha256.hexdigest(), md5.hexdigest(), size


def format_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def format_ts_utc(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


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


def get_c_user_from_cookies(cookies_path: Path) -> str:
    """Odczytuje ID zalogowanego śledczego/użytkownika (c_user) z pliku ciasteczek."""
    if not cookies_path.exists():
        return "Unknown"
    try:
        with open(cookies_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for c in data:
                if c.get("name") == "c_user":
                    return str(c.get("value"))
        elif isinstance(data, dict):
            return str(data.get("c_user", "Unknown"))
    except Exception:
        pass
    return "Unknown"


class MediaVault:
    """Zarządza pobieraniem multimediów, ich hashowaniem i zachowaniem łańcucha dowodowego."""

    def __init__(self, media_dir: Path):
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_entries = []
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })

    def download_artifact(self, url: str, filename: str, artifact_type: str) -> dict | None:
        if not url or not url.startswith("http"):
            return None
        dest_path = self.media_dir / filename
        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200:
                content = resp.content
                with open(dest_path, "wb") as f:
                    f.write(content)

                sha256 = calc_sha256(content)
                md5 = calc_md5(content)
                entry = {
                    "filename": filename,
                    "rel_path": f"media/{filename}",
                    "artifact_type": artifact_type,
                    "size_bytes": len(content),
                    "sha256": sha256,
                    "md5": md5,
                    "source_url": url,
                    "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
                    "http_etag": resp.headers.get("ETag"),
                    "http_content_type": resp.headers.get("Content-Type"),
                    "http_last_modified": resp.headers.get("Last-Modified"),
                }
                self.manifest_entries.append(entry)
                return entry
        except Exception as e:
            # Nie blokuj śledztwa w przypadku problemów z pojedynczym CDN
            pass
        return None


def run_forensic_acquisition(target_str: str, examiner_name: str = "Examiner", case_id: str = None,
                             post_limit: int | None = 50,
                             friends_limit: int | None = None,
                             photos_limit: int | None = None,
                             fetch_comments: bool = True,
                             fetch_reactors: bool = True, download_media: bool = True,
                             output_vault_dir: Path = None, verbose: bool = True) -> dict:
    """
    Główny proces pełnej akwizycji kryminalistycznej Facebooka.
    """
    t_start = time.time()
    start_utc = datetime.now(timezone.utc).isoformat()
    start_local = datetime.now().isoformat()

    if not case_id:
        case_id = f"FB-CASE-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    if output_vault_dir is None:
        output_vault_dir = ROOT_DIR / "evidence_vault"
    output_vault_dir.mkdir(parents=True, exist_ok=True)

    # 1. Sprawdzenie i automatyczne pozyskanie ciasteczek (logowanie do Facebooka)
    if grab_facebook_cookies:
        has_c, c_msg = grab_facebook_cookies(force_refresh=False)
        if verbose:
            print(f"[*] Sprawdzanie sesji i logowania: {c_msg}")
        if not has_c and not COOKIES_FILE.exists():
            raise FileNotFoundError("Nie wykryto aktywnej sesji Facebooka. Zaloguj się w przeglądarce (Chrome/Edge/Firefox) lub utwórz facebook_cookies.json.")

    c_user = get_c_user_from_cookies(COOKIES_FILE)

    if verbose:
        print("\n" + "█" * 78)
        print("   🔍 FACEBOOK DIGITAL FORENSICS & EVIDENCE PRESERVATION SUITE")
        print("█" * 78)
        print(f"  📁 Nr Sprawy (Case ID)  : {case_id}")
        print(f"  👮 Śledczy (Examiner)   : {examiner_name}")
        print(f"  🔑 Sesja Facebook (c_user): {c_user}")
        print(f"  💻 Host Akwizycji       : {socket.gethostname()} ({platform.system()} {platform.release()})")
        print(f"  ⏰ Czas Rozpoczęcia     : {start_local} (UTC: {start_utc})")
        print(f"  🎯 Cel (Target)         : {target_str}")
        print("█" * 78 + "\n")

    # Inicjalizacja kontekstu (quiet=True wycisza surowy debug GraphQL, zastepujac go czytelnymi komunikatami sledczymi)
    if not COOKIES_FILE.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku cookies w ścieżce: {COOKIES_FILE}")

    ctx = fgs.FacebookContext(cookies_file=str(COOKIES_FILE), quiet=True)
    target_type, target_val = parse_target(target_str)

    if verbose:
        print(f"[*] Rozpoznano format celu: {target_type.upper()} = '{target_val}'")
        print("[*] Pobieranie początkowych metadanych i zakładek profilu...")

    if target_type == "id":
        user = fgs.Profile.from_id(ctx, id=target_val)
    else:
        user = fgs.Profile.from_username(ctx, username=target_val)

    user_id = safe_getattr(user, "id") or target_val
    user_name = safe_getattr(user, "name") or "Unknown_User"
    clean_name = "".join(c for c in user_name if c.isalnum() or c in ("-", "_")).strip() or "target"

    # Przygotowanie dedykowanego folderu sprawy
    case_folder_name = f"{case_id}_{clean_name}_{user_id}"
    case_dir = output_vault_dir / case_folder_name
    case_dir.mkdir(parents=True, exist_ok=True)
    media_dir = case_dir / "media"
    vault = MediaVault(media_dir)

    log_file = case_dir / "acquisition_log.txt"

    def log_event(msg):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {msg}"
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(line + "\n")

    log_event(f"START CASE {case_id} by {examiner_name} on host {socket.gethostname()}")
    log_event(f"Target: {target_str} (Resolved: {target_type}={target_val}, ID: {user_id}, Name: {user_name})")
    log_event(f"Examiner Facebook Session c_user: {c_user}")

    if verbose:
        print(f"[+] Utworzono skarbiec dowodowy: {case_dir}")
        print(f"[+] Otwarto dziennik zdarzeń śledczych: {log_file.name}")

    # 1. Ekstrakcja danych tożsamości i profilu (Dossier)
    if verbose:
        print("\n[FAZA 1/6] 📋 Ekstrakcja kartoteki tożsamości, bio, rodziny i powiązań...")

    dossier = {
        "identity": {
            "id": user_id,
            "name": user_name,
            "alternate_name": safe_getattr(user, "alternate_name"),
            "gender": safe_getattr(user, "gender"),
            "url": safe_getattr(user, "url"),
            "is_verified": safe_getattr(user, "is_verified"),
            "status_text": safe_getattr(user, "status_text"),
            "profile_picture_url": safe_getattr(user, "profile_picture_url"),
            "nicknames": safe_getattr(user, "nicknames") or [],
            "name_pronunciation": safe_getattr(user, "name_pronunciation") or [],
        },
        "stats": {
            "followers_count": safe_getattr(user, "followers_count"),
            "following_count": safe_getattr(user, "following_count"),
            "friends_count": safe_getattr(user, "friends_count"),
        },
        "background": {
            "biography": safe_getattr(user, "biography"),
            "favorite_quotes": safe_getattr(user, "favorite_quotes"),
            "category": safe_getattr(user, "category"),
            "description": safe_getattr(user, "description"),
            "current_location": safe_getattr(user, "current_location"),
            "hometown": safe_getattr(user, "hometown"),
            "birthday": safe_getattr(user, "birthday"),
            "relationship": safe_getattr(user, "relationship"),
            "family": safe_getattr(user, "family") or [],
            "languages": safe_getattr(user, "language") or [],
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
        }
    }

    # Pobranie i zabezpieczenie awatara
    avatar_url = dossier["identity"]["profile_picture_url"]
    if download_media and avatar_url:
        if verbose:
            print("  ↳ Pobieranie i zabezpieczanie zdjęcia profilowego celu...")
        down_av = vault.download_artifact(avatar_url, f"avatar_{user_id}.jpg", "PROFILE_PICTURE")
        if verbose and down_av:
            print(f"  ✓ Zabezpieczono awatar: media/{down_av['filename']} (SHA-256: {down_av['sha256'][:16]}...)")

    # 2. Pełny graf znajomych i obserwujących
    if verbose:
        print("\n[FAZA 2/6] 🤝 Zabezpieczanie siatki społecznej (Znajomi, Obserwujący, Obserwowani)...")

    friends_list = []
    followers_list = []
    followees_list = []

    def _fetch_all_friends():
        res = []
        try:
            if verbose:
                print("  ↳ [Znajomi] Pobieranie pełnej listy znajomych celu z GraphQL...")
            for fr in getattr(user, "get_friends", lambda: [])():
                p_dict = safe_profile_dict(fr)
                if p_dict:
                    res.append(p_dict)
                    if verbose and len(res) % 25 == 0:
                        print(f"     • Zabezpieczono {len(res)} znajomych (ostatnio: {p_dict.get('name')})...")
                if friends_limit is not None and len(res) >= friends_limit:
                    break
            if verbose:
                print(f"  ✓ Zakończono pobieranie znajomych (łącznie: {len(res)} kontaktów)")
        except Exception as e:
            if verbose:
                print(f"  ℹ️ [Znajomi] Lista znajomych jest ukryta lub niedostępna ({e}).")
        return res

    def _fetch_all_followers():
        res = []
        try:
            if verbose:
                print("  ↳ [Obserwujący] Pobieranie listy obserwujących...")
            for fl in user.get_followers():
                p_dict = safe_profile_dict(fl)
                if p_dict:
                    res.append(p_dict)
                    if verbose and len(res) % 25 == 0:
                        print(f"     • Zabezpieczono {len(res)} obserwujących...")
                if friends_limit is not None and len(res) >= friends_limit:
                    break
            if verbose:
                print(f"  ✓ Zakończono pobieranie obserwujących: {len(res)} profili")
        except Exception:
            if verbose:
                print(f"  ℹ️ [Obserwujący] Lista obserwujących jest ukryta lub niedostępna.")
        return res

    def _fetch_all_followees():
        res = []
        try:
            if verbose:
                print("  ↳ [Obserwowani] Pobieranie listy obserwowanych profili...")
            for fe in user.get_followees():
                p_dict = safe_profile_dict(fe)
                if p_dict:
                    res.append(p_dict)
                    if verbose and len(res) % 25 == 0:
                        print(f"     • Zabezpieczono {len(res)} obserwowanych...")
                if friends_limit is not None and len(res) >= friends_limit:
                    break
            if verbose:
                print(f"  ✓ Zakończono pobieranie obserwowanych: {len(res)} profili")
        except Exception:
            if verbose:
                print(f"  ℹ️ [Obserwowani] Lista obserwowanych jest ukryta lub niedostępna.")
        return res

    with ThreadPoolExecutor(max_workers=3) as executor:
        f_fr = executor.submit(_fetch_all_friends)
        f_fol = executor.submit(_fetch_all_followers)
        f_fee = executor.submit(_fetch_all_followees)
        friends_list = f_fr.result()
        followers_list = f_fol.result()
        followees_list = f_fee.result()

    known_friend_ids = {f["id"] for f in friends_list if f and f.get("id")}

    # 3. Zabezpieczanie multimediów (Zdjęcia i Wideo/Reels)
    if verbose:
        print("\n[FAZA 3/6] 🖼️ Zabezpieczanie katalogu multimediów (Zdjęcia i Rolki/Wideo)...")
        print("  ↳ Pobieranie katalogu zdjęć użytkownika...")

    photos_catalog = []
    videos_catalog = []

    try:
        for ph in user.get_photos():
            ph_id = getattr(ph, "id", None)
            img_url = getattr(ph, "image_url", None)
            photos_catalog.append({
                "id": ph_id,
                "image_url": img_url,
                "url": getattr(ph, "url", None),
            })
            if download_media and img_url:
                if photos_limit is None or len(photos_catalog) <= photos_limit:
                    down_res = vault.download_artifact(img_url, f"photo_{ph_id or len(photos_catalog)}.jpg", "PROFILE_PHOTO")
                    if verbose and down_res and len(photos_catalog) % 10 == 0:
                        print(f"     • Zabezpieczono {len(photos_catalog)} zdjęć (SHA-256: {down_res['sha256'][:12]}...)...")
            if photos_limit is not None and len(photos_catalog) >= photos_limit:
                break
        if verbose:
            print(f"  ✓ Zabezpieczono łącznie zdjęć w rejestrze: {len(photos_catalog)}")
    except Exception:
        if verbose:
            print("  ℹ️ Zdjęcia profilowe są ukryte lub niedostępne.")

    try:
        if verbose:
            print("  ↳ Pobieranie katalogu wideo i rolek użytkownika...")
        for vd in user.get_videos():
            v_id = getattr(vd, "id", None)
            thumb = getattr(vd, "thumbnail_image_url", None) or getattr(vd, "fallback_image_url", None)
            videos_catalog.append({
                "id": v_id,
                "length_in_second": getattr(vd, "length_in_second", None),
                "created_time": getattr(vd, "created_time", None),
                "created_time_formatted": format_ts(getattr(vd, "created_time", None)),
                "play_count": getattr(vd, "play_count", None),
                "video_url": getattr(vd, "video_url", None),
                "thumbnail_url": thumb,
            })
            if download_media and thumb:
                if photos_limit is None or len(videos_catalog) <= 50:
                    vault.download_artifact(thumb, f"video_thumb_{v_id or len(videos_catalog)}.jpg", "VIDEO_THUMBNAIL")
            if photos_limit is not None and len(videos_catalog) >= photos_limit:
                break
        if verbose:
            print(f"  ✓ Zabezpieczono łącznie nagrań wideo w rejestrze: {len(videos_catalog)}")
    except Exception:
        if verbose:
            print("  ℹ️ Nagrania wideo są ukryte lub niedostępne.")

    # 4. Rekonstrukcja osi czasu, komentarzy i reakcji
    if verbose:
        limit_desc = f"{post_limit}" if post_limit is not None else "wszystkie (ALL)"
        print(f"\n[FAZA 4/6] 📜 Rekonstrukcja osi czasu i badanie interakcji (limit: {limit_desc})...")

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
                            print("  ℹ️ Osiągnięto zapętlenie osi czasu (koniec nowych wpisów).")
                        break
                    continue
                else:
                    consecutive_repeats = 0
                    seen_post_ids.add(p_id)

            raw_posts.append(p)
            if verbose and len(raw_posts) % 10 == 0:
                print(f"  ↳ Zarejestrowano nagłówki {len(raw_posts)} postów...")
            if post_limit is not None and len(raw_posts) >= post_limit:
                break
    except KeyboardInterrupt:
        if verbose:
            print("\n  ⚠️ Przerwano zbieranie nagłówków postów (Ctrl+C). Przejście do analizy...")
    except Exception as e:
        if verbose:
            print(f"  ⚠️ Zakończono pobieranie strumienia: {e}")

    if verbose:
        print(f"  ✓ Zebrano łącznie {len(raw_posts)} nagłówków postów. Rozpoczynanie głębokiej analizy interakcji...")

    def _forensic_process_post(p, post_idx):
        p_id = safe_getattr(p, "id") or f"unknown_{post_idx}"
        p_text = safe_getattr(p, "text") or ""
        p_title = safe_getattr(p, "title") or ""
        creation_time = safe_getattr(p, "creation_time")

        # Kryptograficzny hash treści posta
        content_canonical = f"{p_id}|{creation_time}|{p_title}|{p_text}".encode("utf-8")
        post_sha256 = calc_sha256(content_canonical)

        post_record = {
            "index": post_idx,
            "id": p_id,
            "url": safe_getattr(p, "url"),
            "creation_time_epoch": creation_time,
            "creation_time_local": format_ts(creation_time),
            "creation_time_utc": format_ts_utc(creation_time),
            "title": p_title,
            "text": p_text,
            "sha256_content_hash": post_sha256,
            "reaction_count": safe_getattr(p, "reaction_count"),
            "share_count": safe_getattr(p, "share_count"),
            "comments_count": safe_getattr(p, "comments_count"),
            "reactors_visible": safe_getattr(p, "reactors_visible"),
            "reaction_breakdown": safe_getattr(p, "top_reaction_breakdown") or [],
            "attachments": [],
            "comments": [],
            "reactors": [],
        }

        if verbose:
            print(f"  ↳ [{post_idx}/{len(raw_posts)}] Analiza posta ID: {p_id} ({format_ts(creation_time) or 'Brak daty'})...")

        # Załączniki multimedialne
        for att_idx, att in enumerate(safe_getattr(p, "attachments") or []):
            att_id = getattr(att, "id", None) or f"{p_id}_att_{att_idx}"
            img_url = getattr(att, "image_url", None)
            att_entry = {
                "id": att_id,
                "image_url": img_url,
                "url": getattr(att, "url", None),
                "type": att.__class__.__name__,
            }
            if download_media and img_url:
                att_file = f"post_{p_id}_{att_idx}.jpg"
                down_res = vault.download_artifact(img_url, att_file, f"POST_ATTACHMENT_{att.__class__.__name__.upper()}")
                if down_res:
                    att_entry["local_vault_file"] = att_file
                    att_entry["sha256"] = down_res["sha256"]
            post_record["attachments"].append(att_entry)

        if verbose and len(post_record["attachments"]) > 0:
            print(f"     • [Załączniki] Zabezpieczono {len(post_record['attachments'])} multimediów dla posta {p_id}.")

        # Komentarze i pod-komentarze
        if fetch_comments and post_record["comments_count"] not in (0, "0", None):
            try:
                c_count = 0
                for c in p.get_comments():
                    c_count += 1
                    c_owner = safe_profile_dict(getattr(c, "owner", None))
                    c_text = safe_getattr(c, "text") or ""
                    c_ts = safe_getattr(c, "created_time")
                    c_hash = calc_sha256(f"{safe_getattr(c, 'id')}|{c_ts}|{c_text}".encode("utf-8"))

                    c_entry = {
                        "id": safe_getattr(c, "id"),
                        "created_time_epoch": c_ts,
                        "created_time_local": format_ts(c_ts),
                        "created_time_utc": format_ts_utc(c_ts),
                        "text": c_text,
                        "sha256": c_hash,
                        "reaction_count": safe_getattr(c, "reaction_count"),
                        "author": c_owner,
                        "replies": [],
                    }

                    # Odpowiedzi na komentarz (sub-replies)
                    if getattr(c, "reply_count", 0) and getattr(c, "reply_count", 0) > 0:
                        try:
                            for rep in getattr(c, "get_replies", lambda: [])():
                                r_owner = safe_profile_dict(getattr(rep, "owner", None))
                                r_text = safe_getattr(rep, "text") or ""
                                r_ts = safe_getattr(rep, "created_time")
                                c_entry["replies"].append({
                                    "id": safe_getattr(rep, "id"),
                                    "created_time_epoch": r_ts,
                                    "created_time_local": format_ts(r_ts),
                                    "created_time_utc": format_ts_utc(r_ts),
                                    "text": r_text,
                                    "author": r_owner,
                                    "reaction_count": safe_getattr(rep, "reaction_count"),
                                })
                                if len(c_entry["replies"]) >= 10:
                                    break
                        except Exception:
                            pass

                    post_record["comments"].append(c_entry)
                    if c_count >= 50:
                        break
                if verbose and len(post_record["comments"]) > 0:
                    print(f"     • [Komentarze] Zabezpieczono {len(post_record['comments'])} komentarzy dla posta {p_id}.")
            except Exception:
                pass

        # Osoby reagujące (Reaktory)
        if fetch_reactors and post_record["reactors_visible"]:
            try:
                r_count = 0
                for r in p.get_reactors():
                    r_count += 1
                    r_prof = safe_profile_dict(getattr(r, "profile", None))
                    r_type_id = getattr(r, "reaction_type_id", None)
                    r_type_name, r_type_desc = REACTION_MAP.get(str(r_type_id), ("Reaction", "Reakcja"))
                    post_record["reactors"].append({
                        "reaction_type_id": r_type_id,
                        "reaction_name": r_type_name,
                        "reaction_label": r_type_desc,
                        "profile": r_prof,
                    })
                    if r_count >= 100:
                        break
                if verbose and len(post_record["reactors"]) > 0:
                    print(f"     • [Reaktory] Zabezpieczono {len(post_record['reactors'])} osób reagujących dla posta {p_id}.")
            except Exception:
                pass

        return post_record

    processed_posts = []
    if raw_posts:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_p = {
                executor.submit(_forensic_process_post, p, idx + 1): p
                for idx, p in enumerate(raw_posts)
            }
            try:
                for future in as_completed(future_to_p):
                    try:
                        p_res = future.result()
                        processed_posts.append(p_res)
                        if verbose:
                            txt_prev = (p_res["text"] or "").replace("\n", " ")[:40]
                            print(f"  🔍 Post [{p_res['id']}] 👍 {p_res['reaction_count']} | 💬 {p_res['comments_count']} — \"{txt_prev}...\"")
                    except Exception:
                        pass
            except KeyboardInterrupt:
                if verbose:
                    print("\n  ⚠️ Przerwano przetwarzanie postów (Ctrl+C). Używam pobranych danych...")

    # Sortowanie chronologiczne malejąco (najnowsze pierwsze)
    processed_posts.sort(key=lambda x: x.get("creation_time_epoch") or 0, reverse=True)

    # 5. Analiza powiązań (Sociogram) i behawioralna (Pattern of Life)
    if verbose:
        print("\n[FAZA 5/6] 🧠 Generowanie analizy kryminalistycznej: Sociogram & Pattern of Life...")

    # A. Sociogram powiązań (Associate Matrix)
    associates = {}  # user_id -> stats
    for post in processed_posts:
        # Komentujący
        for cmt in post.get("comments") or []:
            author = cmt.get("author")
            if author and author.get("id"):
                aid = author["id"]
                if aid == user_id:
                    continue  # pomiń samego siebie
                if aid not in associates:
                    associates[aid] = {
                        "id": aid,
                        "name": author.get("name") or "Nieznany",
                        "url": author.get("url"),
                        "avatar": author.get("profile_picture_url"),
                        "is_friend": aid in known_friend_ids,
                        "comments_count": 0,
                        "reactions_count": 0,
                        "reaction_breakdown": {},
                        "interaction_score": 0,
                    }
                associates[aid]["comments_count"] += 1
                associates[aid]["interaction_score"] += 3

            # Odpowiadający
            for rep in cmt.get("replies") or []:
                rep_author = rep.get("author")
                if rep_author and rep_author.get("id"):
                    raid = rep_author["id"]
                    if raid == user_id:
                        continue
                    if raid not in associates:
                        associates[raid] = {
                            "id": raid,
                            "name": rep_author.get("name") or "Nieznany",
                            "url": rep_author.get("url"),
                            "avatar": rep_author.get("profile_picture_url"),
                            "is_friend": raid in known_friend_ids,
                            "comments_count": 0,
                            "reactions_count": 0,
                            "reaction_breakdown": {},
                            "interaction_score": 0,
                        }
                    associates[raid]["comments_count"] += 1
                    associates[raid]["interaction_score"] += 2

        # Reagujący
        for rct in post.get("reactors") or []:
            r_prof = rct.get("profile")
            if r_prof and r_prof.get("id"):
                rid = r_prof["id"]
                if rid == user_id:
                    continue
                if rid not in associates:
                    associates[rid] = {
                        "id": rid,
                        "name": r_prof.get("name") or "Nieznany",
                        "url": r_prof.get("url"),
                        "avatar": r_prof.get("profile_picture_url"),
                        "is_friend": rid in known_friend_ids,
                        "comments_count": 0,
                        "reactions_count": 0,
                        "reaction_breakdown": {},
                        "interaction_score": 0,
                    }
                associates[rid]["reactions_count"] += 1
                associates[rid]["interaction_score"] += 1
                r_label = rct.get("reaction_label") or "Reakcja"
                associates[rid]["reaction_breakdown"][r_label] = associates[rid]["reaction_breakdown"].get(r_label, 0) + 1

    ranked_associates = sorted(associates.values(), key=lambda x: x["interaction_score"], reverse=True)

    if verbose:
        print(f"  ✓ Zidentyfikowano {len(ranked_associates)} unikalnych profili powiązanych (interaktorów).")
        if ranked_associates:
            print("  ↳ Najbardziej powiązani z celem:")
            for idx, a in enumerate(ranked_associates[:5], 1):
                f_rel = "Znajomy" if a.get("is_friend") else "Zewnętrzny"
                print(f"     #{idx} {a.get('name')} (ID: {a.get('id')}) [{f_rel}] — Wynik: {a.get('interaction_score')} pkt (💬 {a.get('comments_count')} | 👍 {a.get('reactions_count')})")

    # B. Pattern of Life (rozkład godzinowy i dni tygodnia)
    hours_dist = {h: 0 for h in range(24)}
    days_dist = {d: 0 for d in ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]}
    day_mapping = {0: "Poniedziałek", 1: "Wtorek", 2: "Środa", 3: "Czwartek", 4: "Piątek", 5: "Sobota", 6: "Niedziela"}

    earliest_epoch = None
    latest_epoch = None

    for post in processed_posts:
        ep = post.get("creation_time_epoch")
        if ep:
            dt = datetime.fromtimestamp(ep)
            hours_dist[dt.hour] += 1
            days_dist[day_mapping[dt.weekday()]] += 1
            if earliest_epoch is None or ep < earliest_epoch:
                earliest_epoch = ep
            if latest_epoch is None or ep > latest_epoch:
                latest_epoch = ep

    pattern_of_life = {
        "hourly_distribution": hours_dist,
        "weekday_distribution": days_dist,
        "earliest_post_local": format_ts(earliest_epoch),
        "latest_post_local": format_ts(latest_epoch),
        "total_posts_analyzed": len(processed_posts),
    }

    if verbose:
        print(f"  ✓ Przeliczono rozkład aktywności Pattern of Life dla {len(processed_posts)} postów.")

    # 6. Eksport dowodowy: JSON, CSV, Raport HTML, Skrypt weryfikacyjny, Manifest
    if verbose:
        print("\n[FAZA 6/6] 🔐 Generowanie pakietu dowodowego, sum kontrolnych SHA-256 i raportu HTML...")

    end_utc = datetime.now(timezone.utc).isoformat()
    end_local = datetime.now().isoformat()
    elapsed_sec = round(time.time() - t_start, 1)

    # A. Zapis surowych danych dowodowych (evidence_raw.json)
    raw_evidence_data = {
        "case_metadata": {
            "case_id": case_id,
            "examiner": examiner_name,
            "examiner_account_id": c_user,
            "host_machine": socket.gethostname(),
            "os_environment": f"{platform.system()} {platform.release()}",
            "python_version": platform.python_version(),
            "acquisition_start_utc": start_utc,
            "acquisition_end_utc": end_utc,
            "acquisition_duration_seconds": elapsed_sec,
            "target_input": target_str,
            "target_id": user_id,
            "target_name": user_name,
        },
        "dossier": dossier,
        "social_graph": {
            "friends": friends_list,
            "followers": followers_list,
            "followees": followees_list,
        },
        "multimedia": {
            "photos": photos_catalog,
            "videos": videos_catalog,
        },
        "posts": processed_posts,
        "analytics": {
            "ranked_associates": ranked_associates,
            "pattern_of_life": pattern_of_life,
        }
    }

    raw_json_file = case_dir / "evidence_raw.json"
    with open(raw_json_file, "w", encoding="utf-8") as f:
        json.dump(raw_evidence_data, f, ensure_ascii=False, indent=2)
    if verbose:
        print(f"  ↳ Zapisano surowy zrzut danych: {raw_json_file.name}")

    # B. Zapis rejestru osi czasu (evidence_timeline.csv)
    timeline_csv_file = case_dir / "evidence_timeline.csv"
    with open(timeline_csv_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ID Posta", "Data (Lokalna)", "Data (UTC)", "Epoch", "Treść",
            "Polubienia/Reakcje", "Komentarze", "Udostępnienia", "Liczba Załączników", "SHA-256 Treści", "Link"
        ])
        for p in processed_posts:
            writer.writerow([
                p.get("id"),
                p.get("creation_time_local"),
                p.get("creation_time_utc"),
                p.get("creation_time_epoch"),
                (p.get("text") or "").replace("\n", " "),
                p.get("reaction_count"),
                p.get("comments_count"),
                p.get("share_count"),
                len(p.get("attachments") or []),
                p.get("sha256_content_hash"),
                p.get("url"),
            ])
    if verbose:
        print(f"  ↳ Zapisano oś czasu: {timeline_csv_file.name}")

    # C. Zapis rejestru powiązań (evidence_associates.csv)
    associates_csv_file = case_dir / "evidence_associates.csv"
    with open(associates_csv_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ID Kontaktu", "Imię i Nazwisko", "Profil URL", "Czy w Znajomych",
            "Liczba Komentarzy", "Liczba Reakcji", "Wynik Interakcji (Score)"
        ])
        for a in ranked_associates:
            writer.writerow([
                a.get("id"),
                a.get("name"),
                a.get("url"),
                "TAK" if a.get("is_friend") else "NIE",
                a.get("comments_count"),
                a.get("reactions_count"),
                a.get("interaction_score"),
            ])
    if verbose:
        print(f"  ↳ Zapisano rejestr powiązań: {associates_csv_file.name}")

    # D. Zapis rejestru znajomych (evidence_friends.csv)
    friends_csv_file = case_dir / "evidence_friends.csv"
    with open(friends_csv_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID Kontaktu", "Imię i Nazwisko", "Profil URL", "Płeć", "Zweryfikowany", "Awatar URL"])
        for fr in friends_list:
            writer.writerow([
                fr.get("id"),
                fr.get("name"),
                fr.get("url"),
                fr.get("gender") or "",
                "TAK" if fr.get("is_verified") else "NIE",
                fr.get("profile_picture_url") or "",
            ])
    if verbose:
        print(f"  ↳ Zapisano rejestr wszystkich znajomych: {friends_csv_file.name} ({len(friends_list)} kontaktów)")

    # D. Generowanie oficjalnego raportu kryminalistycznego HTML (forensic_report.html)
    report_html_file = case_dir / "forensic_report.html"
    _generate_forensic_html_report(report_html_file, raw_evidence_data, vault.manifest_entries)
    if verbose:
        print(f"  ↳ Skompilowano oficjalny raport HTML: {report_html_file.name}")

    # E. Generowanie skryptu weryfikacji integralności (verify_evidence.py)
    verify_script_file = case_dir / "verify_evidence.py"
    _generate_verification_script(verify_script_file)
    if verbose:
        print(f"  ↳ Wygenerowano autonomiczny skrypt weryfikacji: {verify_script_file.name}")

    # F. Kompilacja ostatecznego Manifestu Dowodowego (evidence_manifest.json)
    manifest_file = case_dir / "evidence_manifest.json"
    manifest_data = {
        "manifest_version": "1.0",
        "case_metadata": raw_evidence_data["case_metadata"],
        "target_summary": {
            "id": user_id,
            "name": user_name,
            "posts_acquired": len(processed_posts),
            "friends_acquired": len(friends_list),
            "media_files_acquired": len(vault.manifest_entries),
            "associates_analyzed": len(ranked_associates),
        },
        "evidence_files": []
    }

    # Zarejestruj wszystkie wygenerowane pliki w manifeście
    all_case_files = [
        ("forensic_report.html", "FORENSIC_HTML_REPORT"),
        ("acquisition_log.txt", "ACQUISITION_LOG"),
        ("evidence_raw.json", "EVIDENCE_RAW_JSON"),
        ("evidence_timeline.csv", "TIMELINE_CSV"),
        ("evidence_associates.csv", "ASSOCIATES_CSV"),
        ("evidence_friends.csv", "FRIENDS_CSV"),
        ("verify_evidence.py", "VERIFICATION_SCRIPT"),
    ]

    for rel_name, art_type in all_case_files:
        fpath = case_dir / rel_name
        if fpath.exists():
            f_sha, f_md5, f_sz = calc_file_hashes(fpath)
            manifest_data["evidence_files"].append({
                "rel_path": rel_name,
                "artifact_type": art_type,
                "size_bytes": f_sz,
                "sha256": f_sha,
                "md5": f_md5,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            })

    # Dołącz zarejestrowane multimedia
    for m in vault.manifest_entries:
        manifest_data["evidence_files"].append(m)

    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, ensure_ascii=False, indent=2)

    # Oblicz hash samego manifestu
    m_sha, m_md5, _ = calc_file_hashes(manifest_file)

    if verbose:
        print("\n" + "█" * 78)
        print("   ✅ AKWIZYCJA KRYMINALISTYCZNA ZAKOŃCZONA SUKCESEM")
        print("█" * 78)
        print(f"  📂 Folder Sprawy           : {case_dir}")
        print(f"  🌐 Oficjalny Raport HTML   : {report_html_file.name}")
        print(f"  📜 Manifest Dowodowy (JSON): {manifest_file.name}")
        print(f"  🔐 SHA-256 Manifestu       : {m_sha}")
        print(f"  📊 Zebrane Artefakty       : {len(processed_posts)} postów, {len(friends_list)} znajomych, {len(vault.manifest_entries)} plików mediów")
        print(f"  ⏱️ Czas Realizacji         : {elapsed_sec} s")
        print("█" * 78 + "\n")

    return {
        "case_dir": str(case_dir),
        "report_html": str(report_html_file),
        "manifest_file": str(manifest_file),
        "manifest_sha256": m_sha,
        "posts_count": len(processed_posts),
        "friends_count": len(friends_list),
        "media_count": len(vault.manifest_entries),
    }


def _generate_forensic_html_report(output_file: Path, data: dict, media_entries: list):
    """Generuje interaktywny, certyfikowany raport kryminalistyczny w HTML ze stylem śledczym."""
    meta = data["case_metadata"]
    dos = data["dossier"]
    ident = dos["identity"]
    stats = dos["stats"]
    about = dos["background"]
    posts = data["posts"]
    friends = data["social_graph"]["friends"]
    associates = data["analytics"]["ranked_associates"]
    pol = data["analytics"]["pattern_of_life"]

    avatar_entry = next((m for m in media_entries if m["artifact_type"] == "PROFILE_PICTURE"), None)
    avatar_src = f"media/{avatar_entry['filename']}" if avatar_entry else (ident.get("profile_picture_url") or "")

    # Wykres godzinowy SVG
    max_h = max(pol["hourly_distribution"].values()) if pol["hourly_distribution"] and max(pol["hourly_distribution"].values()) > 0 else 1
    h_bars_svg = []
    for h in range(24):
        val = pol["hourly_distribution"].get(h, 0)
        h_pct = int((val / max_h) * 100)
        h_bars_svg.append(f"""
            <div class="hist-col" title="Godzina {h:02d}:00 - {val} postów">
                <div class="hist-bar" style="height: {max(h_pct, 4)}%;"></div>
                <div class="hist-label">{h:02d}</div>
                <div class="hist-val">{val}</div>
            </div>
        """)

    # Wykres dni tygodnia
    max_d = max(pol["weekday_distribution"].values()) if pol["weekday_distribution"] and max(pol["weekday_distribution"].values()) > 0 else 1
    d_bars_svg = []
    for d, val in pol["weekday_distribution"].items():
        d_pct = int((val / max_d) * 100)
        d_bars_svg.append(f"""
            <div class="day-row">
                <span class="day-name">{d}</span>
                <div class="day-bar-wrap">
                    <div class="day-bar" style="width: {max(d_pct, 3)}%;"></div>
                </div>
                <span class="day-cnt">{val}</span>
            </div>
        """)

    # Tabela powiązań (Sociogram top 20)
    top_associates_rows = []
    for idx, a in enumerate(associates[:25], 1):
        friend_badge = '<span class="badge badge-success">Znajomy</span>' if a.get("is_friend") else '<span class="badge badge-warning">Zewnętrzny</span>'
        top_associates_rows.append(f"""
            <tr>
                <td><strong>#{idx}</strong></td>
                <td>
                    <div class="user-cell">
                        <div>
                            <a href="{a.get('url') or '#'}" target="_blank" class="user-name">{a.get('name')}</a>
                            <div class="user-id">ID: {a.get('id')}</div>
                        </div>
                    </div>
                </td>
                <td>{friend_badge}</td>
                <td class="text-center font-mono">{a.get('comments_count')}</td>
                <td class="text-center font-mono">{a.get('reactions_count')}</td>
                <td class="text-center font-mono font-bold text-accent">{a.get('interaction_score')}</td>
            </tr>
        """)

    # Tabela postów (Dziennik dowodowy)
    post_cards = []
    for p in posts:
        att_html = ""
        for att in p.get("attachments") or []:
            local_f = att.get("local_vault_file")
            img_s = f"media/{local_f}" if local_f else att.get("image_url")
            if img_s:
                att_html += f"""
                    <div class="post-media-thumb">
                        <a href="{img_s}" target="_blank">
                            <img src="{img_s}" alt="Załącznik posta" loading="lazy" />
                        </a>
                        <div class="media-sha">SHA: {(att.get('sha256') or '')[:12]}...</div>
                    </div>
                """

        # Komentarze do posta
        cmts_html = ""
        if p.get("comments"):
            cmts_html = '<div class="post-comments-wrap"><div class="cmts-hdr">Zarejestrowane Komentarze:</div>'
            for c in p.get("comments")[:10]:
                c_auth = c.get("author") or {}
                cmts_html += f"""
                    <div class="cmt-item">
                        <div class="cmt-meta">
                            <strong>{c_auth.get('name') or 'Nieznany'}</strong> (ID: {c_auth.get('id') or '-'}) • {c.get('created_time_local') or ''}
                        </div>
                        <div class="cmt-body">{c.get('text') or ''}</div>
                    </div>
                """
            if len(p["comments"]) > 10:
                cmts_html += f'<div class="cmts-more">... oraz {len(p["comments"]) - 10} kolejnych komentarzy w plikach CSV/JSON.</div>'
            cmts_html += '</div>'

        post_cards.append(f"""
            <div class="evidence-card" id="post-{p.get('id')}">
                <div class="evidence-card-hdr">
                    <div class="ev-hdr-left">
                        <span class="badge badge-accent">POST #{p.get('index')}</span>
                        <span class="badge badge-outline">ID: {p.get('id')}</span>
                        <span class="ev-date">📅 {p.get('creation_time_local') or 'Brak daty'} ({p.get('creation_time_utc') or ''})</span>
                    </div>
                    <div class="ev-hdr-right">
                        <a href="{p.get('url') or '#'}" target="_blank" class="ev-link">🔗 Oryginalny Link FB</a>
                    </div>
                </div>
                <div class="evidence-card-body">
                    {f'<div class="post-title"><strong>{p.get("title")}</strong></div>' if p.get("title") else ''}
                    <div class="post-text">{p.get('text') or '<span class="text-muted">(Brak treści tekstowej - wpis multimedialny lub zaktualizowanie profilu)</span>'}</div>
                    {f'<div class="post-media-grid">{att_html}</div>' if att_html else ''}
                    {cmts_html}
                </div>
                <div class="evidence-card-ftr">
                    <div class="post-stats">
                        <span>👍 Reakcje: <strong>{p.get('reaction_count') or 0}</strong></span>
                        <span>💬 Komentarze: <strong>{p.get('comments_count') or 0}</strong></span>
                        <span>🔄 Udostępnienia: <strong>{p.get('share_count') or 0}</strong></span>
                    </div>
                    <div class="post-hash" title="Kryptograficzna suma kontrolna SHA-256 treści posta">
                        <span class="hash-label">SHA-256:</span>
                        <code>{p.get('sha256_content_hash')}</code>
                    </div>
                </div>
            </div>
        """)

    # Karty znajomych
    friends_cards = []
    for f in friends:
        friends_cards.append(f"""
            <div class="friend-card">
                <div class="friend-avatar">
                    <img src="{f.get('profile_picture_url') or ''}" alt="" onerror="this.src='https://static.xx.fbcdn.net/rsrc.php/v3/yo/r/UlIqmHJn-SK.gif'" />
                </div>
                <div class="friend-info">
                    <a href="{f.get('url') or '#'}" target="_blank" class="friend-name">{f.get('name') or 'Użytkownik'}</a>
                    <div class="friend-id">ID: {f.get('id')}</div>
                </div>
            </div>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RAPORT ŚLEDCZY: {ident.get('name')} | {meta.get('case_id')}</title>
    <style>
        :root {{
            --bg-main: #0B0F19;
            --bg-card: #151C2C;
            --bg-card-hover: #1A2338;
            --border: #232E47;
            --border-highlight: #00F0FF;
            --text-main: #E2E8F0;
            --text-muted: #8E9DB7;
            --accent: #00F0FF;
            --accent-glow: rgba(0, 240, 255, 0.25);
            --danger: #FF3366;
            --success: #00E676;
            --warning: #FFB300;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg-main);
            color: var(--text-main);
            font-family: var(--font-sans);
            line-height: 1.5;
            padding: 24px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}

        /* Evidence Official Header */
        .official-header {{
            background: linear-gradient(135deg, #0F172A 0%, #1E293B 100%);
            border: 2px solid var(--border-highlight);
            border-radius: 12px;
            padding: 24px 32px;
            margin-bottom: 24px;
            box-shadow: 0 0 25px var(--accent-glow);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }}
        .seal-box {{ display: flex; align-items: center; gap: 16px; }}
        .seal-badge {{
            background: #000;
            border: 2px solid var(--border-highlight);
            color: var(--accent);
            font-family: var(--font-mono);
            font-size: 24px;
            padding: 12px 18px;
            border-radius: 8px;
            font-weight: 900;
            letter-spacing: 2px;
        }}
        .seal-title h1 {{ font-size: 20px; font-weight: 800; color: #FFF; letter-spacing: 0.5px; text-transform: uppercase; }}
        .seal-title p {{ font-size: 13px; color: var(--accent); font-family: var(--font-mono); }}
        .custody-meta {{
            display: grid;
            grid-template-columns: repeat(2, minmax(200px, 1fr));
            gap: 8px 24px;
            font-size: 12px;
            background: rgba(0,0,0,0.4);
            padding: 12px 18px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .custody-meta div {{ display: flex; justify-content: space-between; gap: 10px; }}
        .meta-lbl {{ color: var(--text-muted); }}
        .meta-val {{ color: #FFF; font-family: var(--font-mono); font-weight: 600; }}

        /* Navigation Tabs */
        .tabs-nav {{
            display: flex;
            gap: 8px;
            margin-bottom: 24px;
            overflow-x: auto;
            border-bottom: 2px solid var(--border);
            padding-bottom: 8px;
        }}
        .tab-btn {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            color: var(--text-main);
            padding: 10px 18px;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            white-space: nowrap;
        }}
        .tab-btn:hover {{ background: var(--bg-card-hover); border-color: var(--accent); }}
        .tab-btn.active {{
            background: var(--accent);
            color: #000;
            border-color: var(--accent);
            font-weight: 800;
            box-shadow: 0 0 15px var(--accent-glow);
        }}

        /* Sections */
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        /* Profile Card */
        .profile-dossier {{
            display: grid;
            grid-template-columns: 320px 1fr;
            gap: 24px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .avatar-wrap {{ text-align: center; }}
        .avatar-img {{
            width: 220px;
            height: 220px;
            border-radius: 12px;
            object-fit: cover;
            border: 3px solid var(--accent);
            margin-bottom: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }}
        .dossier-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 16px;
        }}
        .dossier-item {{
            background: rgba(0,0,0,0.25);
            padding: 12px 16px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .dossier-lbl {{ font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; margin-bottom: 4px; }}
        .dossier-val {{ font-size: 14px; color: #FFF; word-break: break-word; }}

        /* Badges */
        .badge {{
            display: inline-block;
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 4px;
            font-family: var(--font-mono);
            text-transform: uppercase;
        }}
        .badge-accent {{ background: rgba(0, 240, 255, 0.15); color: var(--accent); border: 1px solid var(--accent); }}
        .badge-outline {{ background: transparent; color: var(--text-muted); border: 1px solid var(--border); }}
        .badge-success {{ background: rgba(0, 230, 118, 0.15); color: var(--success); border: 1px solid var(--success); }}
        .badge-warning {{ background: rgba(255, 179, 0, 0.15); color: var(--warning); border: 1px solid var(--warning); }}

        /* Pattern of life graphs */
        .pol-wrap {{
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 24px;
            margin-bottom: 24px;
        }}
        .chart-box {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
        }}
        .chart-title {{ font-size: 16px; font-weight: 700; margin-bottom: 16px; color: #FFF; display: flex; justify-content: space-between; }}
        .hist-container {{
            display: flex;
            height: 180px;
            align-items: flex-end;
            gap: 4px;
            padding-top: 20px;
            border-bottom: 1px solid var(--border);
        }}
        .hist-col {{ flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }}
        .hist-bar {{ width: 100%; background: linear-gradient(to top, var(--accent), #0077FF); border-radius: 3px 3px 0 0; min-height: 4px; }}
        .hist-label {{ font-size: 10px; font-family: var(--font-mono); color: var(--text-muted); margin-top: 6px; }}
        .hist-val {{ font-size: 9px; font-family: var(--font-mono); color: var(--accent); margin-bottom: 4px; }}

        .day-row {{ display: flex; align-items: center; margin-bottom: 10px; gap: 12px; font-size: 13px; }}
        .day-name {{ width: 100px; color: var(--text-muted); }}
        .day-bar-wrap {{ flex: 1; background: rgba(0,0,0,0.3); height: 14px; border-radius: 4px; overflow: hidden; }}
        .day-bar {{ height: 100%; background: var(--accent); border-radius: 4px; }}
        .day-cnt {{ width: 30px; text-align: right; font-family: var(--font-mono); font-weight: 700; color: #FFF; }}

        /* Tables */
        .table-wrap {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            margin-bottom: 24px;
        }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; }}
        th {{ background: rgba(0,0,0,0.4); padding: 12px 16px; color: var(--text-muted); font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border); }}
        td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); }}
        tr:hover td {{ background: var(--bg-card-hover); }}
        .user-name {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
        .user-name:hover {{ text-decoration: underline; }}
        .user-id {{ font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }}
        .font-mono {{ font-family: var(--font-mono); }}
        .font-bold {{ font-weight: 700; }}
        .text-center {{ text-align: center; }}
        .text-accent {{ color: var(--accent); }}

        /* Evidence Post Cards */
        .evidence-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-bottom: 18px;
            overflow: hidden;
        }}
        .evidence-card-hdr {{
            background: rgba(0,0,0,0.3);
            padding: 12px 18px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .ev-hdr-left {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
        .ev-date {{ font-size: 12px; font-family: var(--font-mono); color: var(--text-muted); }}
        .ev-link {{ font-size: 12px; color: var(--accent); text-decoration: none; }}
        .ev-link:hover {{ text-decoration: underline; }}
        .evidence-card-body {{ padding: 18px; font-size: 14px; white-space: pre-wrap; }}
        .post-media-grid {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; }}
        .post-media-thumb {{ width: 140px; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; background: #000; }}
        .post-media-thumb img {{ width: 100%; height: 100px; object-fit: cover; display: block; }}
        .media-sha {{ font-size: 9px; font-family: var(--font-mono); color: var(--text-muted); padding: 4px; text-align: center; }}
        .evidence-card-ftr {{
            background: rgba(0,0,0,0.4);
            padding: 10px 18px;
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
            font-size: 12px;
        }}
        .post-stats {{ display: flex; gap: 16px; color: var(--text-muted); }}
        .post-stats strong {{ color: #FFF; }}
        .post-hash {{ display: flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 11px; }}
        .hash-label {{ color: var(--text-muted); }}
        .post-hash code {{ color: var(--accent); background: rgba(0, 240, 255, 0.1); padding: 2px 6px; border-radius: 4px; }}

        /* Post comments inline */
        .post-comments-wrap {{ margin-top: 16px; background: rgba(0,0,0,0.3); border-radius: 8px; padding: 12px; border: 1px solid var(--border); }}
        .cmts-hdr {{ font-size: 11px; font-weight: 700; color: var(--accent); text-transform: uppercase; margin-bottom: 8px; }}
        .cmt-item {{ padding: 6px 0; border-bottom: 1px dashed var(--border); font-size: 12px; }}
        .cmt-item:last-child {{ border-bottom: none; }}
        .cmt-meta {{ color: var(--text-muted); font-size: 11px; }}
        .cmt-meta strong {{ color: #FFF; }}
        .cmt-body {{ margin-top: 2px; color: var(--text-main); }}
        .cmts-more {{ font-size: 11px; color: var(--text-muted); margin-top: 6px; font-style: italic; }}

        /* Friends Grid */
        .friends-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
        .friend-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .friend-avatar img {{ width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 1px solid var(--accent); }}
        .friend-info {{ overflow: hidden; }}
        .friend-name {{ color: #FFF; font-weight: 600; text-decoration: none; font-size: 13px; display: block; text-overflow: ellipsis; overflow: hidden; white-space: nowrap; }}
        .friend-name:hover {{ color: var(--accent); }}
        .friend-id {{ font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }}

        /* Print formatting */
        @media print {{
            body {{ background: #FFF !important; color: #000 !important; padding: 0 !important; }}
            .official-header {{ border: 2px solid #000 !important; box-shadow: none !important; background: none !important; }}
            .tabs-nav {{ display: none !important; }}
            .tab-content {{ display: block !important; margin-bottom: 40px !important; page-break-inside: avoid; }}
            .evidence-card {{ border: 1px solid #999 !important; background: #FFF !important; page-break-inside: avoid; }}
            .hist-bar {{ background: #444 !important; }}
            .day-bar {{ background: #444 !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Evidence Official Header -->
        <header class="official-header">
            <div class="seal-box">
                <div class="seal-badge">DFIR</div>
                <div class="seal-title">
                    <h1>Protokół Cyfrowej Informatyki Śledczej</h1>
                    <p>Certyfikowany Wyciąg Dowodowy Platformy Facebook</p>
                </div>
            </div>
            <div class="custody-meta">
                <div><span class="meta-lbl">Sygnatura Sprawy:</span> <span class="meta-val">{meta.get('case_id')}</span></div>
                <div><span class="meta-lbl">Badający (Examiner):</span> <span class="meta-val">{meta.get('examiner')}</span></div>
                <div><span class="meta-lbl">Konto Sesji (c_user):</span> <span class="meta-val">{meta.get('examiner_account_id')}</span></div>
                <div><span class="meta-lbl">Host Śledczy:</span> <span class="meta-val">{meta.get('host_machine')}</span></div>
                <div><span class="meta-lbl">Czas Rozpoczęcia:</span> <span class="meta-val">{meta.get('acquisition_start_utc')}</span></div>
                <div><span class="meta-lbl">Czas Zakończenia:</span> <span class="meta-val">{meta.get('acquisition_end_utc')}</span></div>
            </div>
        </header>

        <!-- Navigation Tabs -->
        <nav class="tabs-nav">
            <button class="tab-btn active" onclick="openTab('tab-dossier')">👤 Kartoteka Celu</button>
            <button class="tab-btn" onclick="openTab('tab-pol')">⏱️ Pattern of Life (24h/7d)</button>
            <button class="tab-btn" onclick="openTab('tab-sociogram')">👥 Sociogram Powiązań ({len(associates)})</button>
            <button class="tab-btn" onclick="openTab('tab-timeline')">📜 Oś Czasu Postów ({len(posts)})</button>
            <button class="tab-btn" onclick="openTab('tab-friends')">🤝 Siatka Znajomych ({len(friends)})</button>
            <button class="tab-btn" onclick="openTab('tab-vault')">🖼️ Zabezpieczone Media ({len(media_entries)})</button>
        </nav>

        <!-- TAB 1: DOSSIER -->
        <section id="tab-dossier" class="tab-content active">
            <div class="profile-dossier">
                <div class="avatar-wrap">
                    <img src="{avatar_src}" alt="Zdjęcie profilowe" class="avatar-img" onerror="this.src='https://static.xx.fbcdn.net/rsrc.php/v3/yo/r/UlIqmHJn-SK.gif'" />
                    <h2 style="font-size: 20px; font-weight: 800; color: #FFF;">{ident.get('name')}</h2>
                    <div style="font-family: var(--font-mono); font-size: 12px; color: var(--accent); margin-bottom: 8px;">ID: {ident.get('id')}</div>
                    {f'<div class="badge badge-success">Profil Zweryfikowany (Blue Badge)</div>' if ident.get('is_verified') else '<div class="badge badge-outline">Konto Standardowe</div>'}
                </div>
                <div class="dossier-grid">
                    <div class="dossier-item">
                        <div class="dossier-lbl">Adres Profilu (Vanity URL)</div>
                        <div class="dossier-val"><a href="{ident.get('url') or '#'}" target="_blank" class="user-name">{ident.get('url') or 'Brak'}</a></div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Płeć Deklarowana</div>
                        <div class="dossier-val">{ident.get('gender') or 'Nieokreślona'}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Pseudonimy / Nicknames</div>
                        <div class="dossier-val">{', '.join([n.get('name','') for n in ident.get('nicknames', [])]) or 'Brak'}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Związek / Status Partnerski</div>
                        <div class="dossier-val">{about.get('relationship', {}).get('type') or 'Brak danych'} {about.get('relationship', {}).get('date') or ''}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Data Urodzenia</div>
                        <div class="dossier-val">{about.get('birthday') or 'Brak danych'}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Lokalizacja / Miejsce Pobytu</div>
                        <div class="dossier-val">{about.get('current_location') or 'Brak danych'}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Miejscowość Pochodzenia (Hometown)</div>
                        <div class="dossier-val">{about.get('hometown') or 'Brak danych'}</div>
                    </div>
                    <div class="dossier-item">
                        <div class="dossier-lbl">Statystyki Sieci</div>
                        <div class="dossier-val">Znajomi: <strong>{stats.get('friends_count') or len(friends)}</strong> | Obserwujący: <strong>{stats.get('followers_count') or 'Ukryte'}</strong></div>
                    </div>
                    <div class="dossier-item" style="grid-column: 1 / -1;">
                        <div class="dossier-lbl">Biogram (Bio / About Me)</div>
                        <div class="dossier-val">{about.get('biography') or '<span class="text-muted">Brak wpisu biogramu</span>'}</div>
                    </div>
                    <div class="dossier-item" style="grid-column: 1 / -1;">
                        <div class="dossier-lbl">Ulubione Cytaty (Quotes)</div>
                        <div class="dossier-val">{about.get('favorite_quotes') or '<span class="text-muted">Brak cytatów w profilu</span>'}</div>
                    </div>
                    <div class="dossier-item" style="grid-column: 1 / -1;">
                        <div class="dossier-lbl">Historia Zatrudnienia (Works)</div>
                        <div class="dossier-val">{', '.join([f"{w.get('company','')} ({w.get('desc','')})" for w in about.get('works', [])]) or 'Brak danych'}</div>
                    </div>
                    <div class="dossier-item" style="grid-column: 1 / -1;">
                        <div class="dossier-lbl">Wykształcenie (Edukacja)</div>
                        <div class="dossier-val">
                            Uczelnie: {', '.join([c.get('name','') for c in about.get('colleges', [])]) or 'Brak'}<br>
                            Szkoły średnie: {', '.join([h.get('name','') for h in about.get('high_schools', [])]) or 'Brak'}
                        </div>
                    </div>
                    <div class="dossier-item" style="grid-column: 1 / -1;">
                        <div class="dossier-lbl">Członkowie Rodziny (Family)</div>
                        <div class="dossier-val">{', '.join([f"{f.get('name','')}: {f.get('type','')}" for f in about.get('family', [])]) or 'Brak zarejestrowanych powiązań rodzinnych'}</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- TAB 2: PATTERN OF LIFE -->
        <section id="tab-pol" class="tab-content">
            <div class="pol-wrap">
                <div class="chart-box">
                    <div class="chart-title">
                        <span>Rozkład Aktywności w Cyklu Dobowym (00:00 - 23:00)</span>
                        <span class="badge badge-accent">24H TIMELINE MATRIX</span>
                    </div>
                    <div class="hist-container">
                        {''.join(h_bars_svg)}
                    </div>
                    <div style="font-size: 11px; color: var(--text-muted); margin-top: 10px; font-family: var(--font-mono);">
                        Wskazuje na prawdopodobną strefę czasową oraz rytm dobowy (godziny snu vs szczyty publikacji celu).
                    </div>
                </div>
                <div class="chart-box">
                    <div class="chart-title">
                        <span>Aktywność wg Dni Tygodnia</span>
                        <span class="badge badge-outline">WEEKDAY FREQ</span>
                    </div>
                    <div>
                        {''.join(d_bars_svg)}
                    </div>
                </div>
            </div>
        </section>

        <!-- TAB 3: SOCIOGRAM & ASSOCIATES -->
        <section id="tab-sociogram" class="tab-content">
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th style="width: 60px;">Ranga</th>
                            <th>Profil Osoby Powiązanej</th>
                            <th style="width: 120px;">Relacja</th>
                            <th class="text-center" style="width: 110px;">Komentarze</th>
                            <th class="text-center" style="width: 110px;">Reakcje</th>
                            <th class="text-center" style="width: 130px;">Wskaźnik Bliskości</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(top_associates_rows) if top_associates_rows else '<tr><td colspan="6" class="text-center text-muted">Brak zarejestrowanych interakcji na analizowanych wpisach</td></tr>'}
                    </tbody>
                </table>
            </div>
        </section>

        <!-- TAB 4: POST TIMELINE & EVIDENCE LEDGER -->
        <section id="tab-timeline" class="tab-content">
            <div class="posts-stream">
                {''.join(post_cards) if post_cards else '<div class="evidence-card"><div class="evidence-card-body text-center text-muted">Brak postów na osi czasu celu.</div></div>'}
            </div>
        </section>

        <!-- TAB 5: FRIENDS -->
        <section id="tab-friends" class="tab-content">
            <div style="margin-bottom: 20px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap;">
                <input type="text" id="friendSearchInput" placeholder="🔍 Szukaj znajomego (imię, nazwisko, ID)..." 
                       oninput="filterFriendsGrid()" 
                       style="flex: 1; min-width: 280px; max-width: 450px; padding: 10px 14px; background: rgba(0,0,0,0.4); border: 1px solid var(--border); border-radius: 6px; color: #FFF; font-size: 13px;">
                <span style="font-size: 12px; color: var(--text-muted); font-family: var(--font-mono);" id="friendCountLabel">
                    Zabezpieczono łącznie: <strong>{len(friends)}</strong> kontaktów
                </span>
            </div>
            <div class="friends-grid" id="friendsGridContainer">
                {''.join(friends_cards) if friends_cards else '<div class="text-muted">Lista znajomych celu jest ukryta lub nie zawiera publicznych wpisów.</div>'}
            </div>
        </section>

        <!-- TAB 6: MEDIA VAULT -->
        <section id="tab-vault" class="tab-content">
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Plik Lokalny</th>
                            <th>Typ Artefaktu</th>
                            <th>Rozmiar</th>
                            <th>Suma Kontrolna SHA-256</th>
                            <th>Akcja</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join([f"""
                            <tr>
                                <td><code>{m.get('filename')}</code></td>
                                <td><span class="badge badge-outline">{m.get('artifact_type')}</span></td>
                                <td class="font-mono">{m.get('size_bytes', 0):,} B</td>
                                <td><code class="text-accent">{m.get('sha256')}</code></td>
                                <td><a href="{m.get('rel_path')}" target="_blank" class="user-name">Otwórz Plik</a></td>
                            </tr>
                        """ for m in media_entries]) if media_entries else '<tr><td colspan="5" class="text-center text-muted">Brak pobranych multimediów</td></tr>'}
                    </tbody>
                </table>
            </div>
        </section>
    </div>

    <script>
        function openTab(tabId) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            const target = document.getElementById(tabId);
            if (target) target.classList.add('active');
            event.currentTarget.classList.add('active');
        }}

        function filterFriendsGrid() {{
            const q = (document.getElementById('friendSearchInput')?.value || '').toLowerCase().trim();
            const cards = document.querySelectorAll('.friend-card');
            let visible = 0;
            cards.forEach(c => {{
                const name = (c.querySelector('.friend-name')?.textContent || '').toLowerCase();
                const id = (c.querySelector('.friend-id')?.textContent || '').toLowerCase();
                if (!q || name.includes(q) || id.includes(q)) {{
                    c.style.display = 'flex';
                    visible++;
                }} else {{
                    c.style.display = 'none';
                }}
            }});
            const lbl = document.getElementById('friendCountLabel');
            if (lbl) lbl.innerHTML = `Wyświetlono: <strong>${{visible}}</strong> z ${{cards.length}} kontaktów`;
        }}
    </script>
</body>
</html>
"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)


def _generate_verification_script(output_file: Path):
    """Generuje autonomiczny skrypt weryfikacji sum kontrolnych pakietu dowodowego."""
    script_content = '''#!/usr/bin/env python3
"""
Autonomiczny skrypt weryfikacji integralności materiału dowodowego (DFIR Evidence Verifier).
Sprawdza zgodność plików na dysku z kryptograficznym manifestem evidence_manifest.json.
"""

import sys
import json
import hashlib
from pathlib import Path

def calc_sha256(filepath):
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()

def main():
    base_dir = Path(__file__).resolve().parent
    manifest_file = base_dir / "evidence_manifest.json"

    print("=" * 72)
    print("   🔐 DFIR EVIDENCE INTEGRITY VERIFIER")
    print("=" * 72)

    if not manifest_file.exists():
        print("[BLAD] Brak pliku evidence_manifest.json w folderze sprawy!")
        sys.exit(1)

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    meta = manifest.get("case_metadata", {})
    print(f"Nr Sprawy:        {meta.get('case_id')}")
    print(f"Śledczy:          {meta.get('examiner')}")
    print(f"Cel (Target):     {meta.get('target_name')} (ID: {meta.get('target_id')})")
    print(f"Data Akwizycji:   {meta.get('acquisition_start_utc')}")
    print("-" * 72)

    files = manifest.get("evidence_files", [])
    total = len(files)
    valid = 0
    errors = 0

    for idx, item in enumerate(files, 1):
        rel_p = item.get("rel_path") or item.get("filename")
        fpath = base_dir / rel_p
        expected_sha = item.get("sha256")

        if not fpath.exists():
            print(f"  [{idx}/{total}] ❌ BRAK PLIKU: {rel_p}")
            errors += 1
            continue

        actual_sha = calc_sha256(fpath)
        if actual_sha.lower() == expected_sha.lower():
            print(f"  [{idx}/{total}] ✅ ZGODNY: {rel_p} (SHA256 OK)")
            valid += 1
        else:
            print(f"  [{idx}/{total}] 🚨 NARUSZENIE INTEGRALNOŚCI: {rel_p}")
            print(f"      Oczekiwany : {expected_sha}")
            print(f"      Faktyczny  : {actual_sha}")
            errors += 1

    print("-" * 72)
    if errors == 0:
        print(f"✅ WSZYSTKIE PLIKI ({valid}/{total}) SĄ W 100% AUTENTYCZNE I ZGODNE Z MANIFESTEM.")
        print("Łańcuch dowodowy (Chain of Custody) został w pełni zachowany.")
        sys.exit(0)
    else:
        print(f"🚨 WYKRYTO {errors} BŁĘDÓW LUB ZMIAN W PLIKACH DOWODOWYCH!")
        sys.exit(2)

if __name__ == "__main__":
    main()
'''
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(script_content)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Facebook Digital Forensics & OSINT Evidence Preservation Tool")
    parser.add_argument("target", nargs="?", help="URL, username lub Facebook ID celu")
    parser.add_argument("--limit", type=str, default="50", help="Limit postów (np. 10, 25, 50, 100, all)")
    parser.add_argument("--case-id", type=str, default=None, help="Sygnatura/Numer sprawy (Case ID)")
    parser.add_argument("--examiner", type=str, default="Borman (Examiner)", help="Imię/Nazwisko lub identyfikator badającego")
    parser.add_argument("--friends-limit", type=str, default=None, help="Limit znajomych (np. 100, 500, all; domyślnie: wszyscy)")
    parser.add_argument("--photos-limit", type=str, default=None, help="Limit zdjęć do pobrania (domyślnie: wszystkie)")
    parser.add_argument("--no-media", action="store_true", help="Wyłącz pobieranie multimediów na dysk")
    args = parser.parse_args()

    target = args.target
    if not target:
        print("\n" + "=" * 70)
        print("   🕵️ FACEBOOK DIGITAL FORENSICS & EVIDENCE VAULT")
        print("=" * 70)
        target = input("Podaj profil Facebooka (URL, username lub ID): ").strip()
        if not target:
            print("[BLAD] Nie podano celu.")
            sys.exit(1)

    limit_val = None if args.limit.lower() == "all" else int(args.limit)
    friends_val = None if not args.friends_limit or args.friends_limit.lower() == "all" else int(args.friends_limit)
    photos_val = None if not args.photos_limit or args.photos_limit.lower() == "all" else int(args.photos_limit)

    run_forensic_acquisition(
        target_str=target,
        examiner_name=args.examiner,
        case_id=args.case_id,
        post_limit=limit_val,
        friends_limit=friends_val,
        photos_limit=photos_val,
        fetch_comments=True,
        fetch_reactors=True,
        download_media=not args.no_media,
        verbose=True,
    )


if __name__ == "__main__":
    main()
