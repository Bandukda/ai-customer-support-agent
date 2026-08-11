// Voice layer.
//
// Speech-to-text: browser-native Web Speech API (SpeechRecognition) — no key,
// works offline-ish, streams interim results as the customer speaks.
//
// Text-to-speech: ElevenLabs when the backend has a key configured (audio is
// proxied through /api/tts so the key never reaches the browser), with the
// browser's speechSynthesis as an automatic fallback. Any ElevenLabs failure —
// no key, bad voice ID, quota exhausted, network down — silently degrades to
// browser speech rather than breaking the conversation.

const SpeechRecognitionImpl =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;

export const sttSupported = Boolean(SpeechRecognitionImpl);
export const ttsSupported = "speechSynthesis" in window;

let ttsEngine = "browser"; // set from /api/admin/config at startup
let currentAudio = null;

export function setTtsEngine(engine) {
  ttsEngine = engine === "elevenlabs" ? "elevenlabs" : "browser";
}

export function getTtsEngine() {
  return ttsEngine;
}

export function createDictation({ onInterim, onFinal, onStateChange }) {
  if (!sttSupported) return null;
  const recognition = new SpeechRecognitionImpl();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;

  let active = false;

  recognition.onstart = () => {
    active = true;
    onStateChange(true);
  };
  recognition.onend = () => {
    active = false;
    onStateChange(false);
  };
  recognition.onerror = () => {
    active = false;
    onStateChange(false);
  };
  recognition.onresult = (event) => {
    let interim = "";
    let final = "";
    for (const result of event.results) {
      if (result.isFinal) final += result[0].transcript;
      else interim += result[0].transcript;
    }
    if (interim) onInterim(interim);
    if (final) onFinal(final.trim());
  };

  return {
    start() {
      if (!active) {
        try { recognition.start(); } catch { /* already starting */ }
      }
    },
    stop() {
      if (active) recognition.stop();
    },
    get active() {
      return active;
    },
  };
}

/** Speak text aloud. Returns the engine that actually produced the audio. */
export async function speak(text) {
  const clean = (text || "").trim();
  if (!clean) return null;
  stopSpeaking();

  if (ttsEngine === "elevenlabs") {
    try {
      const response = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: clean }),
      });
      if (response.ok) {
        const url = URL.createObjectURL(await response.blob());
        const audio = new Audio(url);
        currentAudio = audio;
        audio.addEventListener("ended", () => URL.revokeObjectURL(url), { once: true });
        await audio.play();
        return "elevenlabs";
      }
      const detail = await response.json().catch(() => ({}));
      console.warn(
        `ElevenLabs TTS unavailable (HTTP ${response.status}): ${detail.detail ?? ""} — using browser speech.`
      );
    } catch (error) {
      // Covers network failures and blocked playback (browser autoplay policy).
      console.warn(`ElevenLabs TTS failed (${error.message}) — using browser speech.`);
    }
  }

  return browserSpeak(clean);
}

function browserSpeak(text) {
  if (!ttsSupported) return null;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.04;
  window.speechSynthesis.speak(utterance);
  return "browser";
}

export function stopSpeaking() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  if (ttsSupported) window.speechSynthesis.cancel();
}
