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
  const eventId = $("new-session-event").value.trim();
  let session;
  try {
    session = await api.post("/api/sessions", {
      name,
      court_count: Number($("new-session-courts").value),
    });
  } catch (error) {
    showError(error.message);
    return;
  }

  if (eventId) {
    // 取り込みだけ失敗しても練習会は残す。管理画面からやり直せる。
    showError("参加者を取り込んでいます…");
    try {
      await api.post(`/api/sessions/${session.token}/members/import`, {
        event_id: Number(eventId),
      });
    } catch (error) {
      showError(`練習会は作りました。取り込みに失敗: ${error.message}`);
      return;
    }
  }
  openSession(session.token);
});

loadSessions().catch((error) => {
  // 捕まえないと、空のプルダウンが並んだ一見正常な画面になる。
  // 既存の練習会があるのに新しく作られると、その日の記録が分断される。
  showError(`練習会の一覧を読めません（${error.message}）`);
  document.body.dataset.ready = "1";
});
