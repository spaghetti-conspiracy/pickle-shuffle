/** 全体表示画面。
 *
 * 練習会のリーダーが読み上げるための画面。全コートの現在のマッチを一覧し、
 * 開始とスキップをここで決める。決めた結果はメンバー用画面へ自動で伝わる
 * （再スケジュールはリーダーの意図、その拡散は自動）。
 */

import {
  $,
  api,
  createAlarm,
  createClock,
  createGate,
  currentSessionToken,
  formatClock,
  playerLabel,
  rememberSessionToken,
  startPolling,
} from "/api.js";

const POLL_INTERVAL_MS = 2000;
// リーダーが操作する画面。押した結果は応答で即座に反映されるので、
// このポーリングは他端末の操作に追従するためのもの。

const sessionToken = currentSessionToken();
let lastRevision = null;
let busy = false;
let poller = null;
const gate = createGate();
const clock = createClock();
//: サーバのプロセスが変わったら知らせる。応答が途切れたり、作り直した直後で
//: 記録が消えていたりするのを、黙っていると「時計が狂った」と見えてしまう。
let serverInstance = null;
const alarm = createAlarm();
//: 時計は毎秒描き直す。ポーリング（2秒）より細かく動かすため。
const CLOCK_INTERVAL_MS = 250;
let alarmDone = false;
//: いま時計を見ているラウンド。変わったらアラームを仕切り直す。
let clockRoundId = null;

/** コートに試合が入っていないときの説明。状態ごとに理由が違う。 */
const EMPTY_COURT_MESSAGE = {
  waiting: "「マッチを作る」を押すと組み合わせが出ます",
  next_round: "次のマッチから使います",
  idle: "人数が足りません",
  practice: "練習コート",
};

function renderCourt(court, highlightBeginners) {
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
        side.append(playerLabel(player, highlightBeginners));
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

/** 色の意味を書いておく。
 *
 * 色だけで伝えると、初見のリーダーには意味が分からないし、
 * 色覚特性のある人には女性と初心者の区別が付かない。
 */
function renderLegend(highlightBeginners) {
  const items = [
    ["male", "男性"],
    ["female", "女性"],
    ["other", "未設定"],
  ];
  // 緑表示が off のときは「初心者」という項目自体を出さない。
  // 出すと、その区分が内部にあることが読み上げの場で伝わってしまう。
  if (highlightBeginners) items.push(["beginner", "初心者"]);
  const legend = $("legend");
  legend.innerHTML = "";
  for (const [tone, label] of items) {
    const item = document.createElement("span");
    item.className = tone;
    item.textContent = label;
    legend.append(item);
  }
}

/** QR と同じ URL を文字でも出す。読み上げにも使うし、届かないときに気づける。 */
function renderMemberUrl(url) {
  $("member-url").textContent = url;
  const unreachable = /\/\/(localhost|127\.0\.0\.1|\[::1\])/.test(url);
  $("qr-warning").classList.toggle("hidden", !unreachable);
}


function render(data) {
  const highlightBeginners = data.session.highlight_beginners;
  document.title = `${data.session.name} — 全体表示`;
  $("session-name").textContent = data.session.name;
  $("admin-link").href = `/manage.html?session=${sessionToken}`;

  const courts = $("courts");
  courts.innerHTML = "";
  for (const court of data.courts) courts.append(renderCourt(court, highlightBeginners));

  $("waiting").textContent = data.waiting.map((p) => p.nickname).join("　") || "—";
  $("resting").textContent = data.resting.map((p) => p.nickname).join("　") || "—";

  const warnings = [];
  if (data.duplicate_nicknames.length) {
    warnings.push(`同名 ${data.duplicate_nicknames.join(" ")}`);
  }
  if (data.stale_members.length) {
    // 何が変わったかは書かない。レベルを直したことが周りに伝わってしまう。
    warnings.push(
      `登録情報が更新されました（次のマッチから反映） ${data.stale_members.join(" ")}`,
    );
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

  renderLegend(highlightBeginners);
  renderMemberUrl(data.member_url);
  // 「次のマッチ」を出すかは時計で決まる。描き直しのたびに戻さない。
  renderClock();
}

/** 画面の下部に出す短い知らせ。
 *
 * 通信の失敗と、操作が断られた理由（人数不足など）を同じ枠で出す。
 * ただし消えるタイミングが違う。通信の失敗は繋がれば消してよいが、
 * 操作の理由は「押しても何も起きなかった」の答えなので、次に操作が
 * 通るまで残す。ポーリングが成功したくらいで消してはいけない。
 */
let noticeIsFromAction = false;

/** 残り時間と、時計まわりのボタン。
 *
 * ここは revision の外で毎回更新する。経過秒を revision に入れると
 * 2秒ごとに全体が描き直され、読み上げの最中にちらつくため。
 */
function renderClock() {
  const element = $("clock");
  const state = clock.state();
  const running = state === "running" || state === "paused";
  // 中断したあとも「試合終了」と出したいので、開始前かどうかで分ける。
  const everStarted = state !== "stopped" || clock.elapsed() > 0;

  const write = (main, note = "") => {
    element.textContent = main;
    if (note) {
      const small = document.createElement("span");
      small.className = "clock-note";
      small.textContent = note;
      element.append(small);
    }
  };

  // 見出しは、持ち時間があるかどうかで変える。
  $("clock-title").textContent = clock.hasLimit() ? "残り時間" : "経過時間";
  $("clock-box").classList.toggle("hidden", !running && !everStarted);

  if (!running) {
    if (everStarted) {
      write("試合終了", "「次のマッチ」で次に進みます");
      element.dataset.state = "over";
    } else {
      write("");
      element.removeAttribute("data-state");
    }
  } else if (!clock.hasLimit()) {
    write(formatClock(clock.elapsed()), state === "paused" ? "一時停止中" : "");
    element.dataset.state = state === "paused" ? "paused" : "running";
  } else {
    const left = clock.remaining();
    if (left <= 0) {
      write("試合終了", `時間です（${formatClock(-left)} 超過）`);
    } else {
      write(formatClock(left), state === "paused" ? "一時停止中" : "");
    }
    element.dataset.state =
      left <= 0 ? "over" : state === "paused" ? "paused" : "running";
  }

  const timedOut = clock.isTimedOut();
  // 試合中は次へ進ませない。中断するか時間切れになるまで押せないようにする。
  // 押せると、読み上げている途中で組み合わせが変わってしまう。
  $("next").classList.toggle("hidden", running && !timedOut);
  $("pause").classList.toggle("hidden", !running || timedOut);
  $("pause").textContent = state === "paused" ? "再開" : "一時停止";
  // 時間切れのあとは「アラームオフ」と「次のマッチ」だけにする。
  $("stop-timer").classList.toggle("hidden", !running || timedOut);
  $("alarm-off").classList.toggle("hidden", !alarm.ringing);

  // 鳴らすのは1回だけ。止めたあとに鳴り直さない。
  if (clock.shouldRing() && !alarmDone) {
    alarmDone = true;
    alarm.start();
  }
  // ほかの端末で止められたら、こちらも止める。
  if (!clock.shouldRing()) alarm.stop();
  if (!timedOut) alarmDone = false;
}

function setNotice(message, fromAction = false) {
  noticeIsFromAction = Boolean(message) && fromAction;
  const element = $("offline");
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

async function poll() {
  if (busy) return;
  const token = gate.token();
  try {
    const data = await api.get(`/api/sessions/${sessionToken}/current`);
    // 待っている間に利用者が操作していたら、この応答はもう古い。
    // 描くと、押した直後に前のマッチへ巻き戻って見える。
    if (gate.isStale(token)) return;
    if (!noticeIsFromAction) setNotice("");
    // 時計は revision に関係なく、毎回合わせ直す。
    if (serverInstance !== null && data.server_instance !== serverInstance) {
      setNotice("サーバが再起動しました。表示を確認してください", true);
    }
    serverInstance = data.server_instance;
    if (data.round_id !== clockRoundId) {
      // 次の試合に移ったら、前の試合の鳴動を持ち越さない。
      clockRoundId = data.round_id;
      alarm.stop();
      alarmDone = false;
    }
    clock.sync(data.timer);
    // 描き直すべきときだけ描き直す。revision は表示すべき中身の指紋。
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      render(data);
    }
  } catch (error) {
    if (error.status === 404) {
      showGone();
    } else {
      // 一時的な通信の失敗。次のポーリングで復帰する見込みなので画面は残す。
      setNotice(`通信できません（${error.message}）`);
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
  gate.bump(); // 先に飛んでいたポーリングの応答を捨てる
  try {
    const data = await run();
    gate.bump();
    if (data.round_id !== clockRoundId) {
      // 次の試合に移ったら、前の試合の鳴動を持ち越さない。
      clockRoundId = data.round_id;
      alarm.stop();
      alarmDone = false;
    }
    clock.sync(data.timer);
    setNotice("");
    lastRevision = data.revision;
    render(data);
  } catch (error) {
    gate.bump();
    if (error.code === "conflict") {
      lastRevision = null; // 他端末が先に進めた。次のポーリングで追従する。
    } else {
      // 人数不足（409）もここに来る。黙って捨てると、押しても何も起きない
      // 画面になる。練習会の開始直後はまだ4人揃っていないのが普通なので、
      // いちばん必要な場面でいちばん必要な説明が消えてしまう。
      setNotice(error.message, true);
    }
  } finally {
    busy = false;
  }
  await poll();
}

$("pause").addEventListener("click", () =>
  act(async () => {
    const action = clock.state() === "paused" ? "resume" : "pause";
    const current = await api.get(`/api/sessions/${sessionToken}/current`);
    return api.post(`/api/rounds/${current.round_id}/timer/${action}`);
  }),
);

$("stop-timer").addEventListener("click", () =>
  act(async () => {
    alarm.stop();
    const current = await api.get(`/api/sessions/${sessionToken}/current`);
    return api.post(`/api/rounds/${current.round_id}/timer/stop`);
  }),
);

$("alarm-off").addEventListener("click", () =>
  act(async () => {
    alarm.stop();
    const current = await api.get(`/api/sessions/${sessionToken}/current`);
    return api.post(`/api/rounds/${current.round_id}/timer/silence`);
  }),
);

$("start").addEventListener("click", () =>
  act(async () => {
    // 音を出す許可は、利用者の操作の中でしか取れない。
    alarm.unlock();
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
  setInterval(renderClock, CLOCK_INTERVAL_MS);
}
