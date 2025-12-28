from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import os

app = Flask(__name__)
CORS(app)  # Allow requests from coloradowell.com

# Simple in-memory storage (Render has persistent disk option, or use Redis add-on)
visitors = []
daily_stats = {}

# Secret key for accessing visitor data
HQ_KEY = os.environ.get('HQ_KEY', 'well2025hq')

@app.route('/')
def home():
    return jsonify({"status": "ok", "service": "Colorado Well Tracker"})

@app.route('/track', methods=['POST', 'OPTIONS'])
def track():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.get_json() or {}
        
        # Get visitor info from request
        visitor = {
            'ip': request.headers.get('CF-Connecting-IP') or 
                  request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or 
                  request.remote_addr,
            'timestamp': int(datetime.now().timestamp() * 1000),
            'page': data.get('page', '/'),
            'referrer': data.get('referrer', ''),
            'country': request.headers.get('CF-IPCountry', 'US'),
            'city': None,
            'region': None,
            'lat': None,
            'lon': None
        }
        
        # Try to get geo info from ipapi.co (free tier: 1000/day)
        try:
            import urllib.request
            ip = visitor['ip']
            if ip and ip not in ['127.0.0.1', 'localhost']:
                geo_url = f'https://ipapi.co/{ip}/json/'
                with urllib.request.urlopen(geo_url, timeout=2) as response:
                    geo = json.loads(response.read().decode())
                    visitor['city'] = geo.get('city')
                    visitor['region'] = geo.get('region')
                    visitor['country'] = geo.get('country_name', visitor['country'])
                    visitor['lat'] = geo.get('latitude')
                    visitor['lon'] = geo.get('longitude')
        except:
            pass  # Geo lookup failed, continue without it
        
        # Add to visitors list
        visitors.insert(0, visitor)
        
        # Keep only last 100 visitors
        while len(visitors) > 100:
            visitors.pop()
        
        # Update daily stats
        today = datetime.now().strftime('%Y-%m-%d')
        daily_stats[today] = daily_stats.get(today, 0) + 1
        
        # Clean up old stats (keep 30 days)
        cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        for key in list(daily_stats.keys()):
            if key < cutoff:
                del daily_stats[key]
        
        return jsonify({"success": True})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/visitors', methods=['GET'])
def get_visitors():
    # Check auth key
    key = request.args.get('key', '')
    if key != HQ_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    
    # Calculate live count (visitors in last 5 minutes)
    five_min_ago = int((datetime.now() - timedelta(minutes=5)).timestamp() * 1000)
    live_count = sum(1 for v in visitors if v['timestamp'] > five_min_ago)
    
    return jsonify({
        "live": live_count,
        "visitors": visitors[:50],  # Return last 50
        "stats": daily_stats
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
