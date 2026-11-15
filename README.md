# hermes-gemini-rate-limit

A proactive, model-aware rate limiting plugin for [Hermes Agent](https://hermes-agent.nousresearch.com) that throttles outbound Google Gemini API calls by both **Requests Per Minute (RPM)** and **Input Tokens Per Minute (TPM)** using a sliding-window algorithm, preventing HTTP 429 `RESOURCE_EXHAUSTED` errors before they happen.

Includes real-time quota telemetry and live controls in the **Hermes Desktop** bottom status bar.

---

## Features

- **Dual Sliding-Window Enforcement (60.0s Window)**:
  - **RPM Gate**: Prevents exceeding peak requests per minute with sub-second sliding expiration.
  - **TPM Gate**: Pre-flight estimates outbound prompt and tool tokens, holding requests if cumulative 60s input tokens exceed capacity.
  - **Smooth Pacing Delay**: Enforces pacing delay (`(60.0 / effective_rpm) * 0.75`) so multi-step tool loops do not exhaust the entire quota in a few seconds.
- **Safety Factor (Default `0.92`)**: Absorbs clock-skew between local execution and Google's quota buckets.
- **Pre-Configured Model Quotas**: Built-in limits for 26 Google Gemini models (Gemini 3.5 Flash Lite, Gemini 3.8 Flash, Gemini 3 Flash, Gemini 2.5 Flash, Gemini Live API, Gemma, Embedding, TTS, etc.).
- **Unenforced RPD Reference**: Keeps reference daily quotas (`RPD`) in configuration without blocking turns, allowing standard upstream handling for daily exhaustion.
- **Reactive 429 Cooldown**: If an upstream 429 error occurs, automatically parses `retryDelay` or `Retry-After` headers and pauses outbound calls to prevent hammering the API.
- **Hermes Desktop Status Bar & Popover UI**:
  - Compact chip in `STATUSBAR_AREAS.right`: `Gemini: {rpmUsed}/{rpm} RPM · {tpmUsed}k/{tpm}k TPM`.
  - Color-coded capacity indicators: Green (<75%), Amber (≥75%), Red (actively throttled).
  - Popover with live usage counters, RPM/TPM numeric inputs, enable switch, and Save button.
  - Command palette shortcut (`Ctrl+K` → *Gemini Rate Limit: Configure Quotas*).
- **In-Chat Slash Command**: `/gemini-limit [rpm] [tpm]` to inspect or adjust limits on the fly.

---

## Directory Structure

Unified package layout compatible with Hermes Desktop (`>=0.22.5`):

```text
gemini-rate-limit/
├── plugin.yaml           # Manifest & middleware registration
├── __init__.py           # Package entry point & slash command handler
├── rate_limiter.py       # Sliding-window engine & atomic JSON storage
├── dashboard/
│   ├── manifest.json     # Dashboard plugin metadata
│   └── plugin_api.py     # FastAPI endpoints (/api/plugins/gemini-rate-limit/)
├── desktop/
│   └── plugin.js         # Hermes Desktop ESM status bar UI & popover
├── tests/
│   └── test_gemini_rate_limit.py # Automated test suite
└── default_config.json   # Default model quotas
```

---

## Installation

### Option 1: Symlink / Clone into Hermes Plugins (Recommended for Development)

```bash
git clone https://github.com/0dysseyAI/hermes-gemini-rate-limit.git ~/.hermes/plugins/gemini-rate-limit
hermes plugins enable gemini-rate-limit
hermes gateway restart
```

### Option 2: Standalone Hermes Desktop Hot-Loading

For Hermes Desktop, link or copy the desktop surface:

```bash
mkdir -p ~/.hermes/desktop-plugins/gemini-rate-limit
cp ~/.hermes/plugins/gemini-rate-limit/desktop/plugin.js ~/.hermes/desktop-plugins/gemini-rate-limit/
```

Press <kbd>Ctrl</kbd>+<kbd>K</kbd> in Hermes Desktop and run **Reload desktop plugins**.

---

## Configuration

Stored at `~/.hermes/gemini_rate_limit.json` (respects `$HERMES_HOME` if set):

```json
{
  "enabled": true,
  "rpm": 15,
  "tpm": 250000,
  "rpd": 500,
  "safety_factor": 0.92,
  "models": {
    "gemini-3.5-flash-lite": { "rpm": 15, "tpm": 250000, "rpd": 500 },
    "gemini-3.8-flash": { "rpm": 5, "tpm": 250000, "rpd": 20 },
    "gemini-3.1-flash-lite": { "rpm": 15, "tpm": 250000, "rpd": 500 },
    "gemini-2.5-flash-lite": { "rpm": 10, "tpm": 250000, "rpd": 20 },
    "gemini-2.5-flash": { "rpm": 5, "tpm": 250000, "rpd": 20 }
  }
}
```

Live telemetry is continuously published to `~/.hermes/gemini_rate_limit_status.json`:

```json
{
  "enabled": true,
  "rpm": 15,
  "tpm": 250000,
  "effective_rpm": 13,
  "effective_tpm": 230000,
  "rpm_used": 1,
  "tpm_used": 3420,
  "last_wait_seconds": 0.0,
  "total_throttled_requests": 0,
  "is_throttling": false
}
```

---

## Usage

### Hermes Desktop Status Bar
- Click the **Gemini** chip on the bottom-right status bar to open the quota management popover.
- Adjust RPM or TPM values and click **Save** to apply changes immediately without restarting.

### Chat Slash Command
View status:
```text
/gemini-limit
```
Update limits live:
```text
/gemini-limit 15 250000
```

---

## Validation & Testing

Run plugin validation against Hermes plugin contracts:
```bash
hermes plugins validate ~/.hermes/plugins/gemini-rate-limit
hermes plugins doctor ~/.hermes/plugins/gemini-rate-limit
```

Run automated test suite:
```bash
python3 tests/test_gemini_rate_limit.py
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
