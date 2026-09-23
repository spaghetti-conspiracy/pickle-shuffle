/** メンバー用画面。
 *
 * 各自がスマートフォンで自分のコートを見るための画面。読むだけで、
 * 開始やスキップは置かない（決めるのはリーダー）。
 * 全体表示画面でリーダーが次のマッチに進めると、ここも自動で追従する。
 */

import { api, currentSessionToken, rememberSessionToken, startPolling } from "/api.js";

const $ = (id) => document.getElementById(id);
// 読むだけの画面なので、全体表示画面より緩くてよい。
// 人数ぶんの端末が同時に叩くため、間隔を詰めすぎると通信量が効いてくる。
const POLL_INTERVAL_MS = 5000;

const sessionToken = currentSessionToken();
const storageKey = `pickle.court.${sessionToken}`;
let selectedCourtId = null;
let lastRevision = null;
let poller = null;

/** コートに試合が入っていないときの説明。状態ごとに理由が違う。 */
const EMPTY_COURT_MESSAGE = {
  waiting: "まだマッチが決まっていません",
  next_round: "次のマッチから使います",
  idle: "このコートは今回お休みです",
  practice: "練習コートです",
};

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

/** 名前の色分け。男女の区分が一目で分かるようにする。
 *
 * 初心者を緑にするかどうかは練習会の設定で切り替える。緑はアルゴリズムの
 * 確認用で、ふだんは男女の区分だけで色を付ける。
 */
function toneOf(player, highlightBeginners) {
  if (highlightBeginners && player.level === "beginner") return "beginner";
  return player.gender;
}

/** 名前を1行に収める。長い名前は文字数に応じて縮める。 */
function playerLabel(player, highlightBeginners) {
  const label = document.createElement("div");
  label.className = "player";
  label.textContent = player.nickname;
  label.dataset.tone = toneOf(player, highlightBeginners);
  // 全角1文字をほぼ1em とみなし、収まる大きさを CSS 側で逆算させる。
  label.style.setProperty("--len", String(Math.max(player.nickname.length, 3)));
  return label;
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

function renderCourt(court, highlightBeginners) {
  const container = $("court");
  container.innerHTML = "";

  if (!court) {
    container.innerHTML = '<p class="member-empty">コートがありません</p>';
    return;
  }

  if (!court.match) {
    const message = document.createElement("p");
    message.className = "member-empty";
    message.textContent = EMPTY_COURT_MESSAGE[court.state] ?? "";
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
      side.append(playerLabel(player, highlightBeginners));
    }
    container.append(side);
  }
}

function render(data) {
  const highlightBeginners = data.session.highlight_beginners;
  document.title = `${data.session.name} — コート表示`;
  $("session-name").textContent = data.session.name;
  $("status").textContent = data.round_status === "adopted" ? "試合中" : "次のマッチ";

  const courts = data.courts;
  if (!courts.some((c) => c.id === selectedCourtId)) {
    selectedCourtId = courts.length ? courts[0].id : null;
  }
  renderTabs(courts);
  renderCourt(courts.find((c) => c.id === selectedCourtId), highlightBeginners);

  const waiting = data.waiting.map((p) => p.nickname);
  const resting = data.resting.map((p) => p.nickname);
  const parts = [];
  if (waiting.length) parts.push(`待機 ${waiting.join(" ")}`);
  if (resting.length) parts.push(`休憩 ${resting.join(" ")}`);
  $("waiting").textContent = parts.join("　/　");
}

async function refresh() {
  try {
    const data = await api.get(`/api/sessions/${sessionToken}/current`);
    // リーダーがマッチを進めたときだけ描き直す。
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      render(data);
    }
  } catch (error) {
    if (error.status === 404) {
      showGone();
    } else {
      // 一時的な通信の失敗。次のポーリングで復帰する見込みなので画面は残す。
      $("status").textContent = `通信できません（${error.message}）`;
    }
  } finally {
    document.body.dataset.ready = "1";
  }
}

/** 練習会が消えている。古いマッチを残すと、まだ有効に見えてしまう。 */
function showGone() {
  poller?.stop();
  $("member").classList.add("hidden");
  $("gone").classList.remove("hidden");
}

if (!sessionToken) {
  $("court").innerHTML = '<p class="member-empty">練習会が指定されていません</p>';
  document.body.dataset.ready = "1";
} else {
  rememberSessionToken(sessionToken);
  selectedCourtId = restoreCourt();
  poller = startPolling(refresh, POLL_INTERVAL_MS);
}
