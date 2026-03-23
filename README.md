# 🎬 Entertainment News Bot — 100% Free

Auto-posts Movies, Anime, K-Drama, and Web Series to your Telegram channel every 6 hours. No credit card. No paid APIs.

## ✅ Everything is Free
| Service | What It Does | Cost |
|---------|-------------|------|
| TMDB API | Movies, Series, K-Drama | Free |
| Jikan API | Anime (MyAnimeList) | Free |
| Google Gemini API | AI captions | Free |
| Telegram Bot API | Posts to channel | Free |

## Setup

### 1. Get API Keys

**TMDB** → https://www.themoviedb.org/signup → Settings → API → Request Key (Developer)

**Telegram Bot** → Open @BotFather → /newbot → copy token → add bot as Admin to your channel

**Google Gemini (Free AI)** → https://aistudio.google.com → Get API Key → Create API Key
(Free: 1500 requests/day — more than enough)

### 2. Install
```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in all 4 values in .env
```

### 3. Run
```bash
python bot.py
```

Posts immediately on launch, then every 6 hours.

## Run 24/7
```bash
nohup python bot.py > bot.log 2>&1 &
```

## Customize
- Change `MAX_POSTS_PER_RUN = 3` in bot.py to post more/less per cycle
- Delete `sent_ids.json` to reset and re-post everything
