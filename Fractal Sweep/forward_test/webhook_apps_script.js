/**
 * Fractal Sweep — Forward-Test Webhook Receiver
 *
 * Paste this into your Google Sheet's Apps Script editor:
 *   Extensions → Apps Script → replace Code.gs contents → Save → Deploy
 *
 * What it does:
 *   - Receives a POST from TradingView (or any webhook source) with a JSON
 *     payload matching the Fractal Sweep indicator's alert format
 *   - Appends a new row to the "Trades" tab with the planned-trade fields
 *     auto-filled (date, time_et, combo, direction, smt, planned_entry,
 *     planned_sl, planned_tp). You fill in actuals + outcome manually.
 *
 * Required payload fields (sent by the indicator's alert() call):
 *   id, ticker, combo, direction, entry, sl, tp, risk_pts, smt, fired_at_ms
 *
 * Optional but recommended:
 *   token  — shared secret for basic auth (set SHARED_SECRET below)
 */

// ─── CONFIG ──────────────────────────────────────────────────────────────────
// Set this to a long random string. The indicator's alert payload must include
// "token":"<this value>" to be accepted. Leave as empty string to disable auth
// (NOT recommended; the deployment URL is unguessable but not secret).
const SHARED_SECRET = "";

const SHEET_NAME = "Trades";

// Map from JSON payload field → spreadsheet column header.
// Add/remove entries here without touching the rest of the script.
const FIELD_TO_HEADER = {
  date:           "date",
  time_et:        "time_et",
  combo:          "combo",
  direction:      "direction",
  smt:            "smt",
  planned_entry:  "planned_entry",
  planned_sl:     "planned_sl",
  planned_tp:     "planned_tp",
  contracts:      "contracts",
  notes:          "notes",
};


// ─── ENTRY POINT ─────────────────────────────────────────────────────────────
function doPost(e) {
  try {
    const payload = parsePayload_(e);
    if (!payload) {
      return jsonResponse_(400, { ok: false, error: "no payload" });
    }

    if (SHARED_SECRET && payload.token !== SHARED_SECRET) {
      return jsonResponse_(401, { ok: false, error: "auth failed" });
    }

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
    if (!sheet) {
      return jsonResponse_(500, {
        ok: false,
        error: `Sheet "${SHEET_NAME}" not found. Make sure your Trades tab is named exactly "${SHEET_NAME}".`,
      });
    }

    // Dedup: if a row with this `id` already exists, skip.
    if (payload.id && hasRowWithId_(sheet, payload.id)) {
      return jsonResponse_(200, {
        ok: true,
        deduped: true,
        id: payload.id,
        message: "row with this id already exists; ignoring duplicate",
      });
    }

    const row = buildRow_(sheet, payload);
    appendRowAtNextEmpty_(sheet, row);

    return jsonResponse_(200, { ok: true, id: payload.id, ticker: payload.ticker });
  } catch (err) {
    return jsonResponse_(500, { ok: false, error: String(err), stack: err.stack });
  }
}


// Quick health check — visit the deployment URL in a browser to verify it's live.
function doGet(e) {
  return jsonResponse_(200, {
    ok: true,
    service: "Fractal Sweep webhook receiver",
    sheet: SHEET_NAME,
    auth_enabled: !!SHARED_SECRET,
    hint: "POST a JSON payload to this URL from TradingView's alert webhook.",
  });
}


// ─── HELPERS ─────────────────────────────────────────────────────────────────

function parsePayload_(e) {
  // TradingView posts the alert message as the raw request body (not as form data).
  // The body is plain text; it's JSON if the alert message itself is a JSON
  // string (which our indicator emits).
  if (!e || !e.postData || !e.postData.contents) return null;
  const raw = e.postData.contents;
  try {
    return JSON.parse(raw);
  } catch (err) {
    // If parsing fails, log the raw body for debugging.
    console.error("Failed to parse payload as JSON:", raw);
    return null;
  }
}


function buildRow_(sheet, payload) {
  // Read the header row once and build a column→index lookup.
  const lastCol = sheet.getLastColumn();
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  const headerToIdx = {};
  headers.forEach((h, i) => { headerToIdx[String(h).trim()] = i; });

  // Initialize an empty row of the correct width.
  const row = new Array(lastCol).fill("");

  // Find the next trade_no by counting non-empty rows in the trade_no column.
  const tradeNoIdx = headerToIdx["trade_no"];
  if (tradeNoIdx !== undefined) {
    row[tradeNoIdx] = nextTradeNumber_(sheet, tradeNoIdx);
  }

  // Convert fired_at_ms epoch → NY local date + time strings.
  if (payload.fired_at_ms) {
    const ts = new Date(Number(payload.fired_at_ms));
    payload.date    = formatDateNY_(ts);
    payload.time_et = formatTimeNY_(ts);
  }

  // Map payload fields → sheet columns via FIELD_TO_HEADER.
  for (const [field, header] of Object.entries(FIELD_TO_HEADER)) {
    const colIdx = headerToIdx[header];
    if (colIdx === undefined) continue;
    const value = payload[field];
    if (value === undefined || value === null) continue;

    if (field === "smt") {
      // Indicator sends boolean; render as TRUE/FALSE for the sheet.
      row[colIdx] = value === true || value === "true" ? "TRUE" : "FALSE";
    } else {
      row[colIdx] = value;
    }
  }

  // Stash the alert id in the notes column so dedup works on subsequent fires.
  // This also gives you a paper trail if you ever need to debug a duplicate.
  if (payload.id) {
    const notesIdx = headerToIdx["notes"];
    if (notesIdx !== undefined) {
      const existing = row[notesIdx] ? String(row[notesIdx]) + " | " : "";
      row[notesIdx] = existing + "alert_id=" + payload.id;
    }
  }

  return row;
}


function nextTradeNumber_(sheet, tradeNoColIdx) {
  // Find the highest existing trade_no, return +1.
  // Reads only the first column — fast even with 500 pre-numbered rows.
  const colValues = sheet.getRange(2, tradeNoColIdx + 1, sheet.getLastRow() - 1, 1).getValues();
  let maxNo = 0;
  let highestNonEmptyRow = 0;
  for (let i = 0; i < colValues.length; i++) {
    const v = colValues[i][0];
    if (typeof v === "number" && v > maxNo) {
      maxNo = v;
    }
    // Track which row is the next one with NO trade data filled in (only trade_no).
    // We need this so the receiver writes at the correct row, not at the bottom.
  }
  return maxNo + 1; // simple: next sequential number
}


function appendRowAtNextEmpty_(sheet, row) {
  // The template pre-populates rows with `trade_no` already set (1, 2, 3, ...).
  // We need to find the first row where the "outcome" column (or any payload
  // column) is empty, and write our values there — overwriting the empty slot
  // rather than appending a brand-new row at the bottom.
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  let datColIdx = headers.indexOf("date");
  if (datColIdx < 0) datColIdx = 1; // fallback — should never happen

  const lastRow = sheet.getLastRow();
  const dateColValues = sheet
    .getRange(2, datColIdx + 1, lastRow - 1, 1)
    .getValues();

  // First row where the date column is still empty (i.e., no trade logged yet).
  let targetRow = -1;
  for (let i = 0; i < dateColValues.length; i++) {
    if (dateColValues[i][0] === "" || dateColValues[i][0] === null) {
      targetRow = i + 2; // +2 because data starts at row 2 (1-indexed)
      break;
    }
  }

  if (targetRow < 0) {
    // All pre-populated rows are filled. Append a brand-new row.
    targetRow = lastRow + 1;
  }

  // Write the row.
  sheet.getRange(targetRow, 1, 1, row.length).setValues([row]);
}


function hasRowWithId_(sheet, id) {
  // Check the notes column for "alert_id=<id>". Cheap O(N) scan but fine for
  // forward-test sample sizes (<500 trades).
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const notesIdx = headers.indexOf("notes");
  if (notesIdx < 0) return false;

  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return false;

  const notes = sheet.getRange(2, notesIdx + 1, lastRow - 1, 1).getValues();
  const needle = "alert_id=" + id;
  for (let i = 0; i < notes.length; i++) {
    const v = String(notes[i][0] || "");
    if (v.indexOf(needle) !== -1) return true;
  }
  return false;
}


// ─── DATE / TIME HELPERS (NY local — matches `time_et` semantics) ───────────

function formatDateNY_(d) {
  return Utilities.formatDate(d, "America/New_York", "yyyy-MM-dd");
}

function formatTimeNY_(d) {
  return Utilities.formatDate(d, "America/New_York", "HH:mm");
}


// ─── RESPONSE WRAPPER ────────────────────────────────────────────────────────

function jsonResponse_(_status, body) {
  // Apps Script's ContentService doesn't support custom HTTP status codes —
  // every response is 200 OK. We include the status in the body so callers
  // (and you, when debugging) can tell what happened.
  body.http_status = _status;
  return ContentService
    .createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}


// ─── MANUAL TEST FUNCTION (run from Apps Script editor for local debugging) ──

function _testHandler() {
  const fakeEvent = {
    postData: {
      contents: JSON.stringify({
        v: 1,
        id: "FS-NQM2026-1714000000000-LONG",
        source: "fractal_sweep",
        ticker: "NQM2026",
        combo: "1H/5M",
        direction: "LONG",
        entry: 21000.50,
        sl: 20990.25,
        tp: 21010.75,
        risk_pts: 10.25,
        rr: 1.0,
        contracts: 1,
        smt: true,
        fired_at_ms: Date.now(),
        token: SHARED_SECRET || undefined,
      }),
    },
  };
  const resp = doPost(fakeEvent);
  console.log("Test response:", resp.getContent());
}
