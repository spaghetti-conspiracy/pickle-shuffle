/** 全体表示画面。
 *
 * 練習会のリーダーが読み上げるための画面。全コートの現在のマッチを一覧し、
 * 開始とスキップをここで決める。決めた結果はメンバー用画面へ自動で伝わる
 * （再スケジュールはリーダーの意図、その拡散は自動）。
 */

import { api, currentSessionToken, rememberSessionToken, startPolling } from "/api.js";

const $ = (id) => document.getElementById(id);
const POLL_INTERVAL_MS = 2000;
// リーダーが操作する画面。押した結果は応答で即座に反映されるので、
// このポーリングは他端末の操作に追従するためのもの。

const sessionToken = currentSessionToken();
let lastRevision = null;
let busy = false;
let poller = null;

/** コートに試合が入っていないときの説明。状態ごとに理由が違う。 */
const EMPTY_COURT_MESSAGE = {
  waiting: "「マッチを作る」を押すと組み合わせが出ます",
  next_round: "次のマッチから使います",
  idle: "人数が足りません",
  practice: "練習コート",
};

/** 名前を1行に収める。長い名前は文字数に応じて縮める。 */
function playerLabel(nickname) {
  const label = document.createElement("div");
  label.className = "player";
  label.textContent = nickname;
  // 全角1文字をほぼ1em とみなし、収まる大きさを CSS 側で逆算させる。
  label.style.setProperty("--len", String(Math.max(nickname.length, 3)));
  return label;
}

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
        side.append(playerLabel(player.nickname));
      }
      body.append(side);
    }
  } else {
    element.classList.add("empty");
    if (court.state === "practice") element.classList.add("practice");
    body.textContent = EMPTY_COURT_MESSAGE[court.state] ?? "";
  }

  element.append(body);
  return element;
}

/** QR と同じ URL を文字でも出す。読み上げにも使うし、届かないときに気づける。 */
function renderMemberUrl(url) {
  $("member-url").textContent = url;
  const unreachable = /\/\/(localhost|127\.0\.0\.1|\[::1\])/.test(url);
  $("qr-warning").classList.toggle("hidden", !unreachable);
}


function render(data) {
  document.title = `${data.session.name} — 全体表示`;
  $("session-name").textContent = data.session.name;
  $("admin-link").href = `/manage.html?session=${sessionToken}`;

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
    warnings.push(`変更あり（次のマッチから反映） ${data.stale_members.join(" ")}`);
  }
  $("warnings").textContent = warnings.join("　");

  // まだ1度も作っていないのに「次のマッチ」と出ると、何を押せばよいか分からない。
  const pending = data.round_status === "pending";
  $("start").classList.toggle("hidden", !pending);
  if (pending) {
    $("next").textContent = "スキップ";
  } else {
    $("next").textContent = data.round_id === null ? "マッチを作る" : "次のマッチ";
  }

  renderMemberUrl(data.member_url);
}

async function poll() {
  if (busy) return;
  try {
    const data = await api.get(`/api/sessions/${sessionToken}/current`);
    // 描き直すべきときだけ描き直す。revision にはマッチの中身を左右する値が
    // 入っていないので、メンバーを編集しても試合中の組み合わせは動かない。
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      render(data);
    }
  } catch (error) {
    if (error.status === 404) {
      showGone();
    } else {
      // 一時的な通信の失敗。次のポーリングで復帰する見込みなので画面は残す。
      $("warnings").textContent = `通信できません（${error.message}）`;
    }
  } finally {
    document.body.dataset.ready = "1";
  }
}

/** 練習会が消えている。古いマッチや QR を残すと、まだ有効に見えてしまう。 */
function showGone() {
  poller?.stop();
  $("overview").classList.add("hidden");
  $("action-bar").classList.add("hidden");
  $("gone").classList.remove("hidden");
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
    const current = await api.get(`/api/sessions/${sessionToken}/current`);
    if (current.round_status !== "pending") return current;
    return api.post(`/api/rounds/${current.round_id}/adopt`);
  }),
);

// スキップも「次のマッチ」も、やることは生成。
// 生成済みの pending があれば自動的に不採用になる（統計には影響しない）。
$("next").addEventListener("click", () =>
  act(() => api.post(`/api/sessions/${sessionToken}/rounds/generate`)),
);

if (!sessionToken) {
  $("warnings").textContent = "練習会が選ばれていません。メンバー登録画面から開いてください。";
  document.body.dataset.ready = "1";
} else {
  rememberSessionToken(sessionToken);
  // 読み込めたときだけ出す。練習会が消えているとリンク切れのアイコンが出てしまう。
  const qr = $("qr");
  qr.addEventListener("load", () => qr.classList.remove("hidden"));
  qr.addEventListener("error", () => qr.classList.add("hidden"));
  qr.src = `/api/sessions/${sessionToken}/member-qr.svg`;
  poller = startPolling(poll, POLL_INTERVAL_MS);
}
