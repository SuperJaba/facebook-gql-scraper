"""
Moduł zarządzania historią zrzutów i wykrywania zmian (Diff Engine)
Zapisuje profile w czytelnych folderach o nazwach np. 'zuck', 'dawid.mikolaj' lub 'Dawid_Mikolaj'.
"""

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

BASE_PATH = Path(__file__).resolve().parent
ROOT_PATH = BASE_PATH.parent if BASE_PATH.name.lower() == "app" else BASE_PATH
HISTORY_DIR = ROOT_PATH / "profiles_history"


def get_profile_folder_name(data: dict) -> str:
    """
    Wyznacza czytelną nazwę folderu dla profilu:
    1. Nazwa użytkownika (vanity username) wyciągnięta z URL (np. 'zuck', 'dawid.mikolaj')
    2. Imię i nazwisko (np. 'Dawid_Mikolaj')
    3. Identyfikator jeśli nic innego nie ma
    """
    ident = data.get("identity") or data
    url = ident.get("url") or ""
    name = ident.get("name") or ""
    uid = str(ident.get("id") or "")

    username = None
    if url:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.strip("/").split("/") if p and p not in ("people", "profile", "p")]
        if path_parts and not path_parts[0].startswith("profile.php") and not path_parts[0].isdigit():
            username = path_parts[0]

    if username:
        clean_user = re.sub(r"[^\w\.-]", "", username)
        if clean_user:
            return clean_user

    if name:
        clean_name = re.sub(r"[^\w\.-]", "_", name.strip())
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        if clean_name:
            return clean_name

    return uid or "unknown_profile"


def get_profile_snapshots(user_id: Optional[str] = None, username: Optional[str] = None, name: Optional[str] = None) -> List[Path]:
    """Wyszukuje wcześniejsze zrzuty profilu po ID, username lub nazwie profilu."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    found_files = []

    # 1. Sprawdź bezpośrednio foldery o pasujących nazwach
    candidates_dirs = []
    if username:
        candidates_dirs.append(HISTORY_DIR / username)
        candidates_dirs.append(HISTORY_DIR / username.lower())
    if user_id:
        candidates_dirs.append(HISTORY_DIR / str(user_id))
    if name:
        clean_n = re.sub(r"[^\w\.-]", "_", name.strip())
        candidates_dirs.append(HISTORY_DIR / clean_n)

    for d in candidates_dirs:
        if d.exists() and d.is_dir():
            for f in d.glob("*.json"):
                if f not in found_files:
                    found_files.append(f)

    # 2. Przeszukaj wszystkie foldery w profiles_history i sprawdź zawartość
    all_json_files = list(HISTORY_DIR.glob("**/*.json")) + list(HISTORY_DIR.parent.glob("fb_*.json"))
    for f in all_json_files:
        if f in found_files:
            continue
        try:
            with open(f, "r", encoding="utf-8") as jf:
                data = json.load(jf)
                fid = str(data.get("id") or data.get("identity", {}).get("id") or "")
                furl = str(data.get("url") or data.get("identity", {}).get("url") or "")
                fname = str(data.get("name") or data.get("identity", {}).get("name") or "")

                matched = False
                if user_id and fid == str(user_id):
                    matched = True
                elif username and (username.lower() in furl.lower() or username.lower() in fname.lower() or f.parent.name.lower() == username.lower()):
                    matched = True
                elif name and name.lower() in fname.lower():
                    matched = True

                if matched and f not in found_files:
                    found_files.append(f)
        except Exception:
            continue

    # Sortuj od najnowszego do najstarszego
    found_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return found_files


def save_snapshot(data: dict) -> Path:
    """Zapisuje zrzut do czytelnego katalogu profilu (np. 'zuck' lub 'Dawid_Mikolaj')."""
    folder_name = get_profile_folder_name(data)
    user_dir = HISTORY_DIR / folder_name
    user_dir.mkdir(parents=True, exist_ok=True)

    uid = str(data.get("id") or data.get("identity", {}).get("id") or "unknown")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sanitized = "".join(c for c in (data.get("name") or data.get("identity", {}).get("name") or uid) if c.isalnum() or c in "_-")
    filename = f"snapshot_{sanitized}_{uid}_{ts}.json"
    file_path = user_dir / filename

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return file_path


def _parse_int_count(val):
    if val is None:
        return None
    val_str = str(val).strip().lower().replace(",", "").replace(" ", "")
    multiplier = 1
    if val_str.endswith("k"):
        multiplier = 1000
        val_str = val_str[:-1]
    elif val_str.endswith("m"):
        multiplier = 1000000
        val_str = val_str[:-1]
    try:
        return int(float(val_str) * multiplier)
    except Exception:
        return None


def compute_diff(old_data: dict, new_data: dict) -> Dict[str, Any]:
    """Oblicza różnice między dwoma zrzutami profilu (starszym i nowszym)."""
    old_ident = old_data.get("identity") or old_data
    new_ident = new_data.get("identity") or new_data

    old_stats = old_data.get("stats") or old_data.get("counts") or {}
    new_stats = new_data.get("stats") or new_data.get("counts") or {}

    old_about = old_data.get("about") or {}
    new_about = new_data.get("about") or {}

    diff = {
        "old_scraped_at": old_data.get("scraped_at"),
        "new_scraped_at": new_data.get("scraped_at"),
        "identity_changes": {},
        "stats_changes": {},
        "about_changes": {},
        "new_posts": [],
        "deleted_posts": [],
        "post_engagement_changes": [],
        "new_comments": [],
        "new_photos_count": 0,
        "new_videos_count": 0,
    }

    # 1. Zmiany tożsamości
    for key in ["name", "alternate_name", "status_text", "is_verified", "profile_picture_url"]:
        v_old = old_ident.get(key)
        v_new = new_ident.get(key)
        if v_old != v_new and (v_old or v_new):
            diff["identity_changes"][key] = {"old": v_old, "new": v_new}

    # 2. Zmiany statystyk
    for key in ["followers", "followees", "friends", "followers_count", "followees_count", "friends_count"]:
        norm_key = key.replace("_count", "")
        v_old = old_stats.get(key) or old_stats.get(norm_key)
        v_new = new_stats.get(key) or new_stats.get(norm_key)
        if v_old != v_new and (v_old is not None or v_new is not None):
            int_old = _parse_int_count(v_old)
            int_new = _parse_int_count(v_new)
            delta = (int_new - int_old) if (int_old is not None and int_new is not None) else None
            diff["stats_changes"][norm_key] = {"old": v_old, "new": v_new, "delta": delta}

    # 3. Zmiany w 'O mnie'
    for key in ["biography", "current_location", "hometown", "birthday", "relationship"]:
        v_old = old_about.get(key)
        v_new = new_about.get(key)
        if v_old != v_new and (v_old or v_new):
            diff["about_changes"][key] = {"old": v_old, "new": v_new}

    # 4. Posty i reakcje
    old_posts_list = old_data.get("posts") or []
    new_posts_list = new_data.get("posts") or []

    old_posts_map = {str(p.get("id")): p for p in old_posts_list if p.get("id")}
    new_posts_map = {str(p.get("id")): p for p in new_posts_list if p.get("id")}

    for pid, p in new_posts_map.items():
        if pid not in old_posts_map:
            diff["new_posts"].append(p)
        else:
            old_p = old_posts_map[pid]
            r_old = _parse_int_count(old_p.get("reactions_count") or old_p.get("reaction_count"))
            r_new = _parse_int_count(p.get("reactions_count") or p.get("reaction_count"))
            c_old = _parse_int_count(old_p.get("comments_count"))
            c_new = _parse_int_count(p.get("comments_count"))

            delta_r = (r_new - r_old) if (r_old is not None and r_new is not None) else 0
            delta_c = (c_new - c_old) if (c_old is not None and c_new is not None) else 0

            if delta_r != 0 or delta_c != 0:
                diff["post_engagement_changes"].append({
                    "id": pid,
                    "text": (p.get("text") or "")[:60],
                    "url": p.get("url"),
                    "reactions": {"old": r_old, "new": r_new, "delta": delta_r},
                    "comments": {"old": c_old, "new": c_new, "delta": delta_c},
                })

            old_c_ids = {str(c.get("id")) for c in old_p.get("comments", []) if c.get("id")}
            for c in p.get("comments", []):
                cid = str(c.get("id"))
                if cid and cid not in old_c_ids:
                    diff["new_comments"].append({
                        "post_id": pid,
                        "comment_id": cid,
                        "author": c.get("owner", {}).get("name") if isinstance(c.get("owner"), dict) else "Anonim",
                        "text": c.get("text"),
                        "created_time": c.get("created_time_formatted"),
                    })

    for pid, p in old_posts_map.items():
        if pid not in new_posts_map:
            diff["deleted_posts"].append(p)

    # 5. Media
    old_photos = {str(ph.get("id")) for ph in old_data.get("photos", []) if ph.get("id")}
    new_photos = {str(ph.get("id")) for ph in new_data.get("photos", []) if ph.get("id")}
    diff["new_photos_count"] = max(0, len(new_photos - old_photos))

    old_videos = {str(v.get("id")) for v in old_data.get("videos", []) if v.get("id")}
    new_videos = {str(v.get("id")) for v in new_data.get("videos", []) if v.get("id")}
    diff["new_videos_count"] = max(0, len(new_videos - old_videos))

    return diff


def print_diff_report(diff: Dict[str, Any]):
    """Drukuje czytelny raport porównawczy w terminalu."""
    print("\n" + "=" * 70)
    print("                RAPORT ZMIAN PROFILU (DIFF)               ")
    print("=" * 70)
    print(f"Poprzedni zrzut: {diff['old_scraped_at'] or 'brak daty'}")
    print(f"Aktualny zrzut:   {diff['new_scraped_at'] or 'teraz'}")
    print("-" * 70)

    has_changes = False

    if diff["identity_changes"]:
        has_changes = True
        print("\n🔹 ZMIANY W DANYCH PROFILU:")
        for k, v in diff["identity_changes"].items():
            print(f"  • {k}: '{v['old']}' ➔ '{v['new']}'")

    if diff["stats_changes"]:
        has_changes = True
        print("\n🔹 ZMIANY STATYSTYK:")
        for k, v in diff["stats_changes"].items():
            delta_str = f" ({'+' if (v['delta'] or 0) > 0 else ''}{v['delta']})" if v["delta"] is not None else ""
            print(f"  • {k}: {v['old']} ➔ {v['new']}{delta_str}")

    if diff["about_changes"]:
        has_changes = True
        print("\n🔹 ZMIANY W SEKCJI 'O MNIE' / BIO:")
        for k, v in diff["about_changes"].items():
            print(f"  • {k}:")
            print(f"      [Stare]: {v['old']}")
            print(f"      [Nowe]:  {v['new']}")

    if diff["new_posts"]:
        has_changes = True
        print(f"\n🔹 NOWE POSTY (+{len(diff['new_posts'])}):")
        for p in diff["new_posts"]:
            txt = (p.get("text") or "").replace("\n", " ")[:70]
            print(f"  🆕 [{p.get('id')}] {txt}...")
            print(f"     Link: {p.get('url')}")

    if diff["deleted_posts"]:
        has_changes = True
        print(f"\n🔹 USUNIĘTE POSTY (-{len(diff['deleted_posts'])}):")
        for p in diff["deleted_posts"]:
            txt = (p.get("text") or "").replace("\n", " ")[:70]
            print(f"  🗑️ [{p.get('id')}] {txt}...")

    if diff["post_engagement_changes"]:
        has_changes = True
        print(f"\n🔹 ZMIANY AKTYWNOŚCI NA ISTNIEJĄCYCH POSTACH ({len(diff['post_engagement_changes'])} postów):")
        for c in diff["post_engagement_changes"]:
            r = c["reactions"]
            cm = c["comments"]
            r_str = f"👍 Reakcje: {r['old']} ➔ {r['new']} ({'+' if r['delta'] > 0 else ''}{r['delta']})" if r['delta'] != 0 else ""
            c_str = f"💬 Komentarze: {cm['old']} ➔ {cm['new']} ({'+' if cm['delta'] > 0 else ''}{cm['delta']})" if cm['delta'] != 0 else ""
            print(f"  📈 Post [{c['id']}]: {r_str} {c_str}")
            print(f"     \"{c['text']}...\"")

    if diff["new_comments"]:
        has_changes = True
        print(f"\n🔹 NOWE KOMENTARZE (+{len(diff['new_comments'])}):")
        for com in diff["new_comments"][:15]:
            print(f"  💬 {com['author']}: \"{com['text']}\" (post: {com['post_id']})")
        if len(diff["new_comments"]) > 15:
            print(f"  ... oraz {len(diff["new_comments"]) - 15} kolejnych.")

    if diff["new_photos_count"] > 0:
        has_changes = True
        print(f"\n🔹 NOWE ZDJĘCIA: +{diff['new_photos_count']}")

    if diff["new_videos_count"] > 0:
        has_changes = True
        print(f"\n🔹 NOWE FILMY / ROLKI: +{diff['new_videos_count']}")

    if not has_changes:
        print("\n✨ Brak zauważalnych zmian od ostatniego sprawdzenia!")
    print("=" * 70 + "\n")
