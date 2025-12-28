# Well Tracker API

Simple Flask API for tracking visitors on coloradowell.com

## Deploy to Render

1. Push this folder to GitHub (or deploy directly)
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your repo and select the `tracker-api` folder
4. Settings:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
5. Add environment variable: `HQ_KEY` = `well2025hq`
6. Deploy!

Your API will be at: `https://your-service-name.onrender.com`

## Endpoints

- `POST /track` - Track a visitor (called from main site)
- `GET /visitors?key=well2025hq` - Get visitor data (for HQ dashboard)
- `GET /health` - Health check
