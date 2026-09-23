/** メンバー用画面。
 *
 * 各自がスマートフォンで自分のコートを見るための画面。読むだけで、
 * 開始やスキップは置かない（決めるのはリーダー）。
 * 全体表示画面でリーダーが次のマッチに進めると、ここも自動で追従する。
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

// 読むだけの画面なので、全体表示画面より緩くてよい。
// 人数ぶんの端末が同時に叩くため、間隔を詰めすぎると通信量が効いてくる。
const POLL_INTERVAL_MS = 5000;

const sessionToken = currentSessionToken();
const storageKey = `pickle.court.${sessionToken}`;
let selectedCourtId = null;
let lastRevision = null;
let poller = null;
let lastData = null;
const gate = createGate();
const clock = createClock();
//: サーバのプロセスが変わったら知らせる。応答が途切れたり、作り直した直後で
//: 記録が消えていたりするのを、黙っていると「時計が狂った」と見えてしまう。
let serverInstance = null;
const alarm = createAlarm();
const CLOCK_INTERVAL_MS = 250;
let confirming = false;
let alarmDone = false;
//: この端末で鳴らすかどうか。ほかの人の端末には影響しない。
const SOUND_KEY = "pickle.sound";
//: この端末で止めたラウンド。全体画面と違い、止めても他の端末は鳴り続ける。
let silencedRound = null;

function soundEnabled() {
  try {
    return localStorage.getItem(SOUND_KEY) !== "off";
  } catch {
    return true; // 保存できない端末でも、既定どおり鳴らす
  }
}

function setSoundEnabled(on) {
  try {
    localStorage.setItem(SOUND_KEY, on ? "on" : "off");
  } catch {
    // 覚えられなくても、その場では効く
  }
}

/** この端末で鳴らすべきか。 */
function shouldRingHere() {
  return (
    clock.shouldRing() &&
    soundEnabled() &&
    silencedRound !== (lastData && lastData.round_id)
  );
}

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

function renderTabs(courts) {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  for (const court of courts) {
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.type = "button";
    // プロパティ代入（tab.role / tab.ariaSelected）は新しめの端末しか反映しない。
    // 古い iPhone では属性が付かず、選択中のタブに色が出なくなる。
    tab.setAttribute("role", "tab");
    tab.textContent = court.name;
    const selected = court.id === selectedCourtId;
    tab.setAttribute("aria-selected", String(selected));
    tab.addEventListener("click", () => {
      rememberCourt(court.id);
      alarm.unlock(); // 音を出す許可は、利用者の操作の中でしか取れない
      // 手元のデータで描き直す。通信の往復を待たせない。
      // 体育館の WiFi は人数ぶんの端末がぶら下がって遅くなるので、
      // 待たせるとタップが効かない画面になる。表示に必要な情報は
      // どのコートぶんも同じ応答に入っている。
      if (lastData) render(lastData);
    });
    if (selected) {
      // 前回の続きで開くと、選択中のタブが画面外にいることがある。
      requestAnimationFrame(() =>
        tab.scrollIntoView({ inline: "nearest", block: "nearest" }),
      );
    }
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
  lastData = data;
  // **この画面では初心者の色分けをしない。** 緑や「初」のバッジを出すと、
  // 内部にレベルの情報があることが全員に分かってしまう。
  // アルゴリズムの確認に使う表示なので、リーダーの全体表示画面だけでよい。
  const highlightBeginners = false;
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

/** 残り時間。手元で数えるので、同期の間も動く。
 *
 * 同期のたびに少し飛んだり巻き戻ったりするが、全体画面と合っていればよい。
 * 手元で時間切れになったら、その場でサーバに確かめに行き、
 * 向こうでも切れていたら鳴らす。手元の時計だけで鳴らすと、
 * 一時停止されていたのに鳴る、といったことが起きる。
 */
function renderClock() {
  const element = $("clock");
  const state = clock.state();
  $("clock-title").textContent = clock.hasLimit() ? "残り時間" : "経過時間";
  // 鳴っている間だけ出す。どの経路を通っても判断がぶれないよう先に決める。
  $("alarm-off").classList.toggle("hidden", !alarm.ringing);
  $("clock-box").classList.toggle(
    "hidden",
    state === "stopped" && clock.elapsed() <= 0,
  );
  if (state === "stopped") {
    // 中断されたあとも「試合終了」と出す。開始前とは区別する。
    if (clock.elapsed() > 0) {
      element.textContent = "試合終了";
      element.dataset.state = "over";
    } else {
      element.textContent = "";
      element.removeAttribute("data-state");
    }
    return;
  }
  if (!clock.hasLimit()) {
    // 見出しに「経過時間」と出ているので、数字だけでよい。
    element.textContent = formatClock(clock.elapsed());
    element.dataset.state = state === "paused" ? "paused" : "running";
    return;
  }
  const left = clock.remaining();
  element.textContent = left <= 0 ? "試合終了" : formatClock(left);
  element.dataset.state = left <= 0 ? "over" : state === "paused" ? "paused" : "running";

  if (left <= 0 && state === "running" && !alarmDone && !confirming && soundEnabled()) {
    confirmTimeout();
  }
  // 鳴っている間は、止められていないかを細かく確かめる。
  // ふだんの5秒間隔だと、リーダーが止めてから最大5秒鳴り続ける。
  if (alarm.ringing && !confirming) confirmTimeout();
  if (left > 0) alarmDone = false;
}

/** 手元で切れたので、サーバに確かめてから鳴らす。 */
async function confirmTimeout() {
  confirming = true;
  try {
    const data = await api.get(`/api/sessions/${sessionToken}/current`);
    if (serverInstance !== null && data.server_instance !== serverInstance) {
      setNotice("サーバが再起動しました。表示を確認してください", true);
    }
    serverInstance = data.server_instance;
    clock.sync(data.timer);
    lastData = data;
    if (shouldRingHere() && !alarmDone) {
      alarmDone = true;
      alarm.start();
    }
    if (!shouldRingHere()) alarm.stop();
  } catch {
    // つながらなければ次のポーリングでやり直す。鳴らさない。
  } finally {
    confirming = false;
  }
}

function setNotice(message) {
  const element = $("offline");
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

async function refresh() {
  const token = gate.token();
  try {
    const data = await api.get(`/api/sessions/${sessionToken}/current`);
    if (gate.isStale(token)) return;
    setNotice("");
    lastData = data;
    clock.sync(data.timer);
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
      // #status を潰すと、今が試合中かどうかも分からなくなる。
      setNotice(`通信できません（${error.message}）`);
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
  setInterval(renderClock, CLOCK_INTERVAL_MS);

  $("sound-on").checked = soundEnabled();
  $("sound-on").addEventListener("change", (event) => {
    setSoundEnabled(event.target.checked);
    if (!event.target.checked) alarm.stop();
    else alarm.unlock();
    renderClock();
  });

  // この端末だけ止める。ほかの人の端末は鳴ったままにしておく。
  $("alarm-off").addEventListener("click", () => {
    silencedRound = lastData ? lastData.round_id : null;
    alarm.stop();
    renderClock();
  });
  // 最初の操作で音の下ごしらえをする（ブラウザは操作なしに鳴らさない）。
  document.addEventListener("pointerdown", () => alarm.unlock(), { once: true });
}
