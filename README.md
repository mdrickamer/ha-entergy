<p align="center">
  <img src="assets/entergy-wordmark.svg" alt="Entergy" width="560">
</p>

<p align="center">
  <a href="https://hacs.xyz/"><img src="https://img.shields.io/badge/HACS-Custom-orange.svg" alt="HACS Custom"></a>
  <a href="https://github.com/daviddelahoz/ha-entergy/releases"><img src="https://img.shields.io/github/v/release/daviddelahoz/ha-entergy" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/daviddelahoz/ha-entergy" alt="License"></a>
  <a href="https://github.com/daviddelahoz/ha-entergy"><img src="https://img.shields.io/maintenance/yes/2026.svg" alt="Maintenance"></a>
</p>

# Entergy

Home Assistant custom integration for Entergy electric usage data.

This integration signs in with your Entergy account, lets you select an account during setup, and creates sensors for recent usage, cost, and Energy Dashboard-friendly cumulative import/export totals.

> This project is unofficial and is not affiliated with, endorsed by, or supported by Entergy.

<p align="center">
  <a href="https://www.buymeacoffee.com/daviddelahoz" target="_blank">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" height="60" width="217">
  </a>
</p>

## Features

- Config flow setup from the Home Assistant UI
- Account selection during setup
- Cloud polling for Entergy usage data
- Latest finalized hour, latest day, last 7 days, and month-to-date usage sensors
- Cost sensors when cost data is returned by Entergy
- Cumulative import and export energy sensors
- Hourly grid-import statistics for the Home Assistant Energy Dashboard, stamped with the hour the energy was used
- Diagnostics support with sensitive values redacted

## Sensors

Energy Dashboard statistics (recommended):

- `entergy_mobile:grid_import_<account>` — hourly grid consumption in kWh
- `entergy_mobile:grid_import_cost_<account>` — hourly cost, only when Entergy sends it

Compatibility sensors:

- Imported Energy Total
- Exported Energy Total

Usage and cost sensors:

- Latest Finalized Hour Net Usage
- Latest Finalized Hour Import
- Latest Finalized Hour Export
- Latest Finalized Hour Cost
- Latest Day Net Usage
- Latest Day Import
- Latest Day Export
- Latest Day Cost
- Last 7 Days Net Usage
- Last 7 Days Cost
- Month-to-Date Net Usage
- Month-to-Date Cost
- Tracked Hourly Intervals
- Latest Finalized Hour Start

## Installation

### HACS custom repository

1. Open HACS.
2. Go to Custom repositories.
3. Add `https://github.com/daviddelahoz/ha-entergy`.
4. Select category `Integration`.
5. Install `Entergy`.
6. Restart Home Assistant.
7. Go to Settings, Devices & services, Add integration.
8. Search for `Entergy`.

### Manual installation

Copy this integration folder to:

```text
/config/custom_components/entergy_mobile
```

Then restart Home Assistant and add the integration from Settings, Devices & services.

For a standard GitHub repository layout, the files should be arranged as:

```text
ha-entergy/
  README.md
  assets/
    entergy-icon.svg
    entergy-wordmark.svg
  custom_components/
    entergy_mobile/
      __init__.py
      api.py
      config_flow.py
      const.py
      coordinator.py
      diagnostics.py
      entity.py
      manifest.json
      sensor.py
      statistics.py
      strings.json
      translations/
        en.json
```

## Configuration

1. In Home Assistant, go to Settings, Devices & services.
2. Select Add integration.
3. Search for `Entergy`.
4. Enter your Entergy credentials.
5. Choose the Entergy account to monitor.

The integration stores the access token only in memory. Home Assistant stores the credentials in the config entry so the integration can log in again after restart.

If Entergy later rejects the password, polling stops and Home Assistant asks you to reauthenticate. Enter the new password there. The existing entry, sensors, and energy statistics stay in place. Temporary connection problems are retried and do not ask for the password again.

## Energy Dashboard

Entergy returns hourly intervals and finalizes them in blocks, roughly every 4 hours and about 5 hours behind real time. Hours that are not finalized yet come back as 0. Use the external statistic below for grid consumption so each bar stays on the hour the energy was used.

In Settings, Dashboards, Energy, set grid consumption to:

```text
entergy_mobile:grid_import_<account>
```

`<account>` is the account id with letters lowercased and any other character turned into an underscore. The statistic name shown in Home Assistant is `Entergy grid import ending XXXX`, and XXXX is only the last four characters of the account. The unit is kWh.

When the usage payload includes an hourly cost, the integration also writes:

```text
entergy_mobile:grid_import_cost_<account>
```

That statistic uses the currency configured in Home Assistant. It is not created when Entergy does not send hourly cost.

Each poll re-imports the last 4 days of the weekly usage response. The running sum continues from the last stored value before that window, so a corrected hour replaces the earlier value instead of being added again. The first successful poll fills in about 4 days. Later polls keep those hours and add new ones. Usage older than that first window is not backfilled, because the request itself only covers about a week.

Switching the Energy dashboard from `Imported Energy Total` to this statistic starts the dashboard from the imported history. The bars already recorded for the old sensor are not moved over. The old sensor remains, with the same entity id, for anything else that reads it.

`Imported Energy Total` and `Exported Energy Total` are still cumulative counters stored by the integration. They start at the first successful poll, they do not move downward when Entergy revises an hour, and deleting the integration storage resets them. Those counters are not what places the hourly bars.

## Options

The data update interval can be changed from the integration options.

Default:

```text
3600 seconds
```

Allowed range:

```text
900 to 86400 seconds
```

Intervals below 900 seconds, including values saved by older versions of this integration, are raised to 900 seconds.

## Debug Logging

To troubleshoot setup or polling, enable debug logs:

```yaml
logger:
  default: info
  logs:
    custom_components.entergy_mobile: debug
```

Restart Home Assistant, reproduce the issue, then check Settings, System, Logs.

Sensitive values such as username, password, email, account number, authorization headers, client ID, and access tokens are not written to logs. API response bodies are logged only at debug level, and only after those values are redacted.

## Known Limitations

- Additional login challenges, such as MFA or unsupported next actions, may not work yet.
- Usage data is requested from `https://mobileapp.api-prod.entergy.com/api` with app version 3.63.0. That host has not been confirmed with a live Entergy login yet.
- The integration depends on Entergy service behavior, which can change without notice.
- Energy Dashboard hourly bars come from the external grid import statistic. `Imported Energy Total` remains a cumulative counter and does not by itself place usage in the hour it occurred.
- The hourly statistic timestamp follows the `date` on each hourly interval. Values with a timezone, including the `Z` timestamps seen from this API, are converted to UTC. A value with no timezone is read as America/Chicago wall time.

## Development

Validate Python syntax locally:

```bash
python -m compileall custom_components/entergy_mobile
```

Run the test suite (it mocks every HTTP call and must not contact `mobileapp.api-prod.entergy.com` or `prod.entergy.mindgrb.io`):

```bash
pip install -r requirements_test.txt
pytest
```

For this workspace, if you are already inside the integration folder:

```bash
python -m compileall .
```
