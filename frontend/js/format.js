// Formatting helpers shared by chat and admin views.

export function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// Minimal markdown (bold / italic / inline code / line breaks) for chat bubbles.
export function renderMarkdownLite(text) {
  let html = escapeHtml(text);
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>");
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  return html.replace(/\n/g, "<br>");
}

// Naive renderer for the policy document (headings, bullets, bold, code).
export function renderPolicyMarkdown(md) {
  const lines = md.split("\n");
  const out = [];
  let inList = false;
  const inline = (s) =>
    escapeHtml(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h2>${inline(line.slice(3))}</h2>`);
    } else if (line.startsWith("# ")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h1>${inline(line.slice(2))}</h1>`);
    } else if (/^\s*- /.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(line.replace(/^\s*- /, ""))}</li>`);
    } else if (/^\s*\d+\.\s/.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(line.replace(/^\s*\d+\.\s/, ""))}</li>`);
    } else if (line.trim() === "") {
      if (inList) { out.push("</ul>"); inList = false; }
    } else {
      if (inList) {
        out[out.length - 1] = out[out.length - 1].replace("</li>", ` ${inline(line.trim())}</li>`);
      } else {
        out.push(`<p>${inline(line)}</p>`);
      }
    }
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}

export function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return "";
  }
}

export function fmtMoney(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

export function fmtNumber(value) {
  return Number(value || 0).toLocaleString();
}

export function prettyJson(data) {
  return JSON.stringify(data, null, 2);
}

// Strip markdown decorations before text-to-speech.
export function plainTextForSpeech(text) {
  return String(text).replace(/\*\*?/g, "").replace(/`/g, "").replace(/\s+/g, " ").trim();
}
