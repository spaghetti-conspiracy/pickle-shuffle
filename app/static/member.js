/** メンバー用画面。
 *
 * 各自がスマートフォンで自分のコートを見るための画面。読むだけで、
 * 開始やスキップは置かない（決めるのはリーダー）。
 * 全体表示画面でリーダーが次のマッチに進めると、ここも自動で追従する。
 */

import { api, currentSessionId, rememberSessionId } from "/api.js";

const $ = (id) => document.getElementById(id);
const POLL_INTERVAL_MS = 2000;

const sessionId = currentSessionId();
const storageKey = `pickle.court.${sessionId}`;
let selectedCourtId = null;
let lastRevision = null;

function rememberCourt(courtId) {
  selectedCourtId = courtId;
  try {
    localStorage.setItem(storageKey, String(courtId));
  } catch {
    // プライベートウィンドウなどで保存できなくても、その回は選べるので支障はない。
  }
}

function restoreCourt() {
  try {
    const saved = localStorage.getItem(storageKey);
    return saved ? Number(saved) : null;
  } catch {
    return null;
  }
}

function renderTabs(courts) {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  for (const court of courts) {
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.type = "button";
    tab.role = "tab";
    tab.textContent = court.name;
    tab.ariaSelected = String(court.id === selectedCourtId);
    tab.addEventListener("click", () => {
      rememberCourt(court.id);
      // 押した結果はすぐ見せる。次のポーリングを待たせない。
      lastRevision = null;
      refresh();
    });
    tabs.append(tab);
  }
}

function renderCourt(court) {
  const container = $("court");
  container.innerHTML = "";

  if (!court) {
    container.innerHTML = '<p class="member-empty">コートがありません</p>';
    return;
  }

  if (!court.match) {
    const message = document.createElement("p");
    message.className = "member-empty";
    message.textContent =
      court.state === "practice" ? "練習コートです" : "まだマッチが決まっていません";
    container.append(message);
    return;
  }

  for (const [index, team] of [court.match.team_a, court.match.team_b].entries()) {
    if (index === 1) {
      const versus = document.createElement("div");
      versus.className = "member-versus";
      versus.textContent = "vs";
      container.append(versus);
    }
    const side = document.createElement("div");
    side.className = "member-team";
    for (const player of team) {
      const label = document.createElement("div");
      label.className = "player";
      label.textContent = player.nickname;
      side.append(label);
    }
    container.append(side);
  }
}

function render(data) {
  document.title = `${data.session.name} — コート表示`;
  $("session-name").textContent = data.session.name;
  $("status").textContent = data.round_status === "adopted" ? "試合中" : "次のマッチ";

  const courts = data.courts;
  if (!courts.some((c) => c.id === selectedCourtId)) {
    selectedCourtId = courts.length ? courts[0].id : null;
  }
  renderTabs(courts);
  renderCourt(courts.find((c) => c.id === selectedCourtId));

  const waiting = data.waiting.map((p) => p.nickname);
  const resting = data.resting.map((p) => p.nickname);
  const parts = [];
  if (waiting.length) parts.push(`待機 ${waiting.join(" ")}`);
  if (resting.length) parts.push(`休憩 ${resting.join(" ")}`);
  $("waiting").textContent = parts.join("　/　");
}

async function refresh() {
  try {
    const data = await api.get(`/api/sessions/${sessionId}/current`);
    // リーダーがマッチを進めたときだけ描き直す。
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      render(data);
    }
  } catch (error) {
    $("status").textContent = `通信できません（${error.message}）`;
  } finally {
    document.body.dataset.ready = "1";
  }
}

if (!sessionId) {
  $("court").innerHTML = '<p class="member-empty">練習会が指定されていません</p>';
  document.body.dataset.ready = "1";
} else {
  rememberSessionId(sessionId);
  selectedCourtId = restoreCourt();
  refresh();
  setInterval(refresh, POLL_INTERVAL_MS);
}
