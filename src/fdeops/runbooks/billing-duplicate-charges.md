# Billing: suspected duplicate charges

## Distinguish holds from charges
An `authorization` is a temporary hold placed by the card network. It is not money
taken. It either becomes a `capture` or lapses (`auth_expired`) by its `expires_at`.
A customer who sees an authorization and a capture for the same amount has usually
been charged once.

## Confirm a true duplicate
A duplicate charge is two `capture` events for the same invoice. Compare
`invoice_id`, `amount_cents` and `processor_ref`. Different processor references for
the same invoice means two separate captures.

## Remedy
Refund the later capture in full. Refunds are consequential and always require
operator approval. Never refund an authorization. Never refund more than was captured.
Reply to the customer with what was found and what has been requested.
