/* FinTrace web client: one page, no build step.
   The token never leaves localStorage except as an Authorization header. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const tokenInput = $("token");
  const questionInput = $("question");
  const universeInput = $("universe");
  const reviewInput = $("review");
  const askButton = $("ask");
  const hint = $("hint");
  const progress = $("progress");
  const steps = $("steps");
  const result = $("result");
  const statusLine = $("status-line");
  const answerBox = $("answer");
  const citationsBox = $("citations");
  const reviewBox = $("review-box");
  const traceButton = $("trace-button");
  const traceView = $("trace-view");

  let currentRun = null;
  let stream = null;

  tokenInput.value = localStorage.getItem("fintrace_token") || "";
  tokenInput.addEventListener("change", () => {
    localStorage.setItem("fintrace_token", tokenInput.value.trim());
  });

  const esc = (value) =>
    String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));

  function reset() {
    steps.innerHTML = "";
    result.hidden = true;
    reviewBox.hidden = true;
    citationsBox.innerHTML = "";
    hint.textContent = "";
    traceView.hidden = true;
    traceView.innerHTML = "";
    traceButton.hidden = true;
    if (stream) { stream.close(); stream = null; }
  }

  function showError(message) {
    hint.textContent = message;
  }

  function renderAnswer(answer, usage) {
    result.hidden = false;
    const text = answer.text || "";
    answerBox.innerHTML = escWithMarkers(text);
    answerBox.querySelectorAll(".cite").forEach((node) => {
      node.onclick = () => {
        const target = citationsBox.querySelector(`[data-cite="${node.dataset.key}"]`);
        if (target) { target.open = true; target.scrollIntoView({ behavior: "smooth" }); }
      };
    });

    const citations = answer.citations || [];
    const facts = answer.facts || [];
    const parts = [];
    citations.forEach((c, i) => {
      const key = c.handle || `K${i + 1}`;
      parts.push(`<details data-cite="${esc(key)}">
        <summary>[${esc(key)}] ${esc(c.accession || "")} · Item ${esc(c.item_code || "")}</summary>
        <blockquote>${esc(c.quote || "")}</blockquote>
      </details>`);
    });
    facts.forEach((f) => {
      parts.push(`<details data-cite="${esc(f.handle)}">
        <summary>[${esc(f.handle)}] XBRL · ${esc(f.ticker)} · ${esc(f.tag)}</summary>
        <blockquote>${esc(f.value)} ${esc(f.unit)} · 截止 ${esc(f.period_end || "")}</blockquote>
      </details>`);
    });
    if (!parts.length) parts.push('<p class="muted">该回答没有引用。</p>');
    citationsBox.innerHTML = `<h4 style="margin:0 0 8px">出处</h4>${parts.join("")}`;

    const badges = [];
    if (answer.refused) badges.push('<span class="badge bad">拒答</span>');
    if (answer.verification_failed) badges.push('<span class="badge warn">未过校验发布</span>');
    if (!answer.refused && !answer.verification_failed) badges.push('<span class="badge ok">完成</span>');
    const cost = usage && usage.cost_usd != null ? `$${Number(usage.cost_usd).toFixed(5)}` : "—";
    const latency = usage && usage.latency_ms ? `${(usage.latency_ms / 1000).toFixed(1)}s` : "—";
    statusLine.innerHTML = `${badges.join(" ")} <span class="muted">成本 ${cost} · LLM 耗时 ${latency}</span>`;
    traceButton.hidden = false;
  }

  function escWithMarkers(raw) {
    const tokens = [];
    const escaped = esc(raw).replace(/\[([KF]\d+)\]/g, (m, key) => {
      tokens.push(key);
      return `\u0000${tokens.length - 1}\u0000`;
    });
    return escaped.replace(/\u0000(\d+)\u0000/g, (m, i) => {
      const key = tokens[Number(i)];
      return `<span class="cite" data-key="${esc(key)}">${esc(key)}</span>`;
    });
  }

  function renderReview(payload) {
    result.hidden = false;
    reviewBox.hidden = false;
    const draft = (payload && payload.draft && payload.draft.text) || "（无草稿）";
    answerBox.textContent = draft;
    statusLine.innerHTML = '<span class="badge warn">待人工复核</span>';
    citationsBox.innerHTML = "";
  }

  async function ask() {
    reset();
    const token = tokenInput.value.trim();
    if (!token) { showError("先填访问令牌。"); return; }
    const question = questionInput.value.trim();
    if (question.length < 3) { showError("问题太短。"); return; }

    askButton.disabled = true;
    progress.hidden = false;
    try {
      const response = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          question,
          universe: universeInput.value.split(",").map((s) => s.trim()).filter(Boolean),
          review: reviewInput.checked,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      currentRun = body.run_id;
      $("run-id").textContent = `#${body.run_id.slice(0, 8)}`;
      subscribe(body.run_id, body.ticket, token);
    } catch (error) {
      progress.hidden = true;
      showError(String(error.message || error));
      askButton.disabled = false;
    }
  }

  function subscribe(runId, ticket, token) {
    steps.innerHTML = "";
    stream = new EventSource(`/api/runs/${runId}/events?ticket=${encodeURIComponent(ticket)}`);
    stream.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "ping") return;
      if (data.type === "queued" || data.type === "running") {
        steps.innerHTML = `<li class="done">已排队，开始执行…</li>`;
      } else if (data.type === "node") {
        const li = document.createElement("li");
        li.className = "done";
        li.textContent = `节点 ${data.node}`;
        steps.appendChild(li);
      } else if (data.type === "awaiting_review") {
        stream.close();
        progress.hidden = true;
        renderReview(data.payload);
        askButton.disabled = false;
      } else if (data.type === "done") {
        stream.close();
        progress.hidden = true;
        renderAnswer(data.answer, data.usage);
        askButton.disabled = false;
      } else if (data.type === "failed") {
        stream.close();
        progress.hidden = true;
        showError(data.error || "运行失败");
        askButton.disabled = false;
      }
    };
    stream.onerror = () => {
      if (stream) { stream.close(); stream = null; }
      poll(runId, token);
    };
  }

  async function poll(runId, token) {
    try {
      const response = await fetch(`/api/runs/${runId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const body = await response.json();
      if (body.status === "awaiting_review") renderReview(body.review_payload);
      else if (body.status === "done") renderAnswer(body.answer, body.usage);
      else if (body.status === "failed") showError(body.error || "运行失败");
      else setTimeout(() => poll(runId, token), 1000);
    } catch (error) {
      showError(String(error));
    }
    askButton.disabled = false;
  }

  async function decide(action) {
    if (!currentRun) return;
    const token = tokenInput.value.trim();
    const body = { action };
    if (action === "edit") {
      const text = window.prompt("改写后的答案：", answerBox.textContent || "");
      if (!text) return;
      body.text = text;
    }
    const response = await fetch(`/api/runs/${currentRun}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) { showError(payload.detail || "复核失败"); return; }
    reviewBox.hidden = true;
    renderAnswer(payload.answer, payload.answer.usage);
  }

  async function loadTrace() {
    if (!currentRun) return;
    const token = tokenInput.value.trim();
    const response = await fetch(`/api/runs/${currentRun}/trace`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) { showError("trace 不可用：可能已被清理"); return; }
    const payload = await response.json();
    const steps = payload.steps || [];
    traceView.innerHTML = `<h4 style="margin:16px 0 8px">执行轨迹</h4>` +
      `<table><thead><tr><th>#</th><th>动作</th><th>工具耗时</th><th>观察（截断）</th></tr></thead><tbody>` +
      steps.map((step) => `<tr>
        <td>${esc(step.index)}</td>
        <td>${esc(step.action)}</td>
        <td>${esc(step.tool_latency_ms)}ms</td>
        <td class="muted">${esc(String(step.observation || "").slice(0, 160))}</td>
      </tr>`).join("") + `</tbody></table>`;
    traceView.hidden = false;
  }

  traceButton.onclick = loadTrace;
  askButton.onclick = ask;
  questionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) ask();
  });
  $("approve").onclick = () => decide("approve");
  $("edit").onclick = () => decide("edit");
  $("reject").onclick = () => decide("reject");
})();
