// App shell: view-mode tabs, provider status pill, and view bootstrapping.

import { getJSON } from "./api.js";
import { initAdmin } from "./admin.js";
import { initChat } from "./chat.js";
import { setTtsEngine } from "./voice.js";

function initModeTabs() {
  const layout = document.getElementById("layout");
  document.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".mode-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      layout.className = `layout mode-${tab.dataset.mode}`;
    });
  });
}

async function initConfigPill() {
  const dot = document.getElementById("config-dot");
  const label = document.getElementById("config-label");
  try {
    const config = await getJSON("/api/admin/config");
    setTtsEngine(config.tts_engine);
    // The chat view renders its voice controls before this fetch resolves;
    // tell it which engine ended up active.
    document.dispatchEvent(new CustomEvent("tts-engine-changed"));
    // provider_label names the vendor actually being called; config.provider is
    // the internal adapter ("openai" drives DeepSeek, Groq, Gemini and Ollama).
    const providerLabel = config.provider_label || config.provider;
    const voiceLabel = config.tts_engine === "elevenlabs" ? " · 🔊 ElevenLabs" : "";
    label.textContent = `${providerLabel} · ${config.model}${voiceLabel}`;
    if (config.base_url) {
      document.getElementById("config-pill").title = `Endpoint: ${config.base_url}`;
    }
    dot.className = `dot ${config.configured ? "ok" : "warn"}`;
    if (!config.configured) {
      label.textContent += " — API key missing";
      document.getElementById("config-pill").title =
        "Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) in the project .env file and restart.";
    }
  } catch {
    label.textContent = "backend unreachable";
    dot.className = "dot warn";
  }
}

initModeTabs();
initConfigPill();
initChat();
initAdmin();
