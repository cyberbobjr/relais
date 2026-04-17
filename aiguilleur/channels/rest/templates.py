"""HTML templates for the REST channel adapter server.

Centralised here so server.py stays within the 800-line file-size limit.
"""

SWAGGER_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>RELAIS REST API — Swagger UI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis],
      deepLinking: true,
    });
  </script>
</body>
</html>
"""

SSE_PLAYGROUND_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>RELAIS REST API — SSE Playground</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
           background: #1a1a2e; color: #e0e0e0; padding: 24px; }
    h1 { font-size: 1.3rem; margin-bottom: 16px; color: #8be9fd; }
    .row { display: flex; gap: 12px; margin-bottom: 12px; align-items: end; }
    label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 4px; }
    input, textarea { background: #16213e; border: 1px solid #333; color: #e0e0e0;
                      border-radius: 6px; padding: 8px 10px; font-family: inherit; font-size: 0.9rem; }
    input { width: 100%; }
    textarea { width: 100%; min-height: 60px; resize: vertical; }
    .col { flex: 1; }
    .col-sm { flex: 0 0 160px; }
    button { background: #0f3460; color: #e0e0e0; border: 1px solid #444; border-radius: 6px;
             padding: 8px 20px; cursor: pointer; font-size: 0.9rem; font-family: inherit; }
    button:hover { background: #1a5276; }
    button:disabled { opacity: 0.4; cursor: not-allowed; }
    button.stop { background: #6b2020; }
    button.stop:hover { background: #8b3030; }
    #output { background: #0a0a1a; border: 1px solid #333; border-radius: 8px;
              padding: 16px; min-height: 200px; max-height: 60vh; overflow-y: auto;
              white-space: pre-wrap; word-wrap: break-word; line-height: 1.6; font-size: 0.95rem; }
    .token { color: #f8f8f2; }
    .meta { color: #6272a4; font-size: 0.8rem; }
    .error { color: #ff5555; }
    .done { color: #50fa7b; }
    .info { color: #8be9fd; font-size: 0.85rem; }
    #status { font-size: 0.8rem; color: #888; margin-bottom: 8px; }
  </style>
</head>
<body>
  <h1>SSE Playground</h1>

  <div class="row">
    <div class="col">
      <label>API Key</label>
      <input type="password" id="apikey" placeholder="Bearer token" />
    </div>
    <div class="col-sm">
      <label>Session ID (optional)</label>
      <input type="text" id="session" placeholder="auto-generated" />
    </div>
  </div>

  <div class="row">
    <div class="col">
      <label>Message</label>
      <textarea id="content" placeholder="Type your message here..."></textarea>
    </div>
  </div>

  <div class="row">
    <button id="btn-send" onclick="sendSSE()">Send (SSE)</button>
    <button id="btn-stop" class="stop" onclick="stopSSE()" disabled>Stop</button>
    <button onclick="clearOutput()">Clear</button>
  </div>

  <div id="status"></div>
  <div id="output"></div>

  <script>
    let controller = null;

    function el(id) { return document.getElementById(id); }

    function append(html) {
      const o = el("output");
      o.innerHTML += html;
      o.scrollTop = o.scrollHeight;
    }

    function setStatus(text) { el("status").textContent = text; }
    function clearOutput() { el("output").innerHTML = ""; setStatus(""); }

    function stopSSE() {
      if (controller) { controller.abort(); controller = null; }
      el("btn-send").disabled = false;
      el("btn-stop").disabled = true;
      setStatus("Stopped.");
    }

    let tokenCount = 0;

    async function sendSSE() {
      const apikey = el("apikey").value.trim();
      const content = el("content").value.trim();
      if (!apikey || !content) { alert("API key and message are required."); return; }

      el("btn-send").disabled = true;
      el("btn-stop").disabled = false;
      tokenCount = 0;
      append('<span class="info">--- New request ---</span>\\n');
      setStatus("Connecting...");

      controller = new AbortController();
      const body = { content };
      const session = el("session").value.trim();
      if (session) body.session_id = session;

      try {
        const resp = await fetch("/v1/messages", {
          method: "POST",
          headers: {
            "Authorization": "Bearer " + apikey,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!resp.ok) {
          const err = await resp.text();
          append('<span class="error">HTTP ' + resp.status + ': ' + err + '</span>\\n');
          stopSSE();
          return;
        }

        // Check if server returned JSON instead of SSE (non-streaming fallback)
        const ct = resp.headers.get("Content-Type") || "";
        if (ct.includes("application/json")) {
          const data = await resp.json();
          append('<span class="token">' + escapeHtml(data.content || "") + '</span>\\n');
          append('<span class="done">--- Done (non-streaming) ---</span>\\n');
          if (data.session_id) { el("session").value = data.session_id; }
          stopSSE();
          return;
        }

        setStatus("Streaming...");
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\\n");
          buffer = lines.pop();

          let eventType = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith("data: ")) {
              const raw = line.slice(6);
              try {
                const data = JSON.parse(raw);
                if (eventType === "token" && data.t) {
                  tokenCount++;
                  append('<span class="token">' + escapeHtml(data.t) + '</span>');
                  setStatus("Streaming... " + tokenCount + " tokens");
                } else if (eventType === "done") {
                  // If no tokens were streamed, display the full content
                  if (tokenCount === 0 && data.content) {
                    append('<span class="token">' + escapeHtml(data.content) + '</span>\\n');
                  }
                  append('\\n<span class="done">--- Done (' + tokenCount + ' tokens) ---</span>\\n');
                  if (data.session_id) {
                    el("session").value = data.session_id;
                    append('<span class="meta">session=' + escapeHtml(data.session_id) + '</span>\\n');
                  }
                  if (data.correlation_id) {
                    append('<span class="meta">corr=' + escapeHtml(data.correlation_id) + '</span>\\n');
                  }
                } else if (eventType === "progress") {
                  setStatus(data.event + ": " + (data.detail || ""));
                } else if (eventType === "error") {
                  append('\\n<span class="error">Error: ' + escapeHtml(data.error || raw) + '</span>\\n');
                } else if (line.trim()) {
                  append('<span class="meta">[' + eventType + '] ' + escapeHtml(raw) + '</span>\\n');
                }
              } catch (e) {
                if (raw.trim()) {
                  append('<span class="meta">' + escapeHtml(raw) + '</span>\\n');
                }
              }
              eventType = "";
            }
          }
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          append('<span class="error">' + escapeHtml(e.message) + '</span>\\n');
        }
      }

      stopSSE();
    }

    function escapeHtml(s) {
      return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }

    // Ctrl+Enter to send
    el("content").addEventListener("keydown", function(e) {
      if (e.ctrlKey && e.key === "Enter") { e.preventDefault(); sendSSE(); }
    });
  </script>
</body>
</html>
"""
