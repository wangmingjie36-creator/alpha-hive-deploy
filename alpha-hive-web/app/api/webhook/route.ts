import { NextRequest, NextResponse } from 'next/server';
import Stripe from 'stripe';
import { stripe } from '@/lib/stripe';

export async function POST(request: NextRequest) {
  const rawBody = await request.text();
  const signature = request.headers.get('stripe-signature');

  if (!signature) {
    return NextResponse.json({ error: 'Missing stripe-signature header.' }, { status: 400 });
  }

  let event: Stripe.Event;

  try {
    event = stripe.webhooks.constructEvent(
      rawBody,
      signature,
      process.env.STRIPE_WEBHOOK_SECRET!
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Webhook signature verification failed.';
    console.error('[Webhook] Signature error:', message);
    return NextResponse.json({ error: message }, { status: 400 });
  }

  switch (event.type) {
    case 'checkout.session.completed': {
      const session = event.data.object as Stripe.Checkout.Session;
      console.log('[Webhook] New subscriber:', {
        email: session.customer_email,
        subscriptionId: session.subscription,
        sessionId: session.id,
        timestamp: new Date().toISOString(),
      });
      break;
    }

    case 'customer.subscription.deleted': {
      const subscription = event.data.object as Stripe.Subscription;
      console.log('[Webhook] Subscription cancelled:', {
        subscriptionId: subscription.id,
        customerId: subscription.customer,
        cancelledAt: new Date().toISOString(),
      });
      break;
    }

    case 'invoice.payment_failed': {
      const invoice = event.data.object as Stripe.Invoice;
      console.log('[Webhook] Payment failed:', {
        invoiceId: invoice.id,
        customerId: invoice.customer,
        amount: invoice.amount_due,
        currency: invoice.currency,
        failedAt: new Date().toISOString(),
      });
      break;
    }

    default:
      console.log(`[Webhook] Unhandled event type: ${event.type}`);
      return NextResponse.json({ received: true, ignored: true });
  }

  return NextResponse.json({ received: true });
}
