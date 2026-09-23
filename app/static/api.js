/** API 呼び出しの共通部分。 */

export async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = new Error(data?.detail ?? `${response.status} ${response.statusText}`);
    error.status = response.status;
    // 同じ 409 でも「他端末が先に操作した」と「人数が足りない」は扱いが違う。
    error.code = data?.code ?? "";
    throw error;
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
  patch: (path, body) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),
};

/** 短く書くためだけのもの。4画面すべてで使う。 */
export const $ = (id) => document.getElementById(id);

export const GENDER_LABELS = { male: "男性", female: "女性", other: "その他" };
// 管理画面の追加フォーム（manage.html）と同じ文言にしておく。
// 片方だけ直すと、同じレベルが画面内で別の名前に見えてしまう。
export const LEVEL_LABELS = {
  pickleball: "ピックルボール経験者",
  racket_experienced: "ラケット経験者（ルール習得中）",
  beginner: "未経験者",
};

/** 練習会のトークンを URL から読む。無ければ最後に見たものを使う。
 *
 * 連番だと他の練習会を推測できてしまうので、URL に出す識別子はトークンにしてある。
 */
export function currentSessionToken() {
  const fromUrl = new URLSearchParams(location.search).get("session");
  if (fromUrl) return fromUrl;
  try {
    return localStorage.getItem("pickle.session");
  } catch {
    return null;
  }
}

export function rememberSessionToken(token) {
  try {
    localStorage.setItem("pickle.session", token);
  } catch {
    // プライベートウィンドウなどで保存できなくても動作には影響しない。
  }
}

/** 画面が見えている間だけ、定期的に ``run`` を呼ぶ。
 *
 * メンバーがスマートフォンをポケットに入れている間もポーリングを続けると、
 * 人数ぶんの通信が延々と積み上がる。見えていない間は止め、画面に戻した瞬間に
 * 1回走らせて最新にする。
 *
 * 返り値の ``stop()`` で完全に止められる（練習会が消えたときなど）。
 */
export function startPolling(run, intervalMs) {
  let timer = null;

  const resume = () => {
    if (timer === null) timer = setInterval(run, intervalMs);
  };
  const pause = () => {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
  const onVisibilityChange = () => {
    if (document.hidden) {
      pause();
    } else {
      run(); // 戻ってきた時点の状態をすぐ見せる
      resume();
    }
  };

  document.addEventListener("visibilitychange", onVisibilityChange);
  run();
  if (!document.hidden) resume();

  return {
    stop() {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      pause();
    },
  };
}


/** 名前の色分け。男女の区分が一目で分かるようにする。
 *
 * 初心者を緑にするかどうかは練習会の設定で切り替える。緑はアルゴリズムの
 * 確認用で、ふだんは男女の区分だけで色を付ける。
 */
export function toneOf(player, highlightBeginners) {
  if (highlightBeginners && player.level === "beginner") return "beginner";
  return player.gender;
}

/** 名前を1行に収める。長い名前は文字数に応じて縮める。
 *
 * 全角は1文字ぶん、半角は半文字ぶんとして数える。UTF-16 の length だと
 * 半角ばかりの名前が必要以上に縮み、絵文字は逆に過大評価される。
 */
export function playerLabel(player, highlightBeginners) {
  const label = document.createElement("div");
  label.className = "player";
  label.textContent = player.nickname;
  label.dataset.tone = toneOf(player, highlightBeginners);
  const width = [...player.nickname].reduce(
    (total, ch) => total + (/[\x20-\x7e\uff61-\uff9f]/.test(ch) ? 0.5 : 1),
    0,
  );
  label.style.setProperty("--len", String(Math.max(width, 3)));
  return label;
}

/** 操作とポーリングの競合を防ぐ世代番号。
 *
 * `await` の前に busy を見るだけでは足りない。操作より先に飛んでいた GET が
 * 操作の描画より後に返ると、古い内容で上書きしてしまう。リーダーが読み上げ
 * 始めた直後に前のマッチへ巻き戻る、という形で表に出る（不変則12）。
 */
export function createGate() {
  let generation = 0;
  return {
    bump: () => {
      generation += 1;
    },
    token: () => generation,
    isStale: (token) => token !== generation,
  };
}
