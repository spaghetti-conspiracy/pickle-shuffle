/** 管理画面。練習会・コート・メンバーを操作する。 */

import {
  api,
  currentSessionId,
  GENDER_LABELS,
  LEVEL_LABELS,
  rememberSessionId,
} from "/api.js";

const $ = (id) => document.getElementById(id);

let sessionId = currentSessionId();
let profiles = [];

function showError(message) {
  $("member-error").textContent = message ?? "";
}

async function loadSessions() {
  const sessions = await api.get("/api/sessions");
  const select = $("session-select");
  select.innerHTML = "";
  for (const session of sessions) {
    const option = document.createElement("option");
    option.value = String(session.id);
    option.textContent = session.name;
    select.append(option);
  }
  if (sessions.length === 0) {
    sessionId = null;
  } else if (!sessions.some((s) => s.id === sessionId)) {
    sessionId = sessions[0].id;
  }
  if (sessionId) {
    select.value = String(sessionId);
    rememberSessionId(sessionId);
  }
  const hasSession = sessionId !== null;
  $("courts-section").classList.toggle("hidden", !hasSession);
  $("members-section").classList.toggle("hidden", !hasSession);
}

async function loadCourts() {
  if (!sessionId) return;
  const session = await api.get(`/api/sessions/${sessionId}`);
  const body = $("courts-body");
  body.innerHTML = "";
  for (const court of session.courts) {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    const nameInput = document.createElement("input");
    nameInput.value = court.name;
    nameInput.size = 12;
    nameInput.addEventListener("change", async () => {
      await api.patch(`/api/sessions/${sessionId}/courts/${court.id}`, {
        name: nameInput.value,
      });
    });
    nameCell.append(nameInput);

    const useCell = document.createElement("td");
    const toggle = document.createElement("button");
    toggle.textContent = court.in_use ? "試合で使う" : "練習コート";
    toggle.classList.toggle("primary", court.in_use);
    toggle.addEventListener("click", async () => {
      try {
        await api.patch(`/api/sessions/${sessionId}/courts/${court.id}`, {
          in_use: !court.in_use,
        });
        showError("");
      } catch (error) {
        showError(error.message);
      }
      await loadCourts();
    });
    useCell.append(toggle);

    row.append(nameCell, useCell);
    body.append(row);
  }
}

async function loadProfiles() {
  profiles = await api.get("/api/member-profiles");
  const list = $("known-nicknames");
  list.innerHTML = "";
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.nickname;
    list.append(option);
  }
}

/** 既に登録のある名前を入力したら、性別とレベルを埋める。 */
function autofillFromProfile() {
  const nickname = $("new-nickname").value.trim();
  const profile = profiles.find((p) => p.nickname === nickname);
  if (!profile) return;
  $("new-gender").value = profile.gender;
  $("new-level").value = profile.level;
}

function statusButton(member) {
  const button = document.createElement("button");
  if (member.status === "resting") {
    button.textContent = "復帰";
    button.classList.add("primary");
  } else {
    button.textContent = "休憩";
  }
  button.addEventListener("click", async () => {
    await api.patch(`/api/members/${member.id}`, {
      status: member.status === "resting" ? "active" : "resting",
    });
    await loadMembers();
  });
  return button;
}

async function loadMembers() {
  if (!sessionId) return;
  const members = await api.get(`/api/sessions/${sessionId}/members`);
  const counts = {};
  for (const member of members) counts[member.nickname] = (counts[member.nickname] ?? 0) + 1;

  const body = $("members-body");
  body.innerHTML = "";
  for (const member of members) {
    const row = document.createElement("tr");
    row.className = member.status;

    const name = document.createElement("td");
    name.textContent = member.nickname;
    if (counts[member.nickname] > 1 && member.status !== "left") {
      const badge = document.createElement("span");
      badge.className = "badge warn";
      badge.textContent = "同名あり";
      name.append(" ", badge);
    }

    const gender = document.createElement("td");
    gender.textContent = GENDER_LABELS[member.gender] ?? member.gender;

    const level = document.createElement("td");
    level.textContent = LEVEL_LABELS[member.level] ?? member.level;

    const plays = document.createElement("td");
    plays.textContent = String(member.plays);

    const actions = document.createElement("td");
    if (member.status !== "left") {
      const remove = document.createElement("button");
      remove.textContent = "削除";
      remove.className = "danger";
      remove.addEventListener("click", async () => {
        await api.del(`/api/members/${member.id}`);
        await loadMembers();
      });
      actions.append(statusButton(member), " ", remove);
    } else {
      actions.textContent = "離脱";
    }

    row.append(name, gender, level, plays, actions);
    body.append(row);
  }

  const duplicates = Object.entries(counts)
    .filter(([, count]) => count > 1)
    .map(([nickname]) => nickname);
  const notice = $("duplicate-notice");
  notice.classList.toggle("hidden", duplicates.length === 0);
  notice.textContent =
    duplicates.length === 0
      ? ""
      : `同じニックネームの人がいます（${duplicates.join(", ")}）。` +
        "試合表示でどちらか分からなくなるので、名前を変えることをおすすめします。";
}

async function refresh() {
  await loadSessions();
  await Promise.all([loadCourts(), loadMembers(), loadProfiles()]);
  // 読み込みが終わったことを示す。操作の前にこれを待てばよい。
  document.body.dataset.ready = "1";
}

$("session-select").addEventListener("change", async (event) => {
  sessionId = Number(event.target.value);
  rememberSessionId(sessionId);
  await refresh();
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
    $("new-session-name").value = "";
    sessionId = session.id;
    rememberSessionId(sessionId);
    showError("");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
});

$("delete-session").addEventListener("click", async () => {
  if (!sessionId) return;
  const name = $("session-select").selectedOptions[0]?.textContent ?? "";
  if (!confirm(`「${name}」の記録を破棄します。よろしいですか？`)) return;
  await api.del(`/api/sessions/${sessionId}`);
  sessionId = null;
  await refresh();
});

$("open-display").addEventListener("click", () => {
  if (!sessionId) {
    showError("先に練習会を作成または選択してください");
    return;
  }
  window.open(`/overview.html?session=${sessionId}`, "_blank");
});

$("new-nickname").addEventListener("change", autofillFromProfile);
$("new-nickname").addEventListener("blur", autofillFromProfile);

$("add-member").addEventListener("click", async () => {
  if (!sessionId) {
    showError("先に練習会を作成または選択してください");
    return;
  }
  const input = $("new-nickname");
  const nickname = input.value.trim();
  if (!nickname) {
    showError("ニックネームを入力してください");
    return;
  }
  // 入力欄は送信前に空にする。通信の完了を待ってから消すと、
  // その間に次の名前を打ち込んでいた場合に消してしまう。
  input.value = "";
  input.focus();
  try {
    await api.post(`/api/sessions/${sessionId}/members`, {
      nickname,
      gender: $("new-gender").value,
      level: $("new-level").value,
    });
    showError("");
    await Promise.all([loadMembers(), loadProfiles()]);
  } catch (error) {
    showError(`${nickname}: ${error.message}`);
    if (!input.value) input.value = nickname;
  }
});

refresh();
