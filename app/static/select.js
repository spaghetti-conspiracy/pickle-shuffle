/** 練習会の作成・選択画面。
 *
 * 練習会を決めるのは開始時の1度きりで、その後の管理とは使うタイミングが違うので
 * 画面を分けてある。ここで選んだら管理画面へ移る。
 */

import { api, rememberSessionToken } from "/api.js";

const $ = (id) => document.getElementById(id);

function showError(message) {
  $("error").textContent = message ?? "";
}

function openSession(token) {
  rememberSessionToken(token);
  location.href = `/manage.html?session=${token}`;
}

async function loadSessions() {
  const sessions = await api.get("/api/sessions");
  const select = $("session-select");
  select.innerHTML = "";
  for (const session of sessions) {
    const option = document.createElement("option");
    option.value = session.token;
    option.textContent = session.name;
    select.append(option);
  }
  // 1つも無いときは、選ぶところを出さずに作成だけ見せる。
  $("pick").classList.toggle("hidden", sessions.length === 0);
  document.body.dataset.ready = "1";
}

$("open-session").addEventListener("click", () => {
  const token = $("session-select").value;
  if (token) openSession(token);
});

$("create-session").addEventListener("click", async () => {
  const name = $("new-session-name").value.trim();
  if (!name) {
    showError("練習会の名前を入力してください");
    return;
  }
  try {
    const session = await api.post("/api/sessions", {
      name,
      court_count: Number($("new-session-courts").value),
    });
    openSession(session.token);
  } catch (error) {
    showError(error.message);
  }
});

loadSessions();
