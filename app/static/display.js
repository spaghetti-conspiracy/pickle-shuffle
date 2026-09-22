/** 試合表示画面。タブレットに出しっぱなしにして使う。 */

import { api, currentSessionId, rememberSessionId } from "/api.js";

const $ = (id) => document.getElementById(id);
const POLL_INTERVAL_MS = 2000;
const ROTATIONS = [0, 90, 180, 270];

const sessionId = currentSessionId();
let lastRevision = null;
let busy = false;

function renderCourt(court) {
  const element = document.createElement("div");
  element.className = "court";

  const name = document.createElement("div");
  name.className = "court-name";
  name.textContent = court.name;
  element.append(name);

  const body = document.createElement("div");
  body.className = "court-body";

  if (court.match) {
    for (const [index, team] of [court.match.team_a, court.match.team_b].entries()) {
      if (index === 1) {
        const versus = document.createElement("div");
        versus.className = "versus";
        versus.textContent = "vs";
        body.append(versus);
      }
      const side = document.createElement("div");
      side.className = "team";
      for (const player of team) {
        const label = document.createElement("div");
        label.className = "player";
        label.textContent = player.nickname;
        side.append(label);
      }
      body.append(side);
    }
  } else {
    element.classList.add("empty");
    if (court.state === "practice") {
      element.classList.add("practice");
      body.textContent = "練習コート";
    } else {
      body.textContent = "人数が足りません";
    }
  }

  element.append(body);
  return element;
}

function render(data) {
  $("session-name").textContent = data.session.name;

  const courts = $("courts");
  courts.dataset.rotation = String(data.session.rotation);
  courts.innerHTML = "";
  for (const court of data.courts) courts.append(renderCourt(court));

  const waiting = data.waiting.map((p) => p.nickname);
  const resting = data.resting.map((p) => p.nickname);
  const parts = [];
  if (waiting.length) parts.push(`待機 ${waiting.join(" ")}`);
  if (resting.length) parts.push(`休憩 ${resting.join(" ")}`);
  $("waiting").textContent = parts.join("　/　");

  const warnings = [];
  if (data.duplicate_nicknames.length) {
    warnings.push(`同名 ${data.duplicate_nicknames.join(" ")}`);
  }
  if (data.stale_members.length) {
    warnings.push(`メンバー変更あり ${data.stale_members.join(" ")}`);
  }
  $("warnings").textContent = warnings.join("　");

  const pending = data.round_status === "pending";
  $("start").classList.toggle("hidden", !pending);
  $("next").textContent = pending ? "スキップ" : "次のマッチ";
}

async function poll() {
  if (busy) return;
  try {
    const data = await api.get(`/api/sessions/${sessionId}/current`);
    // ラウンドとコートの状態が変わったときだけ描き直す。
    // メンバーを編集しただけでは revision が変わらないので、試合中に画面は動かない。
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      render(data);
    }
  } catch (error) {
    $("warnings").textContent = `通信できません（${error.message}）`;
  } finally {
    // 最初の読み込みが終わったことを示す。操作の前にこれを待てばよい。
    document.body.dataset.ready = "1";
  }
}

/** 操作の結果は即座に描き直す。他端末が先に操作していたら取り直す。 */
async function act(run) {
  if (busy) return;
  busy = true;
  try {
    const data = await run();
    lastRevision = data.revision;
    render(data);
  } catch (error) {
    if (error.status === 409) {
      lastRevision = null; // 他端末が先に進めた。次のポーリングで追従する。
    } else {
      $("warnings").textContent = error.message;
    }
  } finally {
    busy = false;
  }
  await poll();
}

$("start").addEventListener("click", () =>
  act(async () => {
    const current = await api.get(`/api/sessions/${sessionId}/current`);
    if (current.round_status !== "pending") return current;
    return api.post(`/api/rounds/${current.round_id}/adopt`);
  }),
);

// スキップも「次のマッチ」も、やることは生成。
// 生成済みの pending があれば自動的に不採用になる（統計には影響しない）。
$("next").addEventListener("click", () =>
  act(() => api.post(`/api/sessions/${sessionId}/rounds/generate`)),
);

$("rotate").addEventListener("click", () =>
  act(async () => {
    const current = await api.get(`/api/sessions/${sessionId}/current`);
    const next = ROTATIONS[(ROTATIONS.indexOf(current.session.rotation) + 1) % ROTATIONS.length];
    await api.patch(`/api/sessions/${sessionId}`, { rotation: next });
    return api.get(`/api/sessions/${sessionId}/current`);
  }),
);

if (!sessionId) {
  $("warnings").textContent = "練習会が選ばれていません。管理画面から開いてください。";
} else {
  rememberSessionId(sessionId);
  poll();
  setInterval(poll, POLL_INTERVAL_MS);
}
