# Changelog

## 0.2.0

- Stop polling and start Home Assistant reauthentication when Entergy rejects the password, so a bad password is not submitted on every poll.
- Retry network failures, timeouts, and server errors with increasing delays. Those failures do not ask for the password again.
- If Entergy or the network is unreachable while Home Assistant starts, setup waits and retries instead of failing until the integration is reloaded by hand.
- Add a reauthentication step so the password can be updated without removing the integration. The existing entry, sensors, and stored energy totals stay in place.
- Keep API response bodies out of normal logs. Tokens, passwords, emails, and account numbers are redacted before anything is logged, including debug logs.
- Redact account identifiers, addresses, and emails in diagnostics.
- Raise the minimum poll interval to 900 seconds. The default stays 3600 seconds. Saved intervals below 900 seconds are raised to 900 seconds.
