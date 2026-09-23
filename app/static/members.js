/** メンバー管理画面。
 *
 * 練習会とは関係のない名簿。ここで追加・修正・削除をする。
 *
 * - **ここでの削除は本当に消す。** ただし練習会の参加者と過去の記録には触らない。
 *   進行中の練習会からいきなり人が抜けると事故になるため
 * - **取り込みの導線は置かない。** 実際のイベントに紐づくときしか外部から
 *   引かない、という歯止めのため。取り込みは練習会の作成・管理画面から
 */

import {
  $,
  api,
  createPasswordGate,
  GENDER_LABELS,
  LEVEL_LABELS,
} from "/api.js";

function showError(message) {
  $("error").textContent = message ?? "";
}

/** 属性を変える選択肢。管理画面のレベル欄と同じ作りにしてある。 */
function attributeSelect(person, field, labels) {
  const select = document.createElement("select");
  for (const [value, label] of Object.entries(labels)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = person[field];
  select.addEventListener("change", async () => {
    try {
      await api.patch(`/api/people/${person.id}`, { [field]: select.value });
      showError("");
      await load();
    } catch (error) {
      showError(error.message);
      select.value = person[field];
    }
  });
  return select;
}

/** 呼び名を変える。押したときだけ送る（打っている途中で送らない）。 */
function nicknameCell(person) {
  const cell = document.createElement("td");
  const input = document.createElement("input");
  input.value = person.nickname;
  input.size = 12;
  input.setAttribute("aria-label", "ニックネーム");
  const commit = async () => {
    const nickname = input.value.trim();
    if (!nickname || nickname === person.nickname) {
      input.value = person.nickname;
      return;
    }
    try {
      await api.patch(`/api/people/${person.id}`, { nickname });
      showError("");
      await load();
    } catch (error) {
      showError(error.message);
      input.value = person.nickname;
    }
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") input.blur();
  });
  cell.append(input);
  if (person.duplicate) {
    // 手で登録したあとに同じ人を取り込んでしまった形。どちらを消すか選べるように。
    const badge = document.createElement("span");
    badge.className = "badge warn";
    badge.textContent = "同名あり";
    cell.append(" ", badge);
  }
  return cell;
}

function removeButton(person) {
  const button = document.createElement("button");
  button.textContent = "削除";
  button.className = "danger";
  button.addEventListener("click", async () => {
    const joined = person.sessions
      ? `\n参加中の練習会が ${person.sessions} 件あります。そちらの参加者としては残りますが、`
        + `その練習会で参加者を取り込み直すと、同じ人が別人として入り直します。`
      : "";
    if (!window.confirm(`「${person.nickname}」を名簿から削除します。${joined}`)) return;
    button.disabled = true;
    try {
      await api.del(`/api/people/${person.id}`);
      showError("");
      await load();
    } catch (error) {
      showError(error.message);
      button.disabled = false;
    }
  });
  return button;
}

async function load() {
  const people = await api.get("/api/people");
  const body = $("people-body");
  body.innerHTML = "";
  for (const person of people) {
    const row = document.createElement("tr");

    const gender = document.createElement("td");
    gender.append(attributeSelect(person, "gender", GENDER_LABELS));

    const level = document.createElement("td");
    level.append(attributeSelect(person, "level", LEVEL_LABELS));

    const source = document.createElement("td");
    source.textContent = person.source_label ?? "手入力";

    const sessions = document.createElement("td");
    sessions.textContent = person.sessions ? `${person.sessions}件` : "—";

    const actions = document.createElement("td");
    actions.append(removeButton(person));

    row.append(nicknameCell(person), gender, level, source, sessions, actions);
    body.append(row);
  }
  $("empty").classList.toggle("hidden", people.length > 0);
  document.body.dataset.ready = "1";
}

$("add-person").addEventListener("click", async () => {
  const nickname = $("new-nickname").value.trim();
  if (!nickname) {
    showError("ニックネームを入力してください");
    return;
  }
  try {
    await api.post("/api/people", {
      nickname,
      gender: $("new-gender").value,
      level: $("new-level").value,
    });
    $("new-nickname").value = "";
    showError("");
    await load();
  } catch (error) {
    showError(error.message);
  }
});

$("new-nickname").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("add-person").click();
});

const gate = createPasswordGate({ load });

gate.enter().catch((error) => {
  showError(`名簿を読めません（${error.message}）`);
  document.body.dataset.ready = "1";
});
