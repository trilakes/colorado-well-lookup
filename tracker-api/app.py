from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import json
import os
import urllib.request

app = Flask(__name__)
CORS(app)  # Allow requests from coloradowell.com

# Simple in-memory storage (Render has persistent disk option, or use Redis add-on)
visitors = []
daily_stats = {}

# Secret keys from environment
HQ_KEY = os.environ.get('HQ_KEY', 'well2025hq')
STRIPE_KEY = os.environ.get('STRIPE_KEY', '')

@app.route('/')
def home():
    return jsonify({"status": "ok", "service": "Colorado Well Tracker"})

@app.route('/stripe/payments', methods=['GET'])
def get_stripe_payments():
    """Get payment data from Stripe - called from HQ dashboard"""
    key = request.args.get('key', '')
    if key != HQ_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    
    if not STRIPE_KEY:
        return jsonify({"error": "Stripe not configured"}), 500
    
    try:
        # Fetch payments from Stripe
        req = urllib.request.Request(
            'https://api.stripe.com/v1/payment_intents?limit=20',
            headers={'Authorization': f'Bearer {STRIPE_KEY}'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        
        payments = [p for p in data.get('data', []) if p.get('status') == 'succeeded']
        
        # Calculate stats
        total_revenue = sum(p['amount'] for p in payments)
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1).timestamp()
        month_revenue = sum(p['amount'] for p in payments if p['created'] >= month_start)
        
        return jsonify({
            "totalRevenue": total_revenue / 100,
            "totalCustomers": len(payments),
            "monthRevenue": month_revenue / 100,
            "monthName": now.strftime('%B'),
            "payments": [{
                "email": p.get('receipt_email') or 'Customer',
                "amount": p['amount'] / 100,
                "date": datetime.fromtimestamp(p['created']).strftime('%Y-%m-%d'),
                "created": p['created']
            } for p in payments[:10]]
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/track', methods=['POST', 'OPTIONS'])
def track():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.get_json() or {}
        
        # Get IP address
        ip = (request.headers.get('CF-Connecting-IP') or 
              request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or 
              request.remote_addr)
        
        # Get user agent and parse browser/device
        user_agent = request.headers.get('User-Agent', '')
        browser = 'Unknown'
        device = 'Desktop'
        
        if 'Chrome' in user_agent and 'Edg' not in user_agent:
            browser = 'Chrome'
        elif 'Edg' in user_agent:
            browser = 'Edge'
        elif 'Firefox' in user_agent:
            browser = 'Firefox'
        elif 'Safari' in user_agent and 'Chrome' not in user_agent:
            browser = 'Safari'
        
        if 'Mobile' in user_agent or 'Android' in user_agent:
            device = 'Mobile'
        elif 'iPad' in user_agent or 'Tablet' in user_agent:
            device = 'Tablet'
        
        # Get visitor info
        visitor = {
            'ip': ip,
            'timestamp': int(datetime.now().timestamp() * 1000),
            'page': data.get('page', '/'),
            'referrer': data.get('referrer', ''),
            'browser': browser,
            'device': device,
            'userAgent': user_agent[:100],  # Truncate for storage
            'country': 'US',
            'city': None,
            'region': None,
            'lat': None,
            'lon': None
        }
        
        # Try ip-api.com (free, no key needed, 45 req/min)
        try:
            if ip and ip not in ['127.0.0.1', 'localhost']:
                geo_url = f'http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon'
                req = urllib.request.Request(geo_url, headers={'User-Agent': 'WellTracker/1.0'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    geo = json.loads(response.read().decode())
                    if geo.get('status') == 'success':
                        visitor['city'] = geo.get('city')
                        visitor['region'] = geo.get('regionName')
                        visitor['country'] = geo.get('country')
                        visitor['lat'] = geo.get('lat')
                        visitor['lon'] = geo.get('lon')
        except Exception as e:
            print(f"Geo lookup failed: {e}")  # Log for debugging
        
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
