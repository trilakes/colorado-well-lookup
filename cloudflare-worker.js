// Cloudflare Worker to verify Stripe subscriptions
// Deploy to: https://workers.cloudflare.com/
// NOTE: Primary verification is now handled by the Render API at
//       https://colorado-wells-api.onrender.com/api/verify
//       This worker is kept as a backup but the frontend uses the API directly.

export default {
  async fetch(request, env) {
    const corsHeaders = {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ valid: false, message: 'Method not allowed' }), {
        status: 405, headers: corsHeaders,
      });
    }

    try {
      const { email } = await request.json();

      if (!email) {
        return new Response(JSON.stringify({ valid: false, message: 'Email required' }), {
          headers: corsHeaders,
        });
      }

      const emailLower = email.toLowerCase().trim();

      // Owner / pre-authorized emails — skip Stripe lookup
      const OWNER_EMAILS = ['kyle@trilakes.co'];
      if (OWNER_EMAILS.includes(emailLower)) {
        return new Response(JSON.stringify({
          valid: true,
          type: 'lifetime',
          message: 'Lifetime access verified',
        }), { headers: corsHeaders });
      }

      const stripeHeaders = { 'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}` };

      // ── Step 1: Customer-based lookup ──
      const customersResponse = await fetch(
        `https://api.stripe.com/v1/customers?email=${encodeURIComponent(emailLower)}&limit=1`,
        { headers: stripeHeaders }
      );
      const customers = await customersResponse.json();

      if (customers.data && customers.data.length > 0) {
        const customerId = customers.data[0].id;

        // 1a) Check for active subscription (monthly)
        const subscriptionsResponse = await fetch(
          `https://api.stripe.com/v1/subscriptions?customer=${customerId}&status=active&limit=1`,
          { headers: stripeHeaders }
        );
        const subscriptions = await subscriptionsResponse.json();

        if (subscriptions.data && subscriptions.data.length > 0) {
          const sub = subscriptions.data[0];
          return new Response(JSON.stringify({
            valid: true,
            type: 'monthly',
            message: 'Active subscription found',
            expiresAt: sub.current_period_end * 1000,
          }), { headers: corsHeaders });
        }

        // 1b) Check for successful one-time payment (lifetime)
        const sessionsResponse = await fetch(
          `https://api.stripe.com/v1/checkout/sessions?customer=${customerId}&limit=20`,
          { headers: stripeHeaders }
        );
        const sessions = await sessionsResponse.json();
        const hasLifetime = sessions.data?.some(s =>
          s.payment_status === 'paid' && s.mode === 'payment'
        );

        if (hasLifetime) {
          return new Response(JSON.stringify({
            valid: true,
            type: 'lifetime',
            message: 'Lifetime access verified',
          }), { headers: corsHeaders });
        }

        // 1c) Fallback — check payment_intents directly
        const paymentsResponse = await fetch(
          `https://api.stripe.com/v1/payment_intents?customer=${customerId}&limit=10`,
          { headers: stripeHeaders }
        );
        const payments = await paymentsResponse.json();
        const hasSuccessfulPayment = payments.data?.some(p => p.status === 'succeeded');

        if (hasSuccessfulPayment) {
          return new Response(JSON.stringify({
            valid: true,
            type: 'lifetime',
            message: 'Payment verified',
          }), { headers: corsHeaders });
        }
      }

      // ── Step 2: Email-based fallback (no customer or customer had no payments) ──
      // Scan checkout sessions from known Payment Links by email.
      const PAYMENT_LINKS = [
        'plink_1T0XVOFiHBHcGzRNQXGAkacg',  // Lifetime $47 (active)
        'plink_1T0XVTFiHBHcGzRNkd9H0TWL',  // Monthly $19 (active)
        'plink_1T0T2KFiHBHcGzRNUJfF6mTD',
        'plink_1T0SuoFiHBHcGzRNUcVpycOk',
        'plink_1T0SuoFiHBHcGzRN9NwMrpqm',
        'plink_1T0Rm2FiHBHcGzRNMDlLGOxZ',
        'plink_1T0RlwFiHBHcGzRNfHxd5vlh',
        'plink_1T0O6zFiHBHcGzRNs0fLtb2q',
        'plink_1T0O6sFiHBHcGzRNYinOZNvg',
        'plink_1T0O2WFiHBHcGzRNnCDNowrR',
        'plink_1SjAS0FiHBHcGzRNSfmm24E1',
      ];

      for (const plinkId of PAYMENT_LINKS) {
        try {
          const r = await fetch(
            `https://api.stripe.com/v1/checkout/sessions?payment_link=${plinkId}&limit=100`,
            { headers: stripeHeaders }
          );
          const data = await r.json();
          for (const s of (data.data || [])) {
            if (s.customer_details?.email?.toLowerCase() === emailLower && s.payment_status === 'paid') {
              const accessType = s.mode === 'subscription' ? 'monthly' : 'lifetime';
              return new Response(JSON.stringify({
                valid: true,
                type: accessType,
                message: `${accessType.charAt(0).toUpperCase() + accessType.slice(1)} access verified`,
              }), { headers: corsHeaders });
            }
          }
        } catch (e) {
          continue;
        }
      }

      return new Response(JSON.stringify({ valid: false, message: 'No active subscription found' }), {
        headers: corsHeaders,
      });

    } catch (error) {
      return new Response(JSON.stringify({ valid: false, message: 'Verification error' }), {
        status: 500, headers: corsHeaders,
      });
    }
  },
};
