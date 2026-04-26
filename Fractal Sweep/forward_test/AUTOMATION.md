# Auto-Log Setup — TradingView → Google Sheets

This wires up your Fractal Sweep indicator's alerts to auto-populate a row in your forward-test spreadsheet every time a setup fires. Manual entry of `actual_entry`, `actual_exit`, `outcome`, `r_realized`, `mae_R`, `mfe_R`, `slippage_pts` still required (those need eyeballs after the trade resolves), but the planned-trade fields fill themselves.

## What gets automated

| Field | Auto-filled from alert | You fill in |
|---|---|---|
| `trade_no` | ✓ (auto-incremented) | |
| `date`, `time_et` | ✓ (NY-local from `fired_at_ms`) | |
| `combo`, `direction`, `smt` | ✓ | |
| `planned_entry`, `planned_sl`, `planned_tp` | ✓ | |
| `planned_risk_pts` | ✓ (auto-formula already in sheet) | |
| `contracts` | ✓ (sized to your $225 risk) | |
| `notes` | ✓ (gets `alert_id=...` for dedup) | |
| `actual_entry`, `actual_exit` | | ✓ after fill |
| `outcome`, `r_realized` | | ✓ after trade closes |
| `mae_R`, `mfe_R`, `slippage_pts` | | ✓ eyeball from chart |

You go from typing ~12 fields per trade to typing ~6.

---

## Setup (one-time, ~15 minutes)

### 1. Open Apps Script editor in your Sheet

In the Google Sheet you already imported the tracker into:

1. **Extensions → Apps Script**
2. A new tab opens with a code editor showing `Code.gs`

### 2. Paste the webhook code

1. Delete everything in `Code.gs` (it usually starts with an empty `function myFunction()`)
2. Open `webhook_apps_script.js` from this folder, copy all of it, and paste into `Code.gs`
3. Click the floppy-disk **Save** icon (or `⌘S`)

### 3. (Optional but recommended) Set a shared secret

In `Code.gs`, find the line:

```javascript
const SHARED_SECRET = "";
```

Replace `""` with a random string of your choice — e.g. `"sweep-fwd-7K3qP9wL"`. Save.

This prevents random people who guess your URL from injecting fake trade rows. The indicator's alert payload will need to include the same token (we'll add it in Step 5).

### 4. Deploy as a web app

1. Click **Deploy → New deployment** (top right)
2. Click the gear icon next to "Select type" → **Web app**
3. Fill in:
   - **Description**: `Fractal Sweep webhook receiver`
   - **Execute as**: `Me (your-email@gmail.com)`
   - **Who has access**: `Anyone` (this means "anyone with the URL"; the URL is unguessable)
4. Click **Deploy**
5. Google will prompt for permissions — **Authorize access**, choose your account, click "Advanced" → "Go to (project name) (unsafe)" → **Allow**
6. Copy the **Web app URL** at the top of the success dialog. It looks like:
   ```
   https://script.google.com/macros/s/AKfyc...long-string.../exec
   ```
   **Save this URL — you'll paste it into TradingView next.**

### 5. Test the deployment

In your browser, visit the Web app URL directly (just paste into the address bar). You should see:

```json
{
  "ok": true,
  "service": "Fractal Sweep webhook receiver",
  "sheet": "Trades",
  "auth_enabled": true,
  "hint": "POST a JSON payload to this URL from TradingView's alert webhook.",
  "http_status": 200
}
```

If you see this, the webhook is live. If you see a Google login page or "Authorization required" page, redeploy with **Who has access: Anyone** (not "Anyone in your domain").

### 6. Test from Apps Script (sanity check before connecting TradingView)

Back in the Apps Script editor:

1. In the function dropdown at the top, select `_testHandler`
2. Click **Run**
3. Check your Google Sheet's Trades tab — a new row should appear at the next empty slot, with `combo=1H/5M`, `direction=LONG`, `entry=21000.50`, etc.

If the test row appears, the script is working. **Delete that test row** (or leave it as note saying "sample") before connecting the live alert.

### 7. Wire up the TradingView alert

In TradingView with the Fractal Sweep indicator on your 5M NQ1! chart:

1. Right-click the chart → **Add alert** (or click the alarm-clock icon)
2. **Condition**: `Fractal Sweep` → `Any alert() function call`
3. **Notifications tab**:
   - ✅ **Webhook URL**: paste your Web app URL from step 4
   - The Message field will be auto-populated by the indicator's `alert()` payload — leave it as the default `{{strategy.order.alert_message}}` style placeholder, OR explicitly set it to `{{plot_0}}` or whatever the indicator passes through. Actually for `alert()` calls, just leave the Message field blank or set to `Fractal Sweep alert` — the JSON payload is hardcoded inside the indicator's `alert()` call, not in TradingView's UI message.

   **IMPORTANT**: TradingView passes whatever string the indicator's `alert()` function emitted as the request body. Since our indicator calls `alert('{"v":1,"id":...}', ...)`, the JSON gets sent as-is. You don't need to type a payload in TradingView.

4. **Expiration**: pick a far-future date or "Open-ended"
5. **Frequency**: matches what's already in the indicator code (`alert.freq_once_per_bar_close`)
6. Click **Create**

### 8. Add the auth token to the indicator (only if you set SHARED_SECRET in step 3)

If you set a `SHARED_SECRET`, the indicator's alert payload needs to include it as a `"token"` field. Edit `pine/fractal_sweep.pine`:

Find the long alert string for LONG (around line 1081) and the SHORT version (around line 1136). Add `"token":"your-secret-here"` to each JSON. Example:

```pine
alert('{"v":1,"id":"FS-' + syminfo.ticker + '-' + str.tostring(time) + '-LONG",' +
      '"source":"fractal_sweep","ticker":"' + syminfo.ticker + '",' +
      '"token":"sweep-fwd-7K3qP9wL",' +    // ← ADD THIS LINE
      '"combo":"' + combo + '","direction":"LONG",' +
      ...
```

Re-save the indicator on TradingView. From now on every alert payload will contain the token, and the webhook will accept it.

---

## Verifying it works

Wait for the next live alert from the indicator (or trigger one manually by replaying historical bars on the chart). Within ~5 seconds of the alert firing, a new row should appear in the Trades tab with all the planned-trade fields filled in.

If it doesn't:

1. **Check the alert log** in TradingView (alarm-clock icon → past alerts). Look for the most recent fire and see the response status.
2. **Check Apps Script execution log**: in the editor, **Executions** (left sidebar). Each `doPost` invocation is logged with input payload and any errors.
3. **Common fixes**:
   - If response is `auth failed` (401): your indicator's `token` doesn't match `SHARED_SECRET`. Verify they're identical.
   - If response is `Sheet "Trades" not found`: your tab is renamed. Either rename it back to `Trades` or change `SHEET_NAME` at the top of the Apps Script.
   - If response is `no payload`: TradingView isn't sending the indicator's alert string as the request body. Make sure the alert is configured for "Any alert() function call" condition (not a specific plot crossing).

---

## Maintenance

- **If you redeploy the Apps Script** (e.g. to add a feature), the URL stays the same as long as you choose **Manage deployments → Edit → New version**. If you create a brand-new deployment, you get a new URL and need to update TradingView.
- **If you accidentally invite errors**: the Apps Script editor's **Executions** tab keeps a history of every webhook call with full request/response. Use it to diagnose any missing trades.
- **Quotas**: Apps Script free tier allows 20,000 URL-fetch invocations/day. At ~12 trades/month, this is laughably over-provisioned.

---

## Limits and caveats

- **Free tier delivery latency**: TradingView Pro+ accounts deliver webhooks within ~2 seconds. Free accounts have severely throttled webhook delivery (often delayed 10+ minutes or dropped). If you're on Free, this won't work reliably; upgrade to Pro+ ($14.95/mo) or stay manual.
- **TradingView only sends alerts when the chart is loaded by the server**: you don't need to be watching the chart, but the indicator does need to be active on at least one TradingView account. Server-side scanning is a Pro+ feature.
- **Apps Script doesn't support real HTTP status codes** — every response is 200 OK with the actual status embedded in the JSON body. TradingView won't retry on failures, so monitor your trade log against your TradingView alert history weekly.
- **Outcome tracking is still manual** — this script only logs the entry signal. After the trade resolves, you fill in `outcome`, `r_realized`, MAE/MFE etc. by hand.

---

## Going further (future work)

If you want full automation including outcome tracking, two paths:

1. **Periodic scraper**: Apps Script has time-based triggers. A function could run every 30 minutes, walk open trades in the sheet, fetch current price from a free quote API, compute MAE/MFE and outcome, and update the row. ~1-2 hours of work.
2. **Tradovate Bridge integration**: extend the existing `tradovate-bridge` branch's webhook receiver to also write outcomes back to the sheet via the Apps Script's URL. Requires the bridge to run on your machine 24/7 but gives you broker-grade fill quality data.

Both are out of scope for the initial paper-trading forward test. Manual outcome entry is fine for the first ~50 trades — it forces you to look at every trade carefully.
