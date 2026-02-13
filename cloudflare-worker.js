// Cloudflare Worker to verify Stripe subscriptions
// Deploy to: https://workers.cloudflare.com/

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

      // Owner / pre-authorized emails — skip Stripe lookup
      const OWNER_EMAILS = ['kyle@trilakes.co'];
      if (OWNER_EMAILS.includes(email.toLowerCase())) {
        return new Response(JSON.stringify({
          valid: true,
          type: 'lifetime',
          message: 'Lifetime access verified',
        }), { headers: corsHeaders });
      }

      // Search for customer by email in Stripe
      const customersResponse = await fetch(
        `https://api.stripe.com/v1/customers?email=${encodeURIComponent(email)}&limit=1`,
        { headers: { 'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}` } }
      );
      const customers = await customersResponse.json();

      if (!customers.data || customers.data.length === 0) {
        return new Response(JSON.stringify({ valid: false, message: 'No account found for this email' }), {
          headers: corsHeaders,
        });
      }

      const customerId = customers.data[0].id;

      // 1) Check for active subscription (monthly)
      const subscriptionsResponse = await fetch(
        `https://api.stripe.com/v1/subscriptions?customer=${customerId}&status=active&limit=1`,
        { headers: { 'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}` } }
      );
      const subscriptions = await subscriptionsResponse.json();

      if (subscriptions.data && subscriptions.data.length > 0) {
        const sub = subscriptions.data[0];
        return new Response(JSON.stringify({
          valid: true,
          type: 'monthly',
          message: 'Active subscription found',
          expiresAt: sub.current_period_end * 1000, // ms timestamp
        }), { headers: corsHeaders });
      }

      // 2) Check for successful one-time payment (lifetime)
      const sessionsResponse = await fetch(
        `https://api.stripe.com/v1/checkout/sessions?customer=${customerId}&limit=20`,
        { headers: { 'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}` } }
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

      // 3) Fallback — check payment_intents directly
      const paymentsResponse = await fetch(
        `https://api.stripe.com/v1/payment_intents?customer=${customerId}&limit=10`,
        { headers: { 'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}` } }
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
