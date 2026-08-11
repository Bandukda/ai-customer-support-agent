// Admin dashboard: live reasoning log (SSE with replay + dedupe), stat tiles,
// refund/escalation ledgers, CRM browser, and the policy document.

import { getJSON, openAdminStream } from "./api.js";
import {
  escapeHtml,
  fmtMoney,
  fmtNumber,
  fmtTime,
  prettyJson,
  renderPolicyMarkdown,
} from "./format.js";

const BADGES = {
  session_started: ["b-neutral", "session"],
  user_message: ["b-ink", "user"],
  llm_request: ["b-llm", "llm call"],
  llm_retry: ["b-warn", "llm retry"],
  assistant_thinking: ["b-llm", "thinking"],
  tool_call: ["b-tool", "tool call"],
  tool_result: ["b-tool", "tool result"],
  tool_retry: ["b-warn", "tool retry"],
  tool_error: ["b-bad", "tool error"],
  policy_decision: ["b-policy", "policy"],
  refund_processed: ["b-good", "refund"],
  escalation_created: ["b-warn", "escalation"],
  agent_response: ["b-neutral", "reply"],
  run_error: ["b-bad", "error"],
  run_completed: ["b-neutral", "done"],
};

const TYPE_GROUPS = {
  llm: new Set(["llm_request", "llm_retry", "assistant_thinking", "agent_response"]),
  tools: new Set(["tool_call", "tool_result", "tool_retry", "tool_error"]),
  policy: new Set(["policy_decision", "refund_processed", "escalation_created"]),
  failures: new Set(["llm_retry", "tool_retry", "tool_error", "run_error"]),
};

export function initAdmin() {
  const feedEl = document.getElementById("log-feed");
  const connDot = document.getElementById("admin-conn");
  const connText = document.getElementById("admin-conn-text");
  const sessionFilterEl = document.getElementById("log-session-filter");
  const autoscrollEl = document.getElementById("log-autoscroll");

  const seenIds = new Set();
  const events = [];
  const knownSessions = new Set();
  // "__current__" follows whichever chat session is live; "" shows everything;
  // anything else is a specific session id chosen from the dropdown.
  let sessionFilter = "__current__";
  let currentSessionId = null;
  let groupFilter = "all";
  let statsTimer = null;

  // ----- Live stream -----
  openAdminStream(
    (event) => {
      if (!event.id) {
        if (event.type === "replay_complete") connText.textContent = "live event stream";
        return;
      }
      if (seenIds.has(event.id)) return;
      seenIds.add(event.id);
      events.push(event);
      if (events.length > 3000) events.splice(0, events.length - 3000);

      if (!knownSessions.has(event.session_id)) {
        knownSessions.add(event.session_id);
        const option = document.createElement("option");
        option.value = event.session_id;
        option.textContent = event.session_id;
        sessionFilterEl.appendChild(option);
      }

      if (passesFilter(event)) appendRow(event);

      scheduleStats();
      if (event.type === "refund_processed") loadRefunds();
      if (event.type === "escalation_created") loadEscalations();
    },
    (connected) => {
      connDot.className = `conn-dot ${connected ? "ok" : "bad"}`;
      connText.textContent = connected ? "live event stream" : "reconnecting…";
    }
  );

  function passesFilter(event) {
    if (sessionFilter === "__current__") {
      // Before the first message of a new session there is nothing current,
      // so the feed sits empty rather than replaying the previous scenario.
      if (event.session_id !== currentSessionId) return false;
    } else if (sessionFilter && event.session_id !== sessionFilter) {
      return false;
    }
    if (groupFilter !== "all" && !TYPE_GROUPS[groupFilter]?.has(event.type)) return false;
    return true;
  }

  function renderEmptyState() {
    if (feedEl.children.length) return;
    const hint = document.createElement("div");
    hint.className = "empty-note";
    hint.textContent =
      sessionFilter === "__current__" && !currentSessionId
        ? "Waiting for the next message — earlier sessions are still in the dropdown above."
        : "No events match this filter.";
    feedEl.appendChild(hint);
  }

  // The chat view owns the session lifecycle; follow it when tracking "current".
  document.addEventListener("chat-session-changed", (e) => {
    currentSessionId = e.detail?.sessionId ?? null;
    if (sessionFilter === "__current__") rerenderFeed();
  });

  function appendRow(event) {
    feedEl.querySelector(".empty-note")?.remove();
    feedEl.appendChild(buildRow(event));
    while (feedEl.children.length > 800) feedEl.removeChild(feedEl.firstChild);
    if (autoscrollEl.checked) feedEl.scrollTop = feedEl.scrollHeight;
  }

  function buildRow(event) {
    const [badgeClass, badgeText] = BADGES[event.type] ?? ["b-neutral", event.type];
    const row = document.createElement("details");
    row.className = "log-row";

    const outcome =
      event.type === "policy_decision" ? event.data?.decision?.outcome ?? "" : "";
    const outcomeHtml = outcome
      ? `<span class="outcome-badge outcome-${outcome}">${outcome.replaceAll("_", " ")}</span>`
      : "";

    row.innerHTML = `
      <summary>
        <span class="log-time">${fmtTime(event.ts)}</span>
        <span class="badge ${badgeClass}">${badgeText}</span>
        <span class="log-session" title="session">${escapeHtml(event.session_id)}</span>
        <span class="log-label">${escapeHtml(event.label)}</span>
        ${outcomeHtml}
      </summary>
      <pre class="log-payload">${escapeHtml(prettyJson(event.data))}</pre>`;
    return row;
  }

  function rerenderFeed() {
    feedEl.innerHTML = "";
    for (const event of events) {
      if (passesFilter(event)) feedEl.appendChild(buildRow(event));
    }
    renderEmptyState();
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  sessionFilterEl.addEventListener("change", () => {
    sessionFilter = sessionFilterEl.value;
    rerenderFeed();
  });

  document.getElementById("log-type-chips").addEventListener("click", (clickEvent) => {
    const chip = clickEvent.target.closest(".chip");
    if (!chip) return;
    document.querySelectorAll("#log-type-chips .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    groupFilter = chip.dataset.group;
    rerenderFeed();
  });

  // ----- Stat tiles -----
  async function loadStats() {
    try {
      const stats = await getJSON("/api/admin/stats");
      document.getElementById("stat-sessions").textContent = fmtNumber(stats.sessions);
      document.getElementById("stat-refunds").textContent = fmtNumber(stats.refunds_processed);
      document.getElementById("stat-refund-amount").textContent = fmtMoney(stats.refunds_total_amount);
      document.getElementById("stat-denials").textContent = fmtNumber(stats.denials);
      document.getElementById("stat-escalations").textContent = fmtNumber(stats.escalations);
      document.getElementById("stat-retries").textContent = fmtNumber(stats.retries);
      document.getElementById("stat-tokens").textContent = fmtNumber(
        (stats.tokens?.input_tokens ?? 0) + (stats.tokens?.output_tokens ?? 0)
      );
    } catch {
      // stats are cosmetic; ignore transient failures
    }
  }
  function scheduleStats() {
    clearTimeout(statsTimer);
    statsTimer = setTimeout(loadStats, 400);
  }

  // ----- Ledger tables -----
  async function loadRefunds() {
    const { refunds } = await getJSON("/api/admin/refunds");
    const body = document.querySelector("#refunds-table tbody");
    document.getElementById("refunds-empty").hidden = refunds.length > 0;
    body.innerHTML = refunds
      .map(
        (r) => `<tr>
          <td><code>${r.id}</code></td>
          <td>${r.order_id}</td>
          <td>${r.customer_id}</td>
          <td>${r.item_skus.join(", ")}</td>
          <td class="num">${fmtMoney(r.amount)}</td>
          <td>${escapeHtml(r.method)}</td>
          <td>${r.processed_at}</td>
        </tr>`
      )
      .join("");
  }

  async function loadEscalations() {
    const { tickets } = await getJSON("/api/admin/escalations");
    const body = document.querySelector("#escalations-table tbody");
    document.getElementById("escalations-empty").hidden = tickets.length > 0;
    body.innerHTML = tickets
      .map(
        (t) => `<tr>
          <td><code>${t.id}</code></td>
          <td>${escapeHtml(t.customer_email)}</td>
          <td>${t.order_id ?? "—"}</td>
          <td>${escapeHtml(t.summary)}</td>
          <td>${escapeHtml(t.sla)}</td>
          <td>${escapeHtml(t.status)}</td>
        </tr>`
      )
      .join("");
  }

  // ----- CRM browser -----
  async function loadCrm() {
    const { customers } = await getJSON("/api/admin/customers");
    document.getElementById("crm-grid").innerHTML = customers
      .map(
        (customer) => `
      <div class="crm-card">
        <div class="crm-top">
          <span class="crm-name">${escapeHtml(customer.name)}</span>
          <span>
            ${customer.flags.map((f) => `<span class="flag-badge">${escapeHtml(f)}</span>`).join(" ")}
            <span class="tier-badge">${escapeHtml(customer.tier)}</span>
          </span>
        </div>
        <div class="crm-email">${escapeHtml(customer.email)}</div>
        <div class="crm-notes">${escapeHtml(customer.demo_notes)}</div>
        ${customer.orders
          .map(
            (order) => `
          <div class="crm-order">
            <span><code>${order.id}</code> · ${order.status}${order.refund_count ? " · refunded" : ""}</span>
            <span>${fmtMoney(order.items_total)}</span>
          </div>`
          )
          .join("")}
      </div>`
      )
      .join("");
  }

  // ----- Policy tab -----
  async function loadPolicy() {
    const { markdown } = await getJSON("/api/admin/policy");
    document.getElementById("policy-doc").innerHTML = renderPolicyMarkdown(markdown);
  }

  // ----- Sub-tabs -----
  document.querySelector(".admin-tabs").addEventListener("click", (clickEvent) => {
    const tab = clickEvent.target.closest(".admin-tab");
    if (!tab) return;
    document.querySelectorAll(".admin-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".admin-view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`.admin-view[data-view="${tab.dataset.tab}"]`).classList.add("active");
  });

  loadStats();
  loadRefunds();
  loadEscalations();
  loadCrm();
  loadPolicy();
  setInterval(loadStats, 15000);
}
