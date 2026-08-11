// Customer chat view: sends turns to /api/chat, renders the live status line
// from streamed reasoning events, and integrates voice input/output.

import { getJSON, streamChat } from "./api.js";
import { escapeHtml, fmtTime, plainTextForSpeech, renderMarkdownLite } from "./format.js";
import {
  createDictation,
  getTtsEngine,
  speak,
  sttSupported,
  stopSpeaking,
  ttsSupported,
} from "./voice.js";

const TOOL_STATUS = {
  lookup_customer: "Looking up the customer account…",
  get_order: "Fetching order details…",
  check_refund_eligibility: "Checking the refund policy engine…",
  process_refund: "Processing the refund…",
  escalate_to_human: "Creating a ticket for the support team…",
};

export function initChat() {
  const messagesEl = document.getElementById("chat-messages");
  const statusEl = document.getElementById("agent-status");
  const statusTextEl = document.getElementById("agent-status-text");
  const sessionEl = document.getElementById("chat-session-id");
  const form = document.getElementById("composer");
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("btn-send");
  const micBtn = document.getElementById("btn-mic");
  const ttsBtn = document.getElementById("btn-tts");

  let sessionId = null;
  let busy = false;
  let ttsEnabled = localStorage.getItem("meridian_tts") === "1";

  function setStatus(text) {
    if (text) {
      statusTextEl.textContent = text;
      statusEl.hidden = false;
    } else {
      statusEl.hidden = true;
    }
  }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    input.disabled = value;
    if (!value) {
      setStatus(null);
      hideThinking();
      discardStreamed();
      input.focus();
    }
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addMessage(role, text) {
    const wrapper = document.createElement("div");
    wrapper.className = `msg ${role}`;
    const who = role === "customer" ? "You" : "Meridian Assist";
    wrapper.innerHTML = `
      <div class="bubble">${renderMarkdownLite(text)}</div>
      <div class="meta">${who} · ${fmtTime(new Date().toISOString())}</div>`;
    messagesEl.appendChild(wrapper);
    scrollToBottom();
  }

  // ----- Thinking indicator + token streaming -----
  // The agent may stream text and only then decide to call a tool, in which
  // case that text was an internal working note rather than the reply. So
  // streamed text lives in a provisional bubble that is discarded whenever a
  // tool call or a new LLM call follows, and is only committed by the
  // authoritative `agent_response` event.
  let thinkingEl = null;
  let liveEl = null;
  let liveBubbleEl = null;
  let liveText = "";

  function showThinking() {
    if (thinkingEl || liveEl) return;
    thinkingEl = document.createElement("div");
    thinkingEl.className = "msg agent thinking";
    thinkingEl.innerHTML =
      `<div class="bubble"><span class="typing" role="status" aria-label="Agent is thinking">` +
      `<i></i><i></i><i></i></span></div>`;
    messagesEl.appendChild(thinkingEl);
    scrollToBottom();
  }

  function hideThinking() {
    thinkingEl?.remove();
    thinkingEl = null;
  }

  function discardStreamed() {
    liveEl?.remove();
    liveEl = null;
    liveBubbleEl = null;
    liveText = "";
  }

  function appendDelta(fragment) {
    if (!fragment) return;
    if (!liveEl) {
      hideThinking();
      liveEl = document.createElement("div");
      liveEl.className = "msg agent streaming";
      liveEl.innerHTML =
        `<div class="bubble"></div>` +
        `<div class="meta">Meridian Assist · ${fmtTime(new Date().toISOString())}</div>`;
      liveBubbleEl = liveEl.querySelector(".bubble");
      messagesEl.appendChild(liveEl);
    }
    liveText += fragment;
    // textContent while streaming: partial markdown renders as garbage, and it
    // keeps untrusted model output out of innerHTML until the final render.
    liveBubbleEl.textContent = liveText;
    scrollToBottom();
  }

  function commitReply(text) {
    hideThinking();
    if (liveEl && liveBubbleEl) {
      liveEl.classList.remove("streaming");
      liveBubbleEl.innerHTML = renderMarkdownLite(text);
      liveEl = null;
      liveBubbleEl = null;
      liveText = "";
      scrollToBottom();
      return;
    }
    // No deltas arrived — non-streaming provider, or an error fallback.
    addMessage("agent", text);
  }

  function handleEvent(event) {
    switch (event.type) {
      case "session_info":
        sessionId = event.session_id;
        sessionEl.textContent = sessionId;
        // Tell the admin log which session is live, so its "Current session"
        // filter can follow along without the user picking from the dropdown.
        document.dispatchEvent(
          new CustomEvent("chat-session-changed", { detail: { sessionId } })
        );
        break;
      case "llm_request":
        // A fresh model call supersedes anything streamed so far this turn.
        discardStreamed();
        showThinking();
        if (event.data?.iteration === 1 && event.data?.attempt === 1) {
          setStatus("Thinking…");
        }
        break;
      case "response_delta":
        appendDelta(event.data?.text ?? "");
        break;
      case "llm_retry":
        discardStreamed();
        showThinking();
        setStatus(`Hit a transient issue — retrying (attempt ${event.data?.attempt ?? "?"})…`);
        break;
      case "assistant_thinking":
        if (event.data?.text) setStatus(event.data.text);
        break;
      case "tool_call":
        // Text streamed before a tool call was a working note, not the reply.
        discardStreamed();
        showThinking();
        setStatus(TOOL_STATUS[event.data?.tool] || `Running ${event.data?.tool}…`);
        break;
      case "tool_retry":
        setStatus("A system hiccuped — retrying that step…");
        break;
      case "tool_error":
        setStatus("Working around an issue…");
        break;
      case "agent_response":
        commitReply(event.data?.text ?? "");
        if (ttsEnabled) speak(plainTextForSpeech(event.data?.text ?? ""));
        break;
      case "run_completed":
      case "stream_timeout":
        setBusy(false);
        break;
      default:
        break;
    }
  }

  async function send(text) {
    const message = text.trim();
    if (!message || busy) return;
    addMessage("customer", message);
    input.value = "";
    autosize();
    setBusy(true);
    setStatus("Sending…");
    // Immediately, not at the first llm_request: the gap before the model
    // starts producing is exactly the stretch the indicator exists to cover.
    showThinking();
    try {
      await streamChat({ sessionId, message }, handleEvent);
    } catch (error) {
      discardStreamed();
      hideThinking();
      addMessage("agent", `⚠️ Could not reach the server: ${escapeHtml(error.message)}`);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send(input.value);
    }
  });

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }
  input.addEventListener("input", autosize);

  // ----- New session -----
  document.getElementById("btn-new-session").addEventListener("click", () => {
    sessionId = null;
    sessionEl.textContent = "new";
    stopSpeaking();
    // Drop the element references too, not just the nodes.
    discardStreamed();
    hideThinking();
    messagesEl.querySelectorAll(".msg").forEach((el) => el.remove());
    // The previous session's events stay on the bus — the admin log just stops
    // showing them, and they remain reachable from its session dropdown.
    document.dispatchEvent(
      new CustomEvent("chat-session-changed", { detail: { sessionId: null } })
    );
    input.focus();
  });

  // ----- Voice: speech-to-text -----
  const dictation = createDictation({
    onInterim: (text) => {
      input.value = text;
    },
    onFinal: (text) => {
      input.value = text;
      send(text);
    },
    onStateChange: (listening) => {
      micBtn.classList.toggle("recording", listening);
      micBtn.title = listening ? "Listening… click to stop" : "Speak instead of typing";
    },
  });
  if (!sttSupported) {
    micBtn.disabled = true;
    micBtn.title = "Speech recognition is not supported in this browser (use Chrome)";
  } else {
    micBtn.addEventListener("click", () => {
      if (dictation.active) dictation.stop();
      else dictation.start();
    });
  }

  // ----- Voice: text-to-speech toggle -----
  function renderTtsState() {
    const engine = getTtsEngine() === "elevenlabs" ? "ElevenLabs" : "browser voice";
    ttsBtn.classList.toggle("active", ttsEnabled);
    ttsBtn.setAttribute("aria-pressed", String(ttsEnabled));
    ttsBtn.title = ttsEnabled
      ? `Voice replies ON (${engine}) — click to mute`
      : `Read agent replies aloud (${engine})`;
  }
  if (!ttsSupported) {
    ttsBtn.disabled = true;
  } else {
    ttsBtn.addEventListener("click", () => {
      ttsEnabled = !ttsEnabled;
      localStorage.setItem("meridian_tts", ttsEnabled ? "1" : "0");
      if (!ttsEnabled) stopSpeaking();
      renderTtsState();
    });
    renderTtsState();
    document.addEventListener("tts-engine-changed", renderTtsState);
  }

  initPersonasDialog();
  input.focus();
}

// Demo-customer picker: seeded profiles with copyable emails / order IDs.
function initPersonasDialog() {
  const dialog = document.getElementById("personas-dialog");
  const listEl = document.getElementById("personas-list");
  let loaded = false;

  document.getElementById("btn-personas").addEventListener("click", async () => {
    dialog.showModal();
    if (loaded) return;
    try {
      const { customers } = await getJSON("/api/admin/customers");
      listEl.innerHTML = customers
        .map(
          (customer) => `
        <div class="persona-card">
          <div class="persona-top">
            <span class="persona-name">${escapeHtml(customer.name)}</span>
            <button class="copyable" data-copy="${escapeHtml(customer.email)}">${escapeHtml(customer.email)}</button>
          </div>
          <div class="persona-scenario">${escapeHtml(customer.demo_notes)}</div>
          <div class="persona-orders">
            ${customer.orders
              .map(
                (order) =>
                  `<button class="copyable" data-copy="${order.id}">${order.id} · ${order.status} · $${order.items_total.toFixed(2)}</button>`
              )
              .join("")}
          </div>
        </div>`
        )
        .join("");
      loaded = true;
    } catch (error) {
      listEl.textContent = `Failed to load: ${error.message}`;
    }
  });

  document.getElementById("personas-close").addEventListener("click", () => dialog.close());
  listEl.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    await navigator.clipboard.writeText(button.dataset.copy.split(" ·")[0]);
    button.classList.add("copied");
    setTimeout(() => button.classList.remove("copied"), 900);
  });
}
