from pathlib import Path
import facebook_gql_scraper as fgs

COOKIES_PATH = Path(__file__).parent / "facebook_cookies.json"

# Inicjalizacja kontekstu z ciasteczkami (quiet=True wycisza logi)
ctx = fgs.FacebookContext(cookies_file=str(COOKIES_PATH), quiet=True)

# Pobranie profilu po username lub id:
user = fgs.Profile.from_username(ctx, username="dawid.mikolaj")
# user = fgs.Profile.from_id(ctx, id="4")

print(f"Imię i nazwisko: {user.name}")
print(f"ID: {user.id}")
print(f"Obserwujący: {user.followers_count}, Znajomi: {user.friends_count}")
print(f"Bio: {user.biography}")
print(f"Lokalizacja: {user.current_location}")

# Pobranie ostatnich postów użytkownika (np. 3):
for i, post in enumerate(user.get_posts()):
    print(f"\n--- Post {i+1} [{post.id}] ---")
    print(post.text[:200] if post.text else "(brak tekstu)")
    print(f"Polubienia: {post.reaction_count}, Komentarze: {post.comments_count}")
    if i >= 2:
        break
