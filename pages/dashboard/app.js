const bridge = window.AstrBotPluginPage;
const result = document.getElementById("result");
const targetList = document.getElementById("target-list");
const targetInput = document.getElementById("target-input");
const statusSummary = document.getElementById("status-summary");

async function renderStatus() {
  try {
    const data = await bridge.apiGet("status");
    const targets = Array.from(
      new Set([...(data.configured_targets || []), ...(data.bound_targets || [])]),
    );
    statusSummary.textContent = `cron ${data.cron_registered ? "已注册" : "未注册"} · ${data.daily_cron || "-"} · 上次推送 ${data.last_push_at || "尚未推送"}`;
    targetList.innerHTML = "";
    if (!targets.length) {
      targetList.innerHTML = '<li class="empty">暂无推送订阅</li>';
    }
    for (const umo of targets) {
      const configured = (data.configured_targets || []).includes(umo);
      const bound = (data.bound_targets || []).includes(umo);
      const item = document.createElement("li");
      item.className = "target-item";
      item.innerHTML = `<span class="target-umo">${esc(umo)}</span>` +
        `<span class="target-source">${configured ? "配置" : ""}${bound ? "绑定" : ""}</span>` +
        (bound ? `<button type="button" data-umo="${esc(umo)}" class="remove-button">移除</button>` : "");
      targetList.appendChild(item);
    }
  } catch (error) {
    statusSummary.textContent = `状态读取失败：${error}`;
  }
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

async function runAction(button, api, label) {
  button.disabled = true;
  result.textContent = `${label}执行中...`;
  try {
    const data = await bridge.apiPost(api);
    result.textContent = `${data.message || "完成"}`;
  } catch (error) {
    result.textContent = `${label}失败：${error}`;
  } finally {
    button.disabled = false;
    await renderStatus();
  }
}

document.getElementById("btn-update").addEventListener("click", () => {
  runAction(document.getElementById("btn-update"), "update", "拉取更新");
});

document.getElementById("btn-push").addEventListener("click", () => {
  runAction(document.getElementById("btn-push"), "push", "推送");
});

document.getElementById("btn-add-target").addEventListener("click", async () => {
  const umo = targetInput.value.trim();
  if (!umo) {
    result.textContent = "请先填写 umo。";
    return;
  }
  const data = await bridge.apiPost("targets", { umo, action: "add" });
  result.textContent = data.message || "已添加";
  targetInput.value = "";
  await renderStatus();
});

targetList.addEventListener("click", async (event) => {
  const button = event.target.closest(".remove-button");
  if (!button) return;
  const data = await bridge.apiPost("targets", {
    umo: button.dataset.umo,
    action: "remove",
  });
  result.textContent = data.message || "已移除";
  await renderStatus();
});

await bridge.ready();
await renderStatus();
