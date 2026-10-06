# Integrations: failing webhook deliveries

## Read the status codes
5xx or timeouts (`status_code` 0) usually mean the customer's receiver was down.
4xx (for example 404 or 410) means the request is being rejected; redelivery will not help.

## Remedy
Redeliver a failed 5xx/timeout delivery once (safe). For persistent 4xx, tell the
customer which URL is failing and what it returns. Disabling an endpoint stops their
integration and needs operator approval.
