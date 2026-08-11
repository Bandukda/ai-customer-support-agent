// API client: JSON endpoints plus the two SSE channels
// (per-turn chat stream via fetch, global admin stream via EventSource).

export async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} -> HTTP ${response.status}`);
  return response.json();
}

// POST /api/chat streams this turn's reasoning events as SSE. EventSource
// cannot POST, so we parse the stream manually from fetch.
export async function streamChat({ sessionId, message }, onEvent) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!response.ok || !response.body) {
    throw new Error(`chat request failed (HTTP ${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)));
          } catch {
            // ignore malformed frames
          }
        }
      }
    }
  }
}

export function openAdminStream(onEvent, onStatus) {
  const source = new EventSource("/api/admin/stream");
  source.onopen = () => onStatus(true);
  source.onerror = () => onStatus(false);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
    } catch {
      // ignore malformed frames
    }
  };
  return source;
}
