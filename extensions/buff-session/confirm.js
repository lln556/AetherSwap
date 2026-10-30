const confirmationId = new URLSearchParams(location.search).get("id") || "";
const originNode = document.querySelector("#target-origin");
const statusNode = document.querySelector("#status");
const confirmButton = document.querySelector("#confirm");
const cancelButton = document.querySelector("#cancel");

function setStatus(message) {
  statusNode.textContent = typeof message === "string" ? message : "";
}

function setBusy(busy) {
  confirmButton.disabled = busy;
  cancelButton.disabled = busy;
}

async function loadConfirmation() {
  try {
    const result = await chrome.runtime.sendMessage({
      type: "GET_CONFIRMATION",
      confirmationId,
    });
    if (!result?.ok) {
      setStatus(result?.error || "确认已失效，请重新导入");
      cancelButton.disabled = false;
      return;
    }
    originNode.textContent = result.origin;
    confirmButton.disabled = false;
  } catch {
    setStatus("无法读取导入确认，请关闭窗口后重试");
    cancelButton.disabled = false;
  }
}

confirmButton.addEventListener("click", async () => {
  setBusy(true);
  setStatus("正在读取并验证 BUFF 会话…");
  try {
    const result = await chrome.runtime.sendMessage({
      type: "CONFIRM_IMPORT",
      confirmationId,
    });
    if (!result?.ok) {
      setStatus(result?.error || "导入未能完成，请重试");
      setBusy(false);
    }
  } catch {
    setStatus("导入未能完成，请关闭窗口后重试");
    setBusy(false);
  }
});

cancelButton.addEventListener("click", async () => {
  setBusy(true);
  try {
    await chrome.runtime.sendMessage({type: "CANCEL_CONFIRMATION", confirmationId});
  } finally {
    window.close();
  }
});

void loadConfirmation();
