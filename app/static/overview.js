/** 全体表示画面。
 *
 * 練習会のリーダーが読み上げるための画面。全コートの現在のマッチを一覧し、
 * 開始とスキップをここで決める。決めた結果はメンバー用画面へ自動で伝わる
 * （再スケジュールはリーダーの意図、その拡散は自動）。
 */

import { api, currentSessionId, rememberSessionId } from "/api.js";

const $ = (id) => document.getElementById(id);
const POLL_INTERVAL_MS = 2000;

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
  document.title = `${data.session.name} — 全体表示`;
  $("session-name").textContent = data.session.name;
  $("admin-link").href = `/?session=${sessionId}`;

  const courts = $("courts");
  courts.innerHTML = "";
  for (const court of data.courts) courts.append(renderCourt(court));

  $("waiting").textContent = data.waiting.map((p) => p.nickname).join("　") || "—";
  $("resting").textContent = data.resting.map((p) => p.nickname).join("　") || "—";

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

if (!sessionId) {
  $("warnings").textContent = "練習会が選ばれていません。メンバー登録画面から開いてください。";
  document.body.dataset.ready = "1";
} else {
  rememberSessionId(sessionId);
  $("qr").src = `/api/sessions/${sessionId}/member-qr.svg`;
  poll();
  setInterval(poll, POLL_INTERVAL_MS);
}
