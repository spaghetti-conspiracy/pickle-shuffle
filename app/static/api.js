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

/** 練習会 id を URL から読む。無ければ最後に見たものを使う。 */
export function currentSessionId() {
  const fromUrl = new URLSearchParams(location.search).get("session");
  if (fromUrl) return Number(fromUrl);
  const saved = localStorage.getItem("pickle.session");
  return saved ? Number(saved) : null;
}

export function rememberSessionId(id) {
  try {
    localStorage.setItem("pickle.session", String(id));
  } catch {
    // プライベートウィンドウなどで保存できなくても動作には影響しない。
  }
}
