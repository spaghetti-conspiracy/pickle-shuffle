/** 練習会の管理画面。
 *
 * どの練習会を扱うかは作成・選択画面（/）で決まっているので、ここでは
 * その練習会のコートとメンバーだけを扱う。
 */

import {
  api,
  currentSessionToken,
  GENDER_LABELS,
  LEVEL_LABELS,
  rememberSessionToken,
} from "/api.js";

const $ = (id) => document.getElementById(id);

const sessionToken = currentSessionToken();
let profiles = [];
/** いまの練習会に登録済みのニックネーム。入力候補から外すために持っておく。 */
let registered = new Set();

function showError(message) {
  $("member-error").textContent = message ?? "";
}

async function loadSession() {
  const session = await api.get(`/api/sessions/${sessionToken}`);
  document.title = `${session.name} — 練習会の管理`;
  $("session-name").textContent = session.name;
  $("highlight-beginners").checked = session.highlight_beginners;
  return session;
}

async function loadCourts() {
  const session = await api.get(`/api/sessions/${sessionToken}`);
  const body = $("courts-body");
  body.innerHTML = "";
  for (const court of session.courts) {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    const nameInput = document.createElement("input");
    nameInput.value = court.name;
    nameInput.size = 12;
    nameInput.addEventListener("change", async () => {
      await api.patch(`/api/sessions/${sessionToken}/courts/${court.id}`, {
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
        await api.patch(`/api/sessions/${sessionToken}/courts/${court.id}`, {
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
  renderProfileOptions();
}

/** 入力候補を描き直す。すでに登録済みの人は出さない（選んでも二重登録になるだけ）。
 *
 * メンバー一覧と候補はどちらが先に読み終わるか決まらないので、
 * 両方の更新からここを呼んで、そのときに分かっている情報で組み立てる。 */
function renderProfileOptions() {
  const list = $("known-nicknames");
  list.innerHTML = "";
  for (const profile of profiles) {
    if (registered.has(profile.nickname)) continue;
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

/** レベルを変える。練習会の最中に上げ下げする運用が前提にある。 */
function levelSelect(member) {
  const select = document.createElement("select");
  for (const [value, label] of Object.entries(LEVEL_LABELS)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = member.level;
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      await api.patch(`/api/members/${member.id}`, { level: select.value });
      await loadMembers();
    } catch (error) {
      select.value = member.level; // 失敗したら見た目を元に戻す
      $("member-error").textContent = error.message;
      select.disabled = false;
    }
  });
  return select;
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
  const members = await api.get(`/api/sessions/${sessionToken}/members`);
  registered = new Set(members.map((m) => m.nickname));
  renderProfileOptions();
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
    if (member.status === "left") {
      level.textContent = LEVEL_LABELS[member.level] ?? member.level;
    } else {
      // ラケット経験者が慣れたら経験者に上げる運用があるので、ここで変えられる。
      // 変更は次の生成から効き、表示中のマッチは動かない。
      level.append(levelSelect(member));
    }

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
  await loadSession();
  await Promise.all([loadCourts(), loadMembers(), loadProfiles()]);
  document.body.dataset.ready = "1";
}

$("highlight-beginners").addEventListener("change", async (event) => {
  try {
    await api.patch(`/api/sessions/${sessionToken}`, {
      highlight_beginners: event.target.checked,
    });
    showError("");
  } catch (error) {
    showError(error.message);
    event.target.checked = !event.target.checked;
  }
});

$("open-overview").addEventListener("click", () => {
  window.open(`/overview.html?session=${sessionToken}`, "_blank");
});

$("finish-session").addEventListener("click", async () => {
  const name = $("session-name").textContent;
  if (!confirm(`「${name}」を終了します。記録は破棄されます。よろしいですか？`)) return;
  try {
    await api.del(`/api/sessions/${sessionToken}`);
    location.href = "/";
  } catch (error) {
    showError(error.message);
  }
});

$("new-nickname").addEventListener("change", autofillFromProfile);
$("new-nickname").addEventListener("blur", autofillFromProfile);

$("add-member").addEventListener("click", async () => {
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
    await api.post(`/api/sessions/${sessionToken}/members`, {
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

if (!sessionToken) {
  // 練習会が決まっていなければ、選ぶところへ戻す。
  location.replace("/");
} else {
  rememberSessionToken(sessionToken);
  refresh().catch(() => {
    // 破棄された練習会の URL を開いた場合など。選び直してもらう。
    location.replace("/");
  });
}
