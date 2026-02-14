# ⚽ Penalty Shootout — Public Deployment

**No web browser needed.** Players use Flash Player 32 Standalone Projector.

## How players connect

1. Download **Flash Player 32 Standalone** (one-time):
   - Windows: https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.exe
   - macOS: https://github.com/ntkernel/flash/releases/download/32.0.0.238/flashplayer_32_sa.dmg


2. Open Flash Player → **File → Open**

3. Paste the game URL:
   ```
   https://YOUR-APP-NAME.fly.dev/Penalty.swf
   ```

The landing page at `https://YOUR-APP-NAME.fly.dev/` shows these instructions automatically.

---

## Files needed before deploying

```
penalty-game/
├── server.py         ← Flask server + robot AI
├── Penalty.swf       ← ← ← COPY YOUR SWF HERE
├── crossdomain.xml
├── requirements.txt
├── Procfile
├── Dockerfile
└── fly.toml
```

---

## Deploy to Fly.io (free, 24/7, no credit card)

```bash
# Install flyctl
brew install flyctl          # macOS
# or: curl -L https://fly.io/install.sh | sh   # Linux

# Login (creates free account)
fly auth signup

# Edit fly.toml → change app name to something unique
# app = "penalty-yourname"

# First deploy
cd penalty-game
fly launch --no-deploy
fly deploy

# Your URL:
fly open
# → https://penalty-yourname.fly.dev
```

Check logs anytime:
```bash
fly logs
```

---

## Deploy to Railway (GUI, no terminal needed)

1. Push this folder to GitHub
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Select your repo → Railway auto-deploys
4. Settings → Generate Domain → share that URL

---

## Why Flash Projector works (not browser)

The SWF uses **relative URLs** (`getMessages.php`, `joinRoom.php`, etc.).
Flash Projector resolves these relative to the URL you opened —
so `https://yourapp.fly.dev/Penalty.swf` makes all requests to `yourapp.fly.dev`.

The server handles both serving the SWF file and all game API calls.
