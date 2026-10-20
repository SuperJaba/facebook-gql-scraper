from pathlib import Path
import facebook_gql_scraper as fgs

COOKIES_PATH = Path(__file__).parent / "facebook_cookies.json"
ctx = fgs.FacebookContext(cookies_file=str(COOKIES_PATH))

post_id = input("Podaj ID posta lub URL: ").strip()
if "facebook.com" in post_id:
    # Wyciąganie ID lub pfbid z URL
    if "pfbid" in post_id:
        import re
        m = re.search(r"pfbid[a-zA-Z0-9]+", post_id)
        if m:
            post = fgs.Post.from_pfbid(ctx, pfbid=m.group(0))
        else:
            post = fgs.Post.from_id(ctx, id=post_id)
    else:
        import re
        m = re.search(r"/posts/(\d+)", post_id)
        post_id = m.group(1) if m else post_id
        post = fgs.Post.from_id(ctx, id=post_id)
else:
    post = fgs.Post.from_id(ctx, id=post_id)

print(f"\nTreść posta: {post.text}")
print(f"Polubienia: {post.reaction_count}, Komentarze: {post.comments_count}\n")

print("--- Komentarze ---")
for comment in post.get_comments():
    print(f"{comment.owner.name if comment.owner else 'Anonim'}: {comment.text}")
    for reply in comment.get_replies():
        print(f"  ↳ {reply.owner.name if reply.owner else 'Anonim'}: {reply.text}")
