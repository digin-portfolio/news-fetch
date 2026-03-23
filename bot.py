import os
import json
import time
import re
import schedule
import requests
import threading
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

TMDB_API_KEY        = os.getenv("TMDB_API_KEY")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
OMDB_API_KEY        = os.getenv("OMDB_API_KEY")
CHANNEL_WATERMARK   = os.getenv("CHANNEL_WATERMARK", "@anime_24hr")
ADMIN_CHAT_ID       = os.getenv("ADMIN_CHAT_ID", "")  # Your Telegram user ID for DM alerts

# Use /tmp on Render (persists during session), local path otherwise
_IS_RENDER      = os.getenv("RENDER", "") != ""
_STATE_DIR      = "/tmp" if _IS_RENDER else "."

SENT_IDS_FILE   = os.path.join(_STATE_DIR, "sent_ids.json")
DIGEST_FILE     = os.path.join(_STATE_DIR, "digest_items.json")
ROTATION_FILE   = os.path.join(_STATE_DIR, "rotation_state.json")
TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_BACKDROP   = "https://image.tmdb.org/t/p/w1280"
TMDB_POSTER     = "https://image.tmdb.org/t/p/w780"
MAX_POSTS_PER_RUN = 5
DAYS_LOOKBACK     = 2
CAPTION_LIMIT     = 1020

CATEGORY_ROTATION = ["movie", "anime", "kdrama", "indian", "series"]

MALAYALAM_OTT = {"SonyLIV", "Manorama MAX", "ZEE5", "Disney+ Hotstar",
                 "Amazon Prime Video", "Netflix", "Hotstar"}

# Language code → human label
LANG_MAP = {
    "ml": ("Malayalam", "Mollywood"),
    "ta": ("Tamil",     "Kollywood"),
    "hi": ("Hindi",     "Bollywood"),
    "te": ("Telugu",    "Tollywood"),
    "ko": ("Korean",    "K-Drama"),
    "ja": ("Japanese",  "Anime"),
    "en": ("English",   "Hollywood"),
    "fr": ("French",    "French Cinema"),
}


def date_from()     : return (date.today() - timedelta(days=DAYS_LOOKBACK)).isoformat()
def date_today()    : return date.today().isoformat()
def date_yesterday(): return (date.today() - timedelta(days=1)).isoformat()
def date_soon()     : return (date.today() + timedelta(days=90)).isoformat()
def date_2weeks()   : return (date.today() + timedelta(days=14)).isoformat()  # Only upcoming within 2 weeks


def is_recent(date_str):
    """Only accept content from yesterday, today, or upcoming 14 days."""
    if not date_str or date_str in ("TBA", "Upcoming", "This Season", ""):
        return True
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        yesterday = date.today() - timedelta(days=1)
        future    = date.today() + timedelta(days=14)
        return yesterday <= d <= future
    except:
        return True


def safe_rating(r):
    try:
        v = float(r)
        return round(v, 1) if v > 0 else None
    except:
        return None


def best_image(item_data):
    if item_data.get("backdrop_path"):
        return TMDB_BACKDROP + item_data["backdrop_path"]
    if item_data.get("poster_path"):
        return TMDB_POSTER + item_data["poster_path"]
    return None


def trim_caption(text):
    return text[:CAPTION_LIMIT - 3] + "..." if len(text) > CAPTION_LIMIT else text


# ─────────────────────────────────────────────
# QUALITY FILTER — Skip low-quality posts
# ─────────────────────────────────────────────

# Words that should never appear in titles we post
BLOCKED_WORDS = [
    "phallus", "penis", "vagina", "porn", "sex tape",
    "xxx", "erotic", "adult film", "onlyfans", "hentai",
    "torture porn", "snuff", "rape", "incest",
]

def passes_quality_filter(item):
    """
    Returns True only if this item is worth posting.
    Skips: no image, no description, low rating, adult/inappropriate content.
    """
    if not item.get("image"):
        return False
    if not item.get("overview") or len(item.get("overview", "")) < 30:
        return False
    # Skip very low-rated items
    rating = safe_rating(item.get("rating"))
    if rating and rating < 4.0:
        return False
    # Skip items with no title
    title = item.get("title", "")
    if not title or len(title) < 2:
        return False
    # Block inappropriate content
    title_lower = title.lower()
    overview_lower = item.get("overview", "").lower()
    if any(w in title_lower or w in overview_lower for w in BLOCKED_WORDS):
        print(f"  🚫 Blocked inappropriate content: {title[:40]}")
        return False
    return True


# ─────────────────────────────────────────────
# PRIORITY LEVEL — Big movie vs indie vs breaking
# ─────────────────────────────────────────────

def get_priority(item):
    """
    Returns priority level: 'high', 'medium', 'low'
    High = big franchise, high rating, trending
    """
    rating   = safe_rating(item.get("rating")) or 0
    tag      = item.get("tag", "")
    has_trailer = bool(item.get("trailer"))
    pop      = item.get("popularity", 0) or 0

    if rating >= 7.5 or tag == "Trending Now" or pop > 100:
        return "high"
    if rating >= 6.0 or has_trailer or tag in ("Just Released", "This Season"):
        return "medium"
    return "low"


def priority_badge(item):
    p = get_priority(item)
    if p == "high":   return "🔥 Big Release"
    if p == "medium": return "📣 Featured"
    return "📰 New"


# ─────────────────────────────────────────────
# SMART NEWS LABEL
# ─────────────────────────────────────────────

def detect_news_label(item):
    has_trailer = bool(item.get("trailer"))
    rd_str      = item.get("release_date", "")
    rd = None
    try:
        rd = datetime.strptime(rd_str[:10], "%Y-%m-%d").date()
    except:
        pass
    today = date.today()

    if has_trailer and rd and rd > today:  return "🎬 Trailer Released"
    if has_trailer and rd and rd <= today: return "🎬 Trailer Out Now"
    if rd and rd <= today:                 return "🎉 Now Streaming / In Theaters"
    if rd and rd <= today + timedelta(7):  return "⚡ Releasing This Week"
    if rd and rd <= today + timedelta(30): return "📅 Release Confirmed"
    if item.get("tag") == "Trending Now":  return "🔥 Trending Right Now"
    if item.get("tag") in ("This Season", "New", "Just Released"): return "🆕 New Release"
    return "📢 Official Announcement"


# ─────────────────────────────────────────────
# PLATFORM DETECTOR
# ─────────────────────────────────────────────

PLATFORM_ICONS = {
    "Netflix":             "Netflix 🔴",
    "Amazon Prime Video":  "Prime Video 🔵",
    "Disney+ Hotstar":     "Hotstar 🟣",
    "Hotstar":             "Hotstar 🟣",
    "Apple TV+":           "Apple TV+ 🍎",
    "Hulu":                "Hulu 💚",
    "HBO":                 "HBO Max 🟤",
    "Max":                 "Max 🟤",
    "SonyLIV":             "SonyLIV 🟠",
    "Manorama MAX":        "Manorama MAX 🔵",
    "ZEE5":                "ZEE5 🟣",
    "Peacock":             "Peacock 🦚",
    "Paramount+":          "Paramount+ 💙",
}

def get_platform(item):
    networks = item.get("networks") or []
    for n in networks:
        if n in PLATFORM_ICONS:
            return PLATFORM_ICONS[n]
        if n:
            return n
    # If released and no network → Theaters
    rd_str = item.get("release_date", "")
    try:
        rd = datetime.strptime(rd_str[:10], "%Y-%m-%d").date()
        if rd <= date.today() and item.get("type", "").endswith("Movie"):
            return "🎟 Theaters"
    except:
        pass
    return None


# ─────────────────────────────────────────────
# HASHTAG GENERATOR — Clean, max 4, short words
# ─────────────────────────────────────────────

def make_hashtags(item):
    tags = []

    # 1. Clean short title tag
    title = item.get("title", "")
    # Keep only first 2 words of title for hashtag
    short_title = "".join(title.split()[:2])
    short_title = re.sub(r"[^a-zA-Z0-9]", "", short_title)
    if short_title:
        tags.append(short_title)

    # 2. First genre (single word)
    genres = item.get("genres") or []
    if genres:
        genre_tag = re.sub(r"[^a-zA-Z0-9]", "", genres[0])
        if genre_tag and genre_tag not in tags:
            tags.append(genre_tag)

    # 3. Industry/type tag
    itype = item.get("type", "")
    if "Movie" in itype:      tags.append("NewMovie")
    elif "Anime" in itype:    tags.append("Anime")
    elif "K-Drama" in itype:  tags.append("KDrama")
    elif "Series" in itype:   tags.append("WebSeries")

    # 4. Language tag if non-English
    lang = item.get("language", "")
    if lang and lang not in ("en", ""):
        lang_label = LANG_MAP.get(lang, ("", ""))[1]
        if lang_label:
            tags.append(re.sub(r"[^a-zA-Z0-9]", "", lang_label))

    # Keep unique, max 4
    seen, unique = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)

    return " ".join(f"#{t}" for t in unique[:4])


# ─────────────────────────────────────────────
# STORAGE
# ─────────────────────────────────────────────

def load_sent():
    if os.path.exists(SENT_IDS_FILE):
        with open(SENT_IDS_FILE) as f:
            return json.load(f)
    return []

def save_sent(ids):
    with open(SENT_IDS_FILE, "w") as f:
        json.dump(ids[-5000:], f)

def load_digest():
    today = date.today().isoformat()
    if os.path.exists(DIGEST_FILE):
        with open(DIGEST_FILE) as f:
            data = json.load(f)
        if data.get("date") == today:
            return data.get("items", [])
    return []

def save_digest(items):
    with open(DIGEST_FILE, "w") as f:
        json.dump({"date": date.today().isoformat(), "items": items}, f)

def get_next_category():
    state = {"index": 0}
    if os.path.exists(ROTATION_FILE):
        with open(ROTATION_FILE) as f:
            state = json.load(f)
    idx = state.get("index", 0) % len(CATEGORY_ROTATION)
    with open(ROTATION_FILE, "w") as f:
        json.dump({"index": idx + 1}, f)
    return CATEGORY_ROTATION[idx]


# ─────────────────────────────────────────────
# OMDB — IMDb Rating
# ─────────────────────────────────────────────

def get_imdb_rating(title, year=None):
    if not OMDB_API_KEY:
        return None
    try:
        params = {"apikey": OMDB_API_KEY, "t": title}
        if year:
            params["y"] = year
        r    = requests.get("https://www.omdbapi.com/", params=params, timeout=8)
        data = r.json()
        if data.get("Response") == "True" and data.get("imdbRating") not in (None, "N/A"):
            return data["imdbRating"]
    except:
        pass
    return None


# ─────────────────────────────────────────────
# TMDB HELPERS
# ─────────────────────────────────────────────

def tmdb(endpoint, params=None):
    p = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if params:
        p.update(params)
    try:
        r = requests.get(f"{TMDB_BASE}/{endpoint}", params=p, timeout=10)
        return r.json().get("results", [])
    except Exception as e:
        print(f"[TMDB] Error {endpoint}: {e}")
        return []

def get_movie_details(movie_id):
    try:
        r    = requests.get(f"{TMDB_BASE}/movie/{movie_id}", params={
            "api_key": TMDB_API_KEY, "append_to_response": "credits,videos"}, timeout=10)
        data = r.json()
        crew    = data.get("credits", {}).get("crew", [])
        videos  = data.get("videos", {}).get("results", [])
        trailer = next((v for v in videos if v["type"] == "Trailer" and v["site"] == "YouTube"), None)
        return {
            "director":   next((c["name"] for c in crew if c["job"] == "Director"), None),
            "cast":       [c["name"] for c in data.get("credits", {}).get("cast", [])[:3]],
            "genres":     [g["name"] for g in data.get("genres", [])],
            "trailer":    f"https://youtu.be/{trailer['key']}" if trailer else None,
            "runtime":    data.get("runtime"),
            "tagline":    data.get("tagline", ""),
            "popularity": data.get("popularity", 0),
            "language":   data.get("original_language", ""),
        }
    except:
        return {}

def get_tv_details(tv_id):
    try:
        r    = requests.get(f"{TMDB_BASE}/tv/{tv_id}", params={
            "api_key": TMDB_API_KEY, "append_to_response": "credits,videos"}, timeout=10)
        data = r.json()
        videos  = data.get("videos", {}).get("results", [])
        trailer = next((v for v in videos if v["type"] == "Trailer" and v["site"] == "YouTube"), None)
        return {
            "cast":       [c["name"] for c in data.get("credits", {}).get("cast", [])[:3]],
            "genres":     [g["name"] for g in data.get("genres", [])],
            "networks":   [n["name"] for n in data.get("networks", [])[:2]],
            "trailer":    f"https://youtu.be/{trailer['key']}" if trailer else None,
            "tagline":    data.get("tagline", ""),
            "seasons":    data.get("number_of_seasons"),
            "popularity": data.get("popularity", 0),
            "language":   data.get("original_language", ""),
        }
    except:
        return {}


# ─────────────────────────────────────────────
# FETCHERS
# ─────────────────────────────────────────────

def fetch_movies():
    items = []
    for tag, gte, lte, sort, ep in [
        ("Upcoming",     date_today(), date_2weeks(), "primary_release_date.asc",  "movie/upcoming"),
        ("Just Released",date_yesterday(), date_today(), "primary_release_date.desc", "discover/movie"),
    ]:
        for m in tmdb(ep, {"primary_release_date.gte": gte,
                           "primary_release_date.lte": lte, "sort_by": sort})[:8]:
            rd = m.get("release_date", "")
            if not is_recent(rd) or not (m.get("poster_path") or m.get("backdrop_path")):
                continue
            details = get_movie_details(m["id"])
            year    = rd[:4] if rd else None
            imdb    = get_imdb_rating(m["title"], year)
            item = {
                "id": f"movie_{tag[:2]}_{m['id']}", "title": m["title"],
                "overview": m.get("overview", ""), "image": best_image(m),
                "release_date": rd, "type": "🎬 Movie", "tag": tag,
                "rating": imdb or safe_rating(m.get("vote_average")),
                "rating_source": "IMDb" if imdb else "TMDB",
                "category": "movie", **details,
            }
            items.append(item)
    return items


def fetch_indian_movies():
    items = []
    for lang_code, label in [("ml","🎬 Malayalam Movie"),("ta","🎬 Tamil Movie"),
                               ("hi","🎬 Bollywood"),("te","🎬 Telugu Movie")]:
        for tag, gte, lte, sort in [
            ("Upcoming",     date_today(), date_2weeks(), "primary_release_date.asc"),
            ("Just Released",date_yesterday(), date_today(), "primary_release_date.desc"),
        ]:
            for m in tmdb("discover/movie", {
                "with_original_language": lang_code, "with_origin_country": "IN",
                "primary_release_date.gte": gte, "primary_release_date.lte": lte, "sort_by": sort,
            })[:4]:
                rd = m.get("release_date", "")
                if not is_recent(rd) or not (m.get("poster_path") or m.get("backdrop_path")):
                    continue
                details = get_movie_details(m["id"])
                year    = rd[:4] if rd else None
                imdb    = get_imdb_rating(m["title"], year)
                networks = details.get("networks") or []
                ott_note = next((n for n in networks if n in MALAYALAM_OTT), "") if lang_code == "ml" else ""
                item = {
                    "id": f"indian_{lang_code}_{tag[:2]}_{m['id']}", "title": m["title"],
                    "overview": m.get("overview", ""), "image": best_image(m),
                    "release_date": rd, "type": label, "tag": tag, "language": lang_code,
                    "rating": imdb or safe_rating(m.get("vote_average")),
                    "rating_source": "IMDb" if imdb else "TMDB",
                    "ott_note": ott_note, "category": "indian", **details,
                }
                items.append(item)
        time.sleep(0.3)
    return items


def fetch_kdramas():
    items = []
    for s in tmdb("discover/tv", {
        "with_origin_country": "KR", "sort_by": "first_air_date.desc",
        "first_air_date.gte": date_yesterday(), "first_air_date.lte": date_2weeks(),
    })[:8]:
        rd = s.get("first_air_date", "")
        if not is_recent(rd) or not (s.get("poster_path") or s.get("backdrop_path")):
            continue
        details = get_tv_details(s["id"])
        items.append({
            "id": f"kdrama_{s['id']}", "title": s.get("name") or s.get("original_name",""),
            "overview": s.get("overview",""), "image": best_image(s),
            "release_date": rd, "type": "🇰🇷 K-Drama", "tag": "New", "language": "ko",
            "rating": safe_rating(s.get("vote_average")), "rating_source": "TMDB",
            "category": "kdrama", **details,
        })
    return items


def fetch_web_series():
    items = []
    seen  = set()
    for s in tmdb("discover/tv", {
        "sort_by": "first_air_date.desc", "first_air_date.gte": date_yesterday(),
        "first_air_date.lte": date_2weeks(), "with_type": "4",
    })[:10]:
        if s.get("origin_country") and "KR" in s["origin_country"]:
            continue
        rd = s.get("first_air_date", "")
        if not is_recent(rd) or not (s.get("poster_path") or s.get("backdrop_path")):
            continue
        seen.add(s["id"])
        details = get_tv_details(s["id"])
        items.append({
            "id": f"series_{s['id']}", "title": s.get("name") or s.get("original_name",""),
            "overview": s.get("overview",""), "image": best_image(s),
            "release_date": rd, "type": "📺 Web Series", "tag": "New",
            "rating": safe_rating(s.get("vote_average")), "rating_source": "TMDB",
            "category": "series", **details,
        })
    for s in tmdb("trending/tv/week")[:8]:
        if s["id"] in seen or (s.get("origin_country") and "KR" in s["origin_country"]):
            continue
        rd = s.get("first_air_date", "")
        if not is_recent(rd) or not (s.get("poster_path") or s.get("backdrop_path")):
            continue
        details = get_tv_details(s["id"])
        items.append({
            "id": f"trending_{s['id']}", "title": s.get("name") or s.get("original_name",""),
            "overview": s.get("overview",""), "image": best_image(s),
            "release_date": rd, "type": "📺 Web Series", "tag": "Trending Now",
            "rating": safe_rating(s.get("vote_average")), "rating_source": "TMDB",
            "category": "series", **details,
        })
    return items


def fetch_anime():
    """
    Only fetch anime that:
    - Started airing yesterday or today (brand new episodes)
    - Has a specific confirmed air date within next 14 days
    Skips anything with vague "Upcoming" dates — those are not today's news.
    """
    items = []
    today     = date.today()
    yesterday = today - timedelta(days=1)
    in_14days = today + timedelta(days=14)

    try:
        # Currently airing — only include if first aired yesterday or today
        r = requests.get("https://api.jikan.moe/v4/seasons/now", params={"limit": 25}, timeout=10)
        for a in r.json().get("data", []):
            img = (a.get("images") or {}).get("jpg", {}).get("large_image_url")
            if not img:
                continue
            aired_from = (a.get("aired") or {}).get("from", "")
            if not aired_from:
                continue  # No date = skip
            try:
                rd_date = datetime.strptime(aired_from[:10], "%Y-%m-%d").date()
            except:
                continue
            # Only include if aired very recently (yesterday or today)
            if rd_date < yesterday:
                continue
            rd = aired_from[:10]
            items.append({
                "id": f"anime_{a['mal_id']}", "title": a.get("title_english") or a.get("title",""),
                "overview": a.get("synopsis",""), "image": img, "language": "ja",
                "release_date": rd, "type": "✨ Anime", "tag": "New Episode",
                "rating": safe_rating(a.get("score")), "rating_source": "MAL",
                "genres": [g["name"] for g in (a.get("genres") or [])],
                "cast":   [s["name"] for s in (a.get("studios") or [])[:2]],
                "episodes": a.get("episodes"), "category": "anime",
                "popularity": a.get("members", 0),
            })

        time.sleep(1)

        # Upcoming — only include if specific air date is within 14 days
        r2 = requests.get("https://api.jikan.moe/v4/seasons/upcoming", params={"limit": 20}, timeout=10)
        for a in r2.json().get("data", []):
            img = (a.get("images") or {}).get("jpg", {}).get("large_image_url")
            if not img:
                continue
            aired_from = (a.get("aired") or {}).get("from", "")
            if not aired_from:
                continue  # No confirmed date = skip (not today's news)
            try:
                rd_date = datetime.strptime(aired_from[:10], "%Y-%m-%d").date()
            except:
                continue
            # Only if airing within next 14 days
            if not (today <= rd_date <= in_14days):
                continue
            rd = aired_from[:10]
            items.append({
                "id": f"anime_up_{a['mal_id']}", "title": a.get("title_english") or a.get("title",""),
                "overview": a.get("synopsis",""), "image": img, "language": "ja",
                "release_date": rd, "type": "✨ Anime", "tag": "Coming Soon",
                "rating": None, "rating_source": "MAL",
                "genres": [g["name"] for g in (a.get("genres") or [])],
                "cast":   [s["name"] for s in (a.get("studios") or [])[:2]],
                "episodes": a.get("episodes"), "category": "anime",
                "popularity": a.get("members", 0),
            })
    except Exception as e:
        print(f"[Anime] Error: {e}")
    return items




def fetch_all():
    all_items = []
    for fn in [fetch_movies, fetch_indian_movies, fetch_kdramas, fetch_web_series, fetch_anime]:
        all_items.extend(fn())
    seen, deduped = set(), []
    for item in all_items:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)
    return deduped


# ─────────────────────────────────────────────
# AI — GROQ CALLS
# ─────────────────────────────────────────────

def groq(prompt, max_tokens=100):
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0.8},
            timeout=15,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except:
        return ""


def ai_story(overview, title):
    """Rewrite description: punchy, emotional, 1-2 sentences, max 130 chars."""
    if not overview or len(overview) < 40:
        return overview
    result = groq(
        f"Rewrite this in 1-2 punchy, emotional sentences (max 130 chars). "
        f"Make it a curiosity hook. No quotes. Title: {title}\n\n{overview[:400]}", 80)
    return result if result else overview[:130]


def ai_why_it_matters(item):
    """Generate 1 line: why this release matters right now."""
    title   = item.get("title", "")
    itype   = item.get("type", "")
    rating  = item.get("rating", "")
    tag     = item.get("tag", "")
    genres  = ", ".join(item.get("genres") or [])
    result  = groq(
        f"Write ONE short sentence (max 80 chars) explaining why '{title}' ({itype}, {genres}) "
        f"is worth attention right now. Status: {tag}. Rating: {rating}. "
        f"Be specific and insightful. No hype words like 'amazing'. No quotes.", 60)
    return result if result else ""


# ─────────────────────────────────────────────
# CAPTION BUILDER — Premium Format
# ─────────────────────────────────────────────

def build_caption(item):
    # ── YouTube trailer gets its own clean format
    if item.get("category") == "youtube":
        title       = item["title"]
        channel     = item.get("channel_name", "")
        trailer     = item.get("trailer", "")
        pub_date    = item.get("release_date", "")
        hashtags    = make_hashtags(item)
        lines = [
            f"🎬 Trailer Released  🔥 Big Release",
            "",
            f"<b>{title}</b>",
            "",
            f"📺 <b>Channel:</b> {channel}",
            f"📅 <b>Published:</b> {pub_date}",
            "",
            f"🎥 <b>Watch Now:</b> {trailer}",
            "",
            hashtags,
            f"— {CHANNEL_WATERMARK}",
        ]
        return trim_caption("\n".join(lines))

    title      = item["title"]
    itype      = item["type"]
    rel_date   = item.get("release_date", "TBA")
    rating     = item.get("rating")
    r_src      = item.get("rating_source", "")
    trailer    = item.get("trailer")
    lang_code  = item.get("language", "")
    ott_note   = item.get("ott_note", "")

    news_label = detect_news_label(item)
    badge      = priority_badge(item)
    platform   = get_platform(item) or ott_note or None
    hashtags   = make_hashtags(item)

    # Language + Industry
    lang_name, industry = LANG_MAP.get(lang_code, ("", ""))

    # AI-powered lines
    story        = ai_story(item.get("overview", ""), title)
    why_matters  = ai_why_it_matters(item)

    # Extra info
    genres   = item.get("genres") or []
    cast     = item.get("cast") or []
    director = item.get("director", "")
    runtime  = item.get("runtime")
    episodes = item.get("episodes")
    seasons  = item.get("seasons")

    # ── Info block as Telegram blockquote (clean bullet points)
    info_lines = []
    info_lines.append(f"• Type: {itype}")
    if lang_name and industry:
        info_lines.append(f"• Language: {lang_name}  ·  Industry: {industry}")
    elif lang_name:
        info_lines.append(f"• Language: {lang_name}")
    info_lines.append(f"• Release: {rel_date}")
    if platform:
        info_lines.append(f"• Platform: {platform}")
    if genres:
        info_lines.append(f"• Genre: {' · '.join(genres[:3])}")
    if cast:
        info_lines.append(f"• Cast: {', '.join(cast[:3])}")
    if director:
        info_lines.append(f"• Director: {director}")

    # Stats line
    stats_parts = []
    if runtime:    stats_parts.append(f"{runtime} min")
    if episodes:   stats_parts.append(f"{episodes} eps")
    if seasons:    stats_parts.append(f"{seasons} season{'s' if seasons and seasons > 1 else ''}")
    if rating:
        icon = "🍅" if r_src == "IMDb" else "⭐"
        stats_parts.append(f"{icon} {rating}/10")
    if stats_parts:
        info_lines.append(f"• {' · '.join(stats_parts)}")

    # Wrap info block in Telegram blockquote using <blockquote> tag
    blockquote = "<blockquote>" + "\n".join(info_lines) + "</blockquote>"

    lines = []

    # News label — clean, no badge clutter
    lines.append(f"<b>{news_label}</b>")
    lines.append("")

    # Bold title — stands out
    lines.append(f"<b>{title}</b>")
    lines.append("")

    # Blockquote info block
    lines.append(blockquote)

    # Story — 3-4 lines, no label, just the text
    if story:
        lines.append("")
        lines.append(story)

    # Why watch — one punchy line
    if why_matters:
        lines.append("")
        lines.append(f"💡 {why_matters}")

    # Footer
    lines.append("")
    lines.append(f"{hashtags}")
    lines.append(f"— {CHANNEL_WATERMARK}")

    return trim_caption("\n".join(lines))


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_photo(caption, image_url, buttons=None):
    base    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "parse_mode": "HTML"}
    if image_url:
        payload["photo"]   = image_url
        payload["caption"] = caption
        endpoint = f"{base}/sendPhoto"
    else:
        payload["text"] = caption
        endpoint = f"{base}/sendMessage"
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "url": url}] for label, url in buttons]
        }
    r = requests.post(endpoint, json=payload, timeout=15)
    return r.json()

def send_message(text):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    return r.json()

def send_dm(text):
    """Send a private DM alert to the admin (you)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass



# ─────────────────────────────────────────────
# DAILY DIGEST
# ─────────────────────────────────────────────

def send_daily_digest():
    items = load_digest()
    if not items:
        print("[Digest] No items today.")
        return
    today_str = datetime.now().strftime("%B %d, %Y")
    lines = [f"📋 <b>Today's Entertainment Roundup</b>", f"🗓 {today_str}", ""]
    for i, item in enumerate(items[:8], 1):
        label  = detect_news_label(item)
        rating = f" ⭐{item['rating']}" if item.get("rating") else ""
        lines.append(f"{i}. <b>{item['title']}</b> {item['type']}{rating}")
        lines.append(f"   {label} • 📅 {item.get('release_date','TBA')}")
    lines += ["", f"— {CHANNEL_WATERMARK}"]
    send_message("\n".join(lines))
    print(f"[Digest] Sent with {len(items)} items.")


def send_weekend_watchlist():
    if datetime.now().weekday() != 4:
        return
    all_items = fetch_all()
    picks = {}
    for item in all_items:
        if not passes_quality_filter(item):
            continue
        cat = item.get("category", "other")
        if cat not in picks:
            picks[cat] = item
        elif safe_rating(item.get("rating") or 0) and \
             safe_rating(item.get("rating") or 0) > safe_rating(picks[cat].get("rating") or 0):
            picks[cat] = item

    if not picks:
        return
    lines = ["🍿 <b>Weekend Watch List</b>",
             f"🗓 {datetime.now().strftime('%B %d, %Y')} — Curated just for you!", ""]
    for i, (_, item) in enumerate(list(picks.items())[:5], 1):
        rating    = f" ⭐{item['rating']}/10" if item.get("rating") else ""
        genre_str = " • ".join((item.get("genres") or [])[:2])
        lines.append(f"{i}. <b>{item['title']}</b> {item['type']}")
        lines.append(f"   📅 {item.get('release_date','TBA')}  {genre_str}{rating}")
    lines += ["", "#WeekendWatch #WhatToWatch", f"— {CHANNEL_WATERMARK}"]
    send_message("\n".join(lines))
    print("[Weekend] Sent watchlist.")


# ─────────────────────────────────────────────
# MAIN RUN
# ─────────────────────────────────────────────

def run_bot():
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bot cycle starting...")

    sent_ids = load_sent()
    category = get_next_category()
    print(f"🎯 Category: {category.upper()}")

    pool = {
        "movie":  fetch_movies,
        "indian": fetch_indian_movies,
        "kdrama": fetch_kdramas,
        "series": fetch_web_series,
        "anime":  fetch_anime,
    }.get(category, fetch_movies)()

    # Deduplicate
    seen, deduped = set(), []
    for item in pool:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)

    # Quality filter
    quality = [i for i in deduped if passes_quality_filter(i)]
    new_items = [i for i in quality if i["id"] not in sent_ids]
    print(f"Fetched: {len(deduped)} | Quality: {len(quality)} | New: {len(new_items)}")

    if not new_items:
        print("Nothing new this cycle.")
        # Track how many cycles in a row had nothing
        empty_state_file = os.path.join(_STATE_DIR, "empty_cycles.json")
        try:
            emp = json.load(open(empty_state_file)) if os.path.exists(empty_state_file) else {"count": 0, "last_dm": ""}
        except:
            emp = {"count": 0, "last_dm": ""}
        emp["count"] = emp.get("count", 0) + 1
        # Send DM alert every 5 empty cycles (2.5 hours)
        if emp["count"] >= 5 and emp.get("last_dm") != date_today():
            send_dm(
                f"⚠️ <b>Bot Alert</b>\n\n"
                f"No new <b>{category.upper()}</b> content found for {emp['count']} cycles in a row.\n"
                f"Date range: {date_from()} → {date_soon()}\n\n"
                f"The bot is still running ✅ — just waiting for new content."
            )
            emp["last_dm"] = date_today()
            emp["count"]   = 0
            print("  📩 DM alert sent to admin.")
        json.dump(emp, open(empty_state_file, "w"))
        return

    # Sort: high priority first, then newest
    def sort_key(i):
        p = {"high": "0", "medium": "1", "low": "2"}.get(get_priority(i), "2")
        rd = i.get("release_date", "")
        if rd in ("TBA", "Upcoming", "This Season", ""):
            rd = "0000"
        return f"{p}_{rd}"
    new_items.sort(key=sort_key, reverse=True)

    digest = load_digest()
    posted = 0

    for item in new_items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        print(f"\n→ [{priority_badge(item)}] {item['title']} ({item.get('release_date','?')})")
        caption = build_caption(item)
        buttons = []
        if item.get("trailer"):
            buttons.append(("▶️ Watch Trailer", item["trailer"]))
        result  = send_photo(caption, item.get("image"), buttons=buttons or None)
        if result.get("ok"):
            sent_ids.append(item["id"])
            save_sent(sent_ids)
            digest.append(item)
            save_digest(digest)
            print("  ✅ Sent!")
            posted += 1
            # Reset empty cycle counter on success
            try:
                json.dump({"count": 0, "last_dm": ""}, open("empty_cycles.json", "w"))
            except:
                pass
        else:
            print(f"  ❌ Failed: {result.get('description','Unknown')}")
        time.sleep(5)

    print(f"\n{'='*50}")
    print(f"Done ✅  Posted {posted}. Next run in 30 mins.")


# ─────────────────────────────────────────────
# YOUTUBE MONITOR — Runs every 30 mins
# ─────────────────────────────────────────────



# ─────────────────────────────────────────────
# SMART TRAILER DETECTOR
# Watches TMDB upcoming movies/shows and posts
# the moment a new trailer becomes available
# ─────────────────────────────────────────────

TRAILER_STATE_FILE = os.path.join(_STATE_DIR, "trailer_state.json")

def load_trailer_state():
    """Tracks which items already had a trailer last time we checked."""
    if os.path.exists(TRAILER_STATE_FILE):
        with open(TRAILER_STATE_FILE) as f:
            return json.load(f)
    return {}

def save_trailer_state(state):
    with open(TRAILER_STATE_FILE, "w") as f:
        json.dump(state, f)


def run_trailer_detector():
    """
    Checks upcoming movies + anime for NEW trailer drops.
    If an item had no trailer before but now has one → post it immediately.
    Runs every 2 hours.
    """
    print(f"\n{'─'*40}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎬 Trailer detector running...")

    state    = load_trailer_state()
    sent_ids = load_sent()
    digest   = load_digest()
    posted   = 0

    # Check upcoming movies — wider window for trailer detection
    upcoming = []
    future_6mo = (date.today() + timedelta(days=180)).isoformat()
    for m in tmdb("movie/upcoming", {
        "primary_release_date.gte": date_today(),
        "primary_release_date.lte": future_6mo,
        "sort_by": "popularity.desc",
    })[:25]:
        if not m.get("poster_path") and not m.get("backdrop_path"):
            continue
        upcoming.append(("movie", m["id"], m["title"], m))

    # Also check trending/popular for trailer drops
    for m in tmdb("movie/popular")[:10]:
        if not m.get("poster_path") and not m.get("backdrop_path"):
            continue
        # Only if releasing in next 6 months
        rd = m.get("release_date","")
        if rd:
            try:
                rd_date = datetime.strptime(rd[:10], "%Y-%m-%d").date()
                if rd_date < date.today() or rd_date > date.today() + timedelta(days=180):
                    continue
            except:
                continue
        if not any(x[1] == m["id"] for x in upcoming):
            upcoming.append(("movie", m["id"], m["title"], m))

    # Check upcoming anime from Jikan (top upcoming by popularity)
    try:
        r = requests.get("https://api.jikan.moe/v4/seasons/upcoming",
                         params={"limit": 15}, timeout=10)
        for a in r.json().get("data", [])[:15]:
            upcoming.append(("anime", a["mal_id"], a.get("title_english") or a.get("title",""), a))
        time.sleep(0.5)
    except:
        pass

    print(f"  Checking {len(upcoming)} upcoming titles for new trailers...")
    for kind, item_id, title, raw in upcoming:
        state_key = f"{kind}_{item_id}"

        if kind == "movie":
            # Skip movies that already released in the past
            rd = raw.get("release_date", "")
            if rd:
                try:
                    rd_date = datetime.strptime(rd[:10], "%Y-%m-%d").date()
                    if rd_date < date.today() - timedelta(days=1):
                        continue  # Already released, skip
                except:
                    pass

            details  = get_movie_details(item_id)
            trailer  = details.get("trailer")
            had_before = state.get(state_key, {}).get("had_trailer", False)

            # Update state
            state[state_key] = {"had_trailer": bool(trailer), "title": title}

            # New trailer just dropped!
            if trailer and not had_before:
                post_id = f"trailer_drop_{item_id}"
                if post_id in sent_ids:
                    continue

                print(f"\n🎬 NEW TRAILER: {title}")
                rd = raw.get("release_date", "")
                year = rd[:4] if rd else ""
                imdb = get_imdb_rating(title, year)

                item = {
                    "id":           post_id,
                    "title":        title,
                    "overview":     raw.get("overview", ""),
                    "image":        best_image(raw),
                    "release_date": rd,
                    "type":         "🎬 Movie",
                    "tag":          "Trailer Released",
                    "rating":       imdb or safe_rating(raw.get("vote_average")),
                    "rating_source": "IMDb" if imdb else "TMDB",
                    "trailer":      trailer,
                    "category":     "movie",
                    **details,
                }

                caption = build_caption(item)
                buttons = [("▶️ Watch Trailer", trailer)]
                result  = send_photo(caption, item.get("image"), buttons=buttons)
                if result.get("ok"):
                    sent_ids.append(post_id)
                    save_sent(sent_ids)
                    digest.append(item)
                    save_digest(digest)
                    print("  ✅ Trailer post sent!")
                    posted += 1
                else:
                    print(f"  ❌ Failed: {result.get('description','')}")
                time.sleep(4)

        elif kind == "anime":
            # For anime, check if it now has a trailer via MAL
            had_before = state.get(state_key, {}).get("had_trailer", False)
            trailer_url = None
            try:
                r = requests.get(f"https://api.jikan.moe/v4/anime/{item_id}",
                                 timeout=8)
                data  = r.json().get("data", {})
                promo = data.get("trailer", {})
                if promo and promo.get("youtube_id"):
                    trailer_url = f"https://youtu.be/{promo['youtube_id']}"
                time.sleep(0.3)
            except:
                pass

            state[state_key] = {"had_trailer": bool(trailer_url), "title": title}

            if trailer_url and not had_before:
                post_id = f"anime_trailer_{item_id}"
                if post_id in sent_ids:
                    continue

                print(f"\n✨ NEW ANIME TRAILER: {title}")
                img = (raw.get("images") or {}).get("jpg", {}).get("large_image_url")
                item = {
                    "id":           post_id,
                    "title":        title,
                    "overview":     raw.get("synopsis", ""),
                    "image":        img,
                    "release_date": "Upcoming",
                    "type":         "✨ Anime",
                    "tag":          "Trailer Released",
                    "rating":       safe_rating(raw.get("score")),
                    "rating_source": "MAL",
                    "trailer":      trailer_url,
                    "genres":       [g["name"] for g in (raw.get("genres") or [])],
                    "cast":         [s["name"] for s in (raw.get("studios") or [])[:2]],
                    "category":     "anime",
                }
                caption = build_caption(item)
                buttons = [("▶️ Watch Trailer", trailer_url)]
                result  = send_photo(caption, item.get("image"), buttons=buttons)
                if result.get("ok"):
                    sent_ids.append(post_id)
                    save_sent(sent_ids)
                    digest.append(item)
                    save_digest(digest)
                    print("  ✅ Anime trailer post sent!")
                    posted += 1
                else:
                    print(f"  ❌ Failed: {result.get('description','')}")
                time.sleep(4)

        if posted >= 3:
            break

    save_trailer_state(state)
    print(f"  Trailer check done. {'Posted ' + str(posted) + ' new trailer(s).' if posted else 'No new trailers.'}")


# ─────────────────────────────────────────────
# GOOGLE NEWS RSS — BREAKING NEWS MODULE
# Fetches real-time news: box office, teasers,
# postponements, cast reveals, etc.
# ─────────────────────────────────────────────

import xml.etree.ElementTree as ET
import html
import hashlib

NEWS_SENT_FILE        = os.path.join(_STATE_DIR, "news_sent.json")
NEWS_SENT_TITLES_FILE = os.path.join(_STATE_DIR, "news_sent_titles.json")

# Google News RSS queries — one per topic
NEWS_QUERIES = [
    # 🇮🇳 Indian Cinema
    ("Indian Cinema",     "bollywood+OR+kollywood+OR+mollywood+OR+tollywood+movie+trailer+OR+teaser+OR+release+OR+box+office"),
    ("Malayalam Movies",  "malayalam+movie+2026+trailer+OR+release+OR+teaser+OR+box+office"),
    ("Tamil Movies",      "tamil+movie+2026+trailer+OR+teaser+OR+release+OR+box+office"),
    ("Telugu Movies",     "telugu+movie+2026+trailer+OR+teaser+OR+release+OR+box+office"),
    ("Bollywood",         "bollywood+movie+2026+trailer+OR+teaser+OR+release+OR+box+office"),
    # ✨ Anime
    ("Anime News",        "anime+2026+trailer+OR+teaser+OR+release+OR+season+OR+episode"),
    # 🇰🇷 K-Drama
    ("K-Drama News",      "kdrama+OR+korean+drama+2026+trailer+OR+release+OR+Netflix+OR+cast"),
    # 🎬 Hollywood
    ("Hollywood News",    "hollywood+movie+2026+trailer+OR+teaser+OR+release+date+OR+box+office"),
]

# Keywords that make a news item worth posting
WORTHY_KEYWORDS = [
    "trailer", "teaser", "release", "box office", "crore", "million",
    "postponed", "delayed", "confirmed", "official", "first look",
    "cast", "announced", "premiere", "streaming", "ott", "netflix",
    "amazon", "disney", "review", "collection", "record", "blockbuster",
    "season", "episode", "renewal", "cancelled",
]

# Skip these low-quality sources
SKIP_SOURCES = ["quora", "reddit", "wikipedia", "imdb.com/user"]


def load_news_sent():
    if os.path.exists(NEWS_SENT_FILE):
        with open(NEWS_SENT_FILE) as f:
            return json.load(f)
    return []

def save_news_sent(ids):
    with open(NEWS_SENT_FILE, "w") as f:
        json.dump(ids[-5000:], f)

def load_news_titles():
    if os.path.exists(NEWS_SENT_TITLES_FILE):
        with open(NEWS_SENT_TITLES_FILE) as f:
            return json.load(f)
    return []

def save_news_titles(titles):
    with open(NEWS_SENT_TITLES_FILE, "w") as f:
        json.dump(titles[-500:], f)

def is_similar_to_sent(title, news_sent_titles):
    """Check if a title is too similar to already-sent news."""
    t = re.sub(r'[^a-z0-9 ]', '', title.lower()).strip()
    words_t = set(t.split())
    for sent_title in news_sent_titles[-100:]:  # Check last 100
        s = re.sub(r'[^a-z0-9 ]', '', sent_title.lower()).strip()
        words_s = set(s.split())
        # If 70%+ words match — it's the same story
        if len(words_t) > 0 and len(words_s) > 0:
            overlap = len(words_t & words_s) / max(len(words_t), len(words_s))
            if overlap >= 0.7:
                return True
    return False


def fetch_google_news(query, label):
    """Fetch news from Google News RSS for a query."""
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []

        root    = ET.fromstring(r.content)
        channel = root.find("channel")
        if channel is None:
            return []

        items   = []
        for item in channel.findall("item")[:8]:
            title_el = item.find("title")
            link_el  = item.find("link")
            desc_el  = item.find("description")
            pub_el   = item.find("pubDate")
            source_el= item.find("source")

            if title_el is None or link_el is None:
                continue

            title   = html.unescape(title_el.text or "")
            link    = link_el.text or ""
            desc    = html.unescape(desc_el.text or "") if desc_el is not None else ""
            pub_str = pub_el.text or "" if pub_el is not None else ""
            source  = source_el.text or "" if source_el is not None else ""

            # Skip low quality sources
            if any(s in link.lower() for s in SKIP_SOURCES):
                continue

            # Must contain worthy keywords
            combined = (title + " " + desc).lower()
            if not any(kw in combined for kw in WORTHY_KEYWORDS):
                continue

            # Only today's news — check pub date
            if pub_str:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt   = parsedate_to_datetime(pub_str)
                    pub_date = pub_dt.date()
                    if pub_date < date.today() - timedelta(days=1):
                        continue  # Skip old news
                except:
                    pass

            # Create unique ID from title hash
            # Normalize title for dedup — removes source name, punctuation, case
            normalized = re.sub(r'[^a-z0-9 ]', '', title.lower())
            normalized = re.sub(r'\s+', ' ', normalized).strip()
            # Also remove common source suffixes like "- Koimoi" "| FilmiBeat"
            normalized = re.sub(r'[-|]\s*\w+\s*$', '', normalized).strip()
            uid = "news_" + hashlib.md5(normalized.encode()).hexdigest()[:12]

            # Clean up description
            clean_desc = re.sub(r"<[^>]+>", "", desc).strip()[:300]

            items.append({
                "id":      uid,
                "title":   title,
                "desc":    clean_desc,
                "link":    link,
                "source":  source,
                "label":   label,
                "pub_str": pub_str[:16] if pub_str else "",
            })

        return items
    except Exception as e:
        print(f"  [News] Error {label}: {e}")
        return []


def build_news_caption(item):
    """Build clean caption for a news post."""
    title   = item["title"]
    desc    = item.get("desc", "")
    source  = item.get("source", "")
    label   = item.get("label", "📰 News")
    pub_str = item.get("pub_str", "")

    # AI rewrite the description to be punchy
    if desc and len(desc) > 40:
        story = groq(
            f"Rewrite this news in 2-3 clear sentences. Be factual and engaging. No hype:\n\n{title}\n{desc}",
            100
        )
    else:
        story = desc or title

    # Clean hashtags from title
    words    = re.findall(r"[A-Za-z0-9]+", title)
    hashtags = " ".join(f"#{w}" for w in words[:3] if len(w) > 3)

    lines = [
        f"<b>📰 {label}</b>",
        "",
        f"<b>{title}</b>",
        "",
    ]

    if story and story != title:
        lines.append(story)
        lines.append("")

    if source:
        lines.append(f"📌 Source: {source}")

    lines.append("")
    lines.append(hashtags)
    lines.append(f"— {CHANNEL_WATERMARK}")

    return trim_caption("\n".join(lines))


def extract_topic(title):
    """Extract core movie/show name from a news title."""
    # Remove source name after dash/pipe
    t = re.sub(r'[\s]*[-\u2013|:][\s]*\w[\w\s]*$', '', title).strip()
    # Remove common news words
    t = re.sub(r'(box office|collection|day \d+|week \d+|crore|million|billion|'
               r'trailer|teaser|review|analysis|verdict|opening|worldwide|overseas|'
               r'record|blockbuster|hits|crosses|earns|makes|grosses|total|'
               r'first|second|third|look|official|release|date|confirmed|'
               r'postponed|delayed|new|latest|breaking)',
               '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    words = [w for w in t.split() if len(w) > 2]
    return " ".join(words[:4]).lower().strip()


def group_news_by_topic(items):
    """
    Group news by movie/show name using smart keyword matching.
    Works for: Dhurandhar 2, Jujutsu Kaisen, etc.
    """
    groups = []
    used   = set()

    for i, item in enumerate(items):
        if i in used:
            continue

        topic_i = extract_topic(item["title"])
        # Also check full title for common keywords
        title_words_i = set(re.findall(r'[a-zA-Z0-9]+', item["title"].lower()))
        # Remove very common words
        stopwords = {'the','a','an','of','in','on','at','to','for','is','are',
                     'was','were','has','have','had','its','this','that','with',
                     'from','and','or','but','not','box','office','day','crore'}
        title_words_i -= stopwords

        group = [item]
        used.add(i)

        for j, other in enumerate(items):
            if j in used or j == i:
                continue

            topic_j = extract_topic(other["title"])
            title_words_j = set(re.findall(r'[a-zA-Z0-9]+', other["title"].lower()))
            title_words_j -= stopwords

            # Match if topics share 2+ meaningful words OR titles share 2+ meaningful words
            topic_overlap = len(set(topic_i.split()) & set(topic_j.split()))
            title_overlap = len(title_words_i & title_words_j)

            if topic_overlap >= 2 or title_overlap >= 3:
                group.append(other)
                used.add(j)

        groups.append({"topic": topic_i, "items": group, "label": item.get("label","📰 News")})

    return groups



def build_grouped_news_caption(group):
    items    = group["items"]
    topic    = group["topic"].title()
    label    = items[0].get("label", "📰 News")
    sources  = list(dict.fromkeys(i.get("source","") for i in items if i.get("source")))[:3]
    links    = [i["link"] for i in items if i.get("link")]
    headlines = "\n".join("- " + i["title"] for i in items[:5])
    summary  = groq(
        "Summarize these news headlines about '" + topic + "' into 3-4 sentences. Be factual, include key numbers. No hype:\n\n" + headlines,
        150
    )
    bullets = []
    for item in items[:5]:
        clean = re.sub(r'\s*[-–|]\s*[A-Z][^-|]+$', '', item["title"]).strip()
        bullets.append("• " + clean)
    sources_line = ", ".join(sources) if sources else ""
    tag_words = re.findall(r'[A-Za-z0-9]+', topic)
    hashtags  = " ".join("#" + w for w in tag_words[:3] if len(w) > 2)
    lines = [
        "<b>📰 " + label + "</b>", "",
        "<b>" + topic + " — Latest Updates</b>", "",
        "<blockquote>" + "\n".join(bullets[:5]) + "</blockquote>",
    ]
    if summary:
        lines += ["", summary]
    if sources_line:
        lines += ["", "📌 " + sources_line]
    lines += ["", hashtags, "— " + CHANNEL_WATERMARK]
    return trim_caption("\n".join(lines)), links[0] if links else None


def run_news_monitor():
    print(f"\n{'─'*40}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📰 News monitor running...")
    news_sent = load_news_sent()
    digest    = load_digest()
    posted    = 0
    all_news  = []
    for label, query in NEWS_QUERIES:
        items = fetch_google_news(query, label)
        all_news.extend(items)
        time.sleep(0.5)
    seen, deduped = set(), []
    for item in all_news:
        if item["id"] not in seen:
            seen.add(item["id"])
            deduped.append(item)
    sent_titles = load_news_titles()
    new_items = [
        i for i in deduped
        if i["id"] not in news_sent
        and not is_similar_to_sent(i["title"], sent_titles)
    ]
    print(f"  Found: {len(deduped)} | New (after dedup): {len(new_items)}")
    if not new_items:
        print("  No new breaking news.")
        return
    groups = group_news_by_topic(new_items)
    print(f"  Grouped into {len(groups)} topic(s)")
    for group in groups:
        if posted >= 3: break
        items = group["items"]
        topic = group["topic"].title()
        print(f"\n→ [{items[0]['label']}] {topic} ({len(items)} articles)")
        if len(items) == 1:
            caption = build_news_caption(items[0])
            link    = items[0].get("link","")
            buttons = [("📖 Read Full Story", link)] if link else None
        else:
            caption, link = build_grouped_news_caption(group)
            buttons = [("📖 Read More", link)] if link else None
        result = send_photo(caption, None, buttons=buttons)
        if result.get("ok"):
            for item in items:
                news_sent.append(item["id"])
                sent_titles.append(item["title"])
            save_news_sent(news_sent)
            save_news_titles(sent_titles)
            digest.append({"id": items[0]["id"], "title": topic,
                           "type": "📰 News", "release_date": date_today(), "rating": None})
            save_digest(digest)
            print(f"  ✅ Sent! ({len(items)} articles → 1 post)")
            posted += 1
        else:
            print(f"  ❌ Failed: {result.get('description','Unknown')}")
        time.sleep(3)
    print(f"  News done. Posted {posted} grouped post(s).")



# ─────────────────────────────────────────────
# HEALTH CHECK SERVER
# Keeps Render free tier alive via UptimeRobot
# ─────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Bot is running")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        pass  # Suppress access logs

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server running on port {port}")
    server.serve_forever()

# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Entertainment News Bot — Premium Edition")
    print(f"Channel   : {TELEGRAM_CHANNEL_ID}")
    print(f"Watermark : {CHANNEL_WATERMARK}")
    print(f"Schedule  : Every 30 mins | {MAX_POSTS_PER_RUN} posts/run")
    print(f"Trailers  : Smart detection via TMDB + dedicated trailer posts")
    print(f"News      : Google News RSS — breaking news every 30 mins")

    # Start health server in background thread (keeps Render alive)
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    run_bot()
    run_trailer_detector()
    run_news_monitor()  # Check breaking news on startup

    schedule.every(30).minutes.do(run_bot)
    schedule.every(30).minutes.do(run_news_monitor)
    schedule.every(2).hours.do(run_trailer_detector)
    schedule.every().day.at("21:00").do(send_daily_digest)
    schedule.every().friday.at("18:00").do(send_weekend_watchlist)

    while True:
        schedule.run_pending()
        time.sleep(30)