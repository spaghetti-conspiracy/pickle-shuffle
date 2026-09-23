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

export const GENDER_LABELS = { male: "男性", female: "女性", other: "その他" };
export const LEVEL_LABELS = {
  pickleball: "ピックルボール経験者",
  racket_experienced: "ラケット経験者",
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
