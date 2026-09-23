/** 練習会の管理画面。
 *
 * どの練習会を扱うかは作成・選択画面（/）で決まっているので、ここでは
 * その練習会のコートとメンバーだけを扱う。
 */

import {
  $,
  api,
  bounceToTop,
  currentSessionToken,
  forgetSessionToken,
  GENDER_LABELS,
  LEVEL_LABELS,
  rememberSessionToken,
} from "/api.js";

const sessionToken = currentSessionToken();
/** メンバー名簿。参加者はここから選んで足す。 */
let people = [];
/** いまの練習会に入っている人の id。名簿の選択肢から外すために持っておく。 */
let registered = new Set();

function showError(message) {
  $("member-error").textContent = message ?? "";
}

/** 持ち時間の選択肢。1分刻みで3〜15分と、無制限。 */
function fillTimerOptions() {
  const select = $("timer-minutes");
  for (let minutes = 3; minutes <= 15; minutes += 1) {
    const option = document.createElement("option");
    option.value = String(minutes);
    option.textContent = `${minutes}分`;
    select.append(option);
  }
}

async function loadSession() {
  const session = await api.get(`/api/sessions/${sessionToken}`);
  document.title = `${session.name} — 練習会の管理 — Pickle Shuffle`;
  $("session-name").textContent = session.name;
  $("highlight-beginners").checked = session.highlight_beginners;
  // 一度取り込んだら、その練習会はそのイベントに紐づく。打ち間違えると
  // 居ない人が一斉に休憩へ回るので、変えられないようにしておく。
  const event = $("import-event");
  if (session.tennisbear_event_id !== null) {
    event.value = String(session.tennisbear_event_id);
    event.readOnly = true;
    $("import-note").textContent =
      "この練習会はこのイベントから取り込んでいます。押すと最新の参加者を取り込みます。" +
      "取り込み元は変えられません。間違えたときは練習会を作り直してください。";
  } else {
    event.readOnly = false;
    $("import-note").textContent = "";
  }
  $("timer-minutes").value =
    session.timer_minutes === null ? "unlimited" : String(session.timer_minutes);
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
      const name = nameInput.value;
      try {
        await api.patch(`/api/sessions/${sessionToken}/courts/${court.id}`, { name });
        court.name = name;
        showError("");
      } catch (error) {
        // 黙って失敗すると、入力欄に新しい名前が残るので通ったように見える。
        nameInput.value = court.name;
        showError(error.message);
      }
    });
    nameCell.append(nameInput);

    const stateCell = document.createElement("td");
    stateCell.textContent = court.in_use ? "試合用" : "練習コート";

    const useCell = document.createElement("td");
    const toggle = document.createElement("button");
    // 現在の状態ではなく、押したら起きることを書く。
    // 同じ画面のメンバー行が「休憩/復帰」＝操作を書く規約なので、揃える。
    toggle.textContent = court.in_use ? "練習コートにする" : "試合に戻す";
    toggle.classList.toggle("primary", !court.in_use);
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

    row.append(nameCell, stateCell, useCell);
    body.append(row);
  }
}

async function loadPeople() {
  people = await api.get("/api/people");
  renderPeopleOptions();
}

/** 名簿の選択肢を描き直す。すでに参加している人は出さない（二重登録になるだけ）。
 *
 * メンバー一覧と名簿はどちらが先に読み終わるか決まらないので、
 * 両方の更新からここを呼んで、そのときに分かっている情報で組み立てる。 */
function renderPeopleOptions() {
  const select = $("pick-person");
  const chosen = select.value;
  select.innerHTML = "";
  const available = people.filter((person) => !registered.has(person.id));
  for (const person of available) {
    const option = document.createElement("option");
    option.value = String(person.id);
    option.textContent = person.nickname;
    select.append(option);
  }
  if (available.some((person) => String(person.id) === chosen)) select.value = chosen;
  // 選べる人がいなければ押せないようにする（全員参加済みか、名簿が読めない）。
  $("pick-person").disabled = available.length === 0;
  $("add-known").disabled = available.length === 0;
  $("pick-note").textContent =
    available.length === 0 ? "名簿の全員がこの練習会に入っています。" : "";
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
    button.disabled = true;
    try {
      await api.patch(`/api/members/${member.id}`, {
        status: member.status === "resting" ? "active" : "resting",
      });
      showError("");
      await loadMembers();
    } catch (error) {
      showError(error.message);
      button.disabled = false;
    }
  });
  return button;
}

async function loadMembers() {
  const members = await api.get(`/api/sessions/${sessionToken}/members`);
  registered = new Set(members.map((m) => m.person_id).filter((id) => id !== null));
  renderPeopleOptions();
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
      remove.textContent = "外す";
      remove.className = "danger";
      remove.addEventListener("click", async () => {
        // 消えるのはこの練習会の参加者リストからだけ。名簿には残る。
        if (!window.confirm(`「${member.nickname}」をこの練習会から外します。名簿には残ります。`)) {
          return;
        }
        remove.disabled = true;
        try {
          await api.del(`/api/members/${member.id}`);
          showError("");
          await loadMembers();
        } catch (error) {
          showError(error.message);
          remove.disabled = false;
        }
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
  await Promise.all([loadCourts(), loadMembers(), loadPeople()]);
  document.body.dataset.ready = "1";
}

/** 持ち時間の変更。試合の最中でも、その場で残り時間に反映される
 * （締切ではなく経過を持っているため）。 */
$("timer-minutes").addEventListener("change", async (event) => {
  const value = event.target.value;
  const body =
    value === "unlimited" ? { unlimited: true } : { timer_minutes: Number(value) };
  try {
    await api.patch(`/api/sessions/${sessionToken}`, body);
    showError("");
  } catch (error) {
    showError(error.message);
    await loadSession();
  }
});

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

/** 参加者の取り込み。何度でも押してよい。
 *
 * すでにいる人は tennisbear の ID で見分けるので増えない。直したレベルも戻らない。
 * 直前に増えた人を足すために、練習会が始まってからも使う。
 */
$("import-members").addEventListener("click", async () => {
  const button = $("import-members");
  const eventId = $("import-event").value.trim();
  const result = $("import-result");
  if (!eventId) {
    showError("イベントID を入れてください");
    return;
  }
  button.disabled = true;
  result.textContent = "取り込んでいます…";
  try {
    const summary = await api.post(
      `/api/sessions/${sessionToken}/members/import`,
      { event_id: Number(eventId) },
    );
    const parts = [`${summary.added.length}人を追加`];
    if (summary.unchanged) parts.push(`${summary.unchanged}人は登録済み`);
    if (summary.resting.length) {
      parts.push(`休憩にした ${summary.resting.join("、")}`);
    }
    result.textContent = parts.join("　/　");
    showError("");
    await Promise.all([loadSession(), loadMembers(), loadPeople()]);
  } catch (error) {
    result.textContent = "";
    showError(error.message);
    // 別の端末が先に取り込み元を決めていることがある。今の状態を出し直す。
    await loadSession().catch(() => {});
  } finally {
    button.disabled = false;
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
    // 覚えたままだと、次に URL 無しで開いたとき消えた練習会を掴む。
    forgetSessionToken();
    location.href = "/";
  } catch (error) {
    showError(error.message);
  }
});

$("add-known").addEventListener("click", async () => {
  const personId = Number($("pick-person").value);
  if (!personId) return;
  try {
    await api.post(`/api/sessions/${sessionToken}/members`, { person_id: personId });
    showError("");
    await Promise.all([loadMembers(), loadPeople()]);
  } catch (error) {
    showError(error.message);
  }
});

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
    await Promise.all([loadMembers(), loadPeople()]);
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
  fillTimerOptions();
  refresh().catch((error) => {
    if (error.status === 404) {
      // 破棄された練習会の URL を開いた場合。選び直してもらう。
      location.replace("/");
      return;
    }
    // 合言葉が無い／切れた。トップ画面で入れてから開き直してもらう。
    if (bounceToTop(error)) return;
    // 通信の瞬断で追い出すと、location.replace なので戻ることもできない。
    showError(`読み込めません（${error.message}）。通信を確認して開き直してください。`);
    document.body.dataset.ready = "1";
  });
}
