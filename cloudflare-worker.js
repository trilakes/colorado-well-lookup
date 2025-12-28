// Cloudflare Worker to verify Stripe subscriptions
// Deploy to: https://workers.cloudflare.com/

export default {
  async fetch(request, env) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
        },
      });
    }

    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method not allowed' }), {
        status: 405,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

    try {
      const { email } = await request.json();

      if (!email) {
        return new Response(JSON.stringify({ valid: false, message: 'Email required' }), {
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      // Search for customer by email in Stripe
      const customersResponse = await fetch(
        `https://api.stripe.com/v1/customers?email=${encodeURIComponent(email)}&limit=1`,
        {
          headers: {
            'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}`,
          },
        }
      );

      const customers = await customersResponse.json();

      if (!customers.data || customers.data.length === 0) {
        return new Response(JSON.stringify({ valid: false, message: 'No account found for this email' }), {
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      const customerId = customers.data[0].id;

      // Check for active subscriptions
      const subscriptionsResponse = await fetch(
        `https://api.stripe.com/v1/subscriptions?customer=${customerId}&status=active&limit=1`,
        {
          headers: {
            'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}`,
          },
        }
      );

      const subscriptions = await subscriptionsResponse.json();

      if (subscriptions.data && subscriptions.data.length > 0) {
        return new Response(JSON.stringify({ valid: true, message: 'Active subscription found' }), {
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      // Also check for successful one-time payments (if you switch to that model)
      const paymentsResponse = await fetch(
        `https://api.stripe.com/v1/payment_intents?customer=${customerId}&limit=10`,
        {
          headers: {
            'Authorization': `Bearer ${env.STRIPE_SECRET_KEY}`,
          },
        }
      );

      const payments = await paymentsResponse.json();
      const hasSuccessfulPayment = payments.data?.some(p => p.status === 'succeeded');

      if (hasSuccessfulPayment) {
        return new Response(JSON.stringify({ valid: true, message: 'Payment verified' }), {
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      return new Response(JSON.stringify({ valid: false, message: 'No active subscription found' }), {
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });

    } catch (error) {
      return new Response(JSON.stringify({ valid: false, message: 'Verification error' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }
  },
};
