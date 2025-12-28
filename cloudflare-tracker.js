// Cloudflare Worker for visitor tracking
// Deploy at: https://dash.cloudflare.com → Workers → Create Worker

// This stores visitors in Cloudflare KV (bind as VISITORS)
// Create a KV namespace called "VISITORS" and bind it to this worker

export default {
  async fetch(request, env) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);

    // Track a visitor
    if (url.pathname === '/track' && request.method === 'POST') {
      try {
        const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
        const country = request.headers.get('CF-IPCountry') || 'XX';
        const body = await request.json();
        
        // Get geolocation from IP (using Cloudflare's built-in data)
        const cf = request.cf || {};
        
        const visitor = {
          id: crypto.randomUUID(),
          ip: ip,
          country: country,
          city: cf.city || 'Unknown',
          region: cf.region || '',
          lat: cf.latitude || 39.7392,  // Default to Denver
          lon: cf.longitude || -104.9903,
          page: body.page || '/',
          referrer: body.referrer || '',
          userAgent: request.headers.get('User-Agent') || '',
          timestamp: Date.now()
        };

        // Get existing visitors (last 24 hours)
        let visitors = [];
        try {
          const stored = await env.VISITORS.get('recent', { type: 'json' });
          if (stored) visitors = stored;
        } catch (e) {}

        // Add new visitor
        visitors.unshift(visitor);

        // Keep only last 100 visitors and last 24 hours
        const dayAgo = Date.now() - (24 * 60 * 60 * 1000);
        visitors = visitors.filter(v => v.timestamp > dayAgo).slice(0, 100);

        // Store back
        await env.VISITORS.put('recent', JSON.stringify(visitors));

        // Update daily stats
        const today = new Date().toISOString().split('T')[0];
        let stats = {};
        try {
          const storedStats = await env.VISITORS.get('stats', { type: 'json' });
          if (storedStats) stats = storedStats;
        } catch (e) {}

        stats[today] = (stats[today] || 0) + 1;
        await env.VISITORS.put('stats', JSON.stringify(stats));

        return new Response(JSON.stringify({ success: true }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });

      } catch (error) {
        return new Response(JSON.stringify({ error: error.message }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }
    }

    // Get visitors (for HQ dashboard)
    if (url.pathname === '/visitors' && request.method === 'GET') {
      // Simple auth check
      const auth = url.searchParams.get('key');
      if (auth !== 'well2025hq') {
        return new Response(JSON.stringify({ error: 'Unauthorized' }), {
          status: 401,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }

      try {
        let visitors = [];
        let stats = {};
        
        try {
          const stored = await env.VISITORS.get('recent', { type: 'json' });
          if (stored) visitors = stored;
        } catch (e) {}

        try {
          const storedStats = await env.VISITORS.get('stats', { type: 'json' });
          if (storedStats) stats = storedStats;
        } catch (e) {}

        // Calculate live visitors (last 5 minutes)
        const fiveMinAgo = Date.now() - (5 * 60 * 1000);
        const liveCount = visitors.filter(v => v.timestamp > fiveMinAgo).length;

        return new Response(JSON.stringify({
          live: liveCount,
          visitors: visitors,
          stats: stats
        }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });

      } catch (error) {
        return new Response(JSON.stringify({ error: error.message }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }
    }

    return new Response('Colorado Well Finder Tracker', {
      headers: corsHeaders
    });
  }
};
