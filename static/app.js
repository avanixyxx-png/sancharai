/* Shared front-end helpers.
   Every mutating request goes through post(), which attaches the CSRF token —
   so no page can forget it and get a silent 403. */

const CSRF = document.querySelector('meta[name="csrf"]')?.content || "";

function toast(message, kind = "") {
  let host = document.getElementById("toast");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

/** POST a form body with the CSRF token attached. Returns the parsed JSON. */
async function post(url, fields = {}) {
  const body = fields instanceof FormData ? fields : new FormData();
  if (!(fields instanceof FormData)) {
    for (const [k, v] of Object.entries(fields)) {
      if (v !== undefined && v !== null) body.append(k, v);
    }
  }
  body.append("csrf_token", CSRF);

  const res = await fetch(url, {
    method: "POST",
    body,
    headers: { "X-CSRF-Token": CSRF },
  });

  let data;
  try {
    data = await res.json();
  } catch {
    data = { ok: false, detail: `Server error (${res.status})` };
  }
  if (!res.ok && data.ok === undefined) data.ok = false;
  return data;
}

/** Wire a button to an action, handling the disabled/label/toast dance. */
async function action(btn, label, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try {
    await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

const GOOD = ["CONFIRMED", "INTERESTED", "PAYMENT_PROMISED"];
const BAD = [
  "NO_ANSWER", "FAILED", "BUSY", "CANCELED", "CANCELLED",
  "NOT_INTERESTED", "PAYMENT_REFUSED", "DNC",
];

function outcomeClass(s) {
  if (GOOD.includes(s)) return "t-good";
  if (BAD.includes(s)) return "t-bad";
  return "t-mid";
}

function pretty(s) {
  return (s || "").replace(/_/g, " ");
}

/** Filter any table by free text. */
function filterTable(tableId, query) {
  const q = (query || "").toLowerCase();
  document.querySelectorAll(`#${tableId} tbody tr`).forEach((row) => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}
