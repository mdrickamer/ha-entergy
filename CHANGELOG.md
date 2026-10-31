# Changelog

## 0.3.0

- Import finalized hourly Entergy usage as external statistics, so Energy dashboard bars use the hour the electricity was used. Entergy finalizes hours in blocks about every 4 hours and about 5 hours behind real time. Hours that are not finalized are reported as 0 and are left out until a later poll. A real zero hour is kept when a later finalized hour follows it.
- Grid import statistic id: `entergy_mobile:grid_import_<account>`. The name is "Entergy grid import ending XXXX", where XXXX is the last four characters of the account. The unit is kWh and the statistic stores a running sum.
- When the payload includes an hourly cost, also import `entergy_mobile:grid_import_cost_<account>` in the Home Assistant currency. If cost is absent, that statistic is left alone.
- Each poll re-imports the last 4 days returned by weekly usage and rebuilds the running sum from the last stored value before that window. A correction replaces those hours instead of adding them a second time. Hours older than the window stay as they were.
- Latest Hour sensors now show the latest finalized hour and its start time. Their unique ids are unchanged. Imported Energy Total is unchanged.
- For hourly Energy dashboard bars, select the grid import statistic as grid consumption. The dashboard then uses the imported history. The previous sensor remains available, and its existing history is not copied into the new statistic.

## 0.2.1

- Call Entergy at `https://mobileapp.api-prod.entergy.com/api` and report app version 3.63.0, matching the current official Android app. The previous host, `prod.entergy.mindgrb.io`, is no longer used. This host change has not been confirmed with a live login. One real sign-in is needed to verify the new host serves the same API.

## 0.2.0

- Stop polling and start Home Assistant reauthentication when Entergy rejects the password, so a bad password is not submitted on every poll.
- Retry network failures, timeouts, and server errors with increasing delays. Those failures do not ask for the password again.
- If Entergy or the network is unreachable while Home Assistant starts, setup waits and retries instead of failing until the integration is reloaded by hand.
- Add a reauthentication step so the password can be updated without removing the integration. The existing entry, sensors, and stored energy totals stay in place.
- Keep API response bodies out of normal logs. Tokens, passwords, emails, and account numbers are redacted before anything is logged, including debug logs.
- Redact account identifiers, addresses, and emails in diagnostics.
- Raise the minimum poll interval to 900 seconds. The default stays 3600 seconds. Saved intervals below 900 seconds are raised to 900 seconds.
