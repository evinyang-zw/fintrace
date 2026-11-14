(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function card(label, value, hint) {
    return `<div class="metric"><span class="muted">${esc(label)}</span>
      <b>${esc(value)}</b><span class="muted">${esc(hint || "")}</span></div>`;
  }

  async function load() {
    const days = Math.max(1, Math.min(90, Number($("days").value) || 7));
    const response = await fetch(`/api/stats?days=${days}`);
    const data = await response.json();
    $("cards").innerHTML = [
      card("请求", data.total, `完成 ${data.done} / 失败 ${data.failed}`),
      card("成功率", `${(data.success_rate * 100).toFixed(1)}%`, "done / total"),
      card("单题成本", `$${data.cost_per_question.toFixed(5)}`, "LLM + 检索"),
      card("窗口总成本", `$${data.total_cost.toFixed(4)}`, `近 ${days} 天`),
      card("p50", `${(data.p50_ms / 1000).toFixed(1)}s`, "端到端"),
      card("p95", `${(data.p95_ms / 1000).toFixed(1)}s`, "端到端"),
      card("缓存命中", `${(data.cache_hit_rate * 100).toFixed(1)}%`, "输入 token"),
      card("待审", data.pending_review, "awaiting_review"),
    ].join("");
    $("daily").innerHTML = (data.daily || []).map((row) => `<tr>
      <td>${esc(row.day)}</td><td>${row.total}</td><td>${row.done}</td>
      <td>${row.failed}</td><td>$${row.cost.toFixed(4)}</td>
      <td>${(row.p95_ms / 1000).toFixed(1)}s</td></tr>`).join("")
      || '<tr><td colspan="6" class="muted">窗口内还没有运行记录。</td></tr>';
    $("tiers").innerHTML = (data.by_tier || []).map((row) => `<tr>
      <td>${esc(row.tier)}</td><td>${row.n}</td>
      <td>$${row.avg_cost.toFixed(5)}</td><td>${(row.p95_ms / 1000).toFixed(1)}s</td></tr>`).join("")
      || '<tr><td colspan="4" class="muted">窗口内没有带检索的运行。</td></tr>';
    $("generated").textContent = `生成于 ${data.generated_at}`;
  }

  $("refresh").onclick = load;
  load();
  setInterval(load, 30000);
})();
