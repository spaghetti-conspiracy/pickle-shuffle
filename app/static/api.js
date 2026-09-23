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
    // FastAPI の入力検証は detail を配列で返す。そのまま Error に渡すと
    // 画面に「[object Object]」と出てしまう。
    const detail = Array.isArray(data?.detail)
      ? data.detail.map((d) => d.msg ?? String(d)).join("　")
      : data?.detail;
    const error = new Error(detail ?? `${response.status} ${response.statusText}`);
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

/** 合言葉が切れていたらトップ画面に戻す。
 *
 * 管理画面と全体表示画面は、トップ画面で合言葉を入れてから開くもの。
 * ここで入力欄を出すと、練習会ごとに合言葉を配ることになってしまう。
 * **メンバー用画面では使わない。** QR を読んだ人に合言葉は要らない。
 */
export function bounceToTop(error) {
  if (error?.status !== 401) return false;
  location.replace("/");
  return true;
}

/** 合言葉の入力欄。
 *
 * **いたずら防止であって、秘密を守る仕組みではない。** 練習会のトークンで
 * 開く画面（管理・全体表示・メンバー用）には掛からないので、QR を読んだ人に
 * 合言葉を配る必要はない。掛かるのは選択画面とメンバー管理画面だけ。
 *
 * `load` が 401 を投げたら入力欄を出し、通ったら本体を出す。
 */
export function createPasswordGate({ load }) {
  const gate = document.getElementById("gate");
  const main = document.getElementById("main");
  const error = document.getElementById("gate-error");

  function show(which) {
    gate.classList.toggle("hidden", which !== "gate");
    main.classList.toggle("hidden", which !== "main");
    // 合言葉を聞いている状態も「描き終わった」に含める。そうしないと、
    // 読み込み中と区別が付かない。
    document.body.dataset.ready = "1";
    if (which === "gate") document.getElementById("password").focus();
  }

  async function enter() {
    try {
      await load();
      show("main");
      return true;
    } catch (failure) {
      if (failure.status !== 401) throw failure;
      show("gate");
      return false;
    }
  }

  document.getElementById("unlock").addEventListener("click", async () => {
    error.textContent = "";
    try {
      await request("POST", "/api/login", {
        password: document.getElementById("password").value,
      });
    } catch (failure) {
      error.textContent = failure.message;
      return;
    }
    document.getElementById("password").value = "";
    await enter();
  });

  document.getElementById("password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") document.getElementById("unlock").click();
  });

  return { enter };
}

/** 短く書くためだけのもの。4画面すべてで使う。 */
export const $ = (id) => document.getElementById(id);

// 取り込みでは性別が分からないことがある。「その他」だと選んだように見えるので
// 「未設定」にする。値そのもの（other）と生成側の扱いは変えない。
export const GENDER_LABELS = { male: "男性", female: "女性", other: "未設定" };
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

/** 覚えている練習会を忘れる。終了したときに呼ぶ。
 *
 * 残したままだと、URL を付けずに全体画面やメンバー画面を開いたときに
 * 消えた練習会を掴んで「終了しました」と出てしまう。
 */
export function forgetSessionToken() {
  try {
    localStorage.removeItem("pickle.session");
  } catch {
    // 消せなくても、次に選んだ時点で上書きされる。
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
export function startPolling(run, interval) {
  // `interval` は数値でも、そのつど決める関数でもよい。
  // メンバー用画面は「変わりそうなときだけ速く」するために関数を渡す。
  const nextDelay = () => (typeof interval === "function" ? interval() : interval);
  let timer = null;

  const tick = () => {
    run();
    if (timer !== null) timer = setTimeout(tick, nextDelay());
  };
  const resume = () => {
    if (timer === null) {
      timer = setTimeout(tick, nextDelay());
    }
  };
  const pause = () => {
    if (timer !== null) {
      clearTimeout(timer);
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
  // 休憩に回った人・外れた人は暗くする。読み上げる前に気づけるように。
  if (player.unavailable) label.dataset.unavailable = "1";
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

/** 残り時間・経過時間の表示。mm:ss。 */
export function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

/** サーバから受け取った時計を、手元で進める。
 *
 * ポーリングは2〜5秒に1回なので、そのままだと表示が飛び飛びになる。
 * 受け取った時点を基準に、経過を自分で足して滑らかに見せる。
 * 次の同期で少し巻き戻ることがあるが、そろっていればよい。
 *
 * 絶対時刻ではなく performance.now() を使う。端末とサーバの時計が
 * ずれていても影響を受けない。
 */
export function createClock() {
  let timer = null;
  let receivedAt = 0;
  return {
    sync(next) {
      timer = next;
      receivedAt = performance.now();
    },
    /** いまの経過秒。動いていなければ受け取った値のまま。 */
    elapsed() {
      if (timer === null) return 0;
      if (timer.state !== "running") return timer.elapsed_seconds;
      return timer.elapsed_seconds + (performance.now() - receivedAt) / 1000;
    },
    /** 残り秒。無制限なら null。 */
    remaining() {
      if (timer === null || timer.limit_seconds === null) return null;
      return timer.limit_seconds - this.elapsed();
    },
    isTimedOut() {
      const left = this.remaining();
      return left !== null && left <= 0;
    },
    /** 止める指示が来ているか。 */
    isSilenced() {
      return timer !== null && Boolean(timer.alarm_silenced);
    },
    /** いま鳴らすべきか。止めた指示が来ていれば鳴らさない。 */
    shouldRing() {
      return (
        timer !== null &&
        timer.state === "running" &&
        this.isTimedOut() &&
        !timer.alarm_silenced
      );
    },
    state() {
      return timer === null ? "stopped" : timer.state;
    },
    hasLimit() {
      return timer !== null && timer.limit_seconds !== null;
    },
  };
}

/** キッチンタイマーのような音。音声ファイルを置かずに合成する。
 *
 * ブラウザは操作なしに音を鳴らさないので、最初の操作で下ごしらえをする。
 */
export function createAlarm() {
  let context = null;
  let stopAt = 0;
  let timer = null;

  const unlock = () => {
    if (context === null) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (Ctor) context = new Ctor();
    }
    if (context && context.state === "suspended") context.resume();
  };

  const beep = () => {
    if (!context) return;
    const now = context.currentTime;
    // 2回の短い電子音を1組にする。キッチンタイマーらしい鳴り方。
    for (const offset of [0, 0.18]) {
      const osc = context.createOscillator();
      const gain = context.createGain();
      osc.type = "square";
      osc.frequency.value = 2000;
      gain.gain.setValueAtTime(0.0001, now + offset);
      gain.gain.exponentialRampToValueAtTime(0.25, now + offset + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.12);
      osc.connect(gain).connect(context.destination);
      osc.start(now + offset);
      osc.stop(now + offset + 0.14);
    }
  };

  return {
    unlock,
    /** 鳴らし始める。既定で30秒たったら自分で止まる。 */
    start(seconds = 30) {
      unlock();
      if (timer !== null) return;
      stopAt = performance.now() + seconds * 1000;
      beep();
      timer = setInterval(() => {
        if (performance.now() >= stopAt) {
          this.stop();
          return;
        }
        beep();
      }, 600);
    },
    stop() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    },
    get ringing() {
      return timer !== null;
    },
  };
}
