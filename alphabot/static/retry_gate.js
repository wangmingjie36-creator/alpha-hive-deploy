// Yahoo 限流后「自动重试一次」的状态机（v0.45.675）。**无 DOM、不 import 任何页面模块**——tests/test_yf_rate_limit_handling.py 用 node 直接跑它。
//
// 规矩（都来自一次真实的审查：此前的实现是个只加不减的 Set，会在新一轮限流里说「已自动重试过一次」而其实没重试）：
//   · 一轮限流（episode）= 从第一次看到限流到第一次成功（或换成别的失败原因）。**每一轮最多自动重试一次**：连着撞只会多挨几条 429；
//   · 每次新的渲染开始（用户手动点了刷新 / 切了子标签）先 `begin`：取消还没触发的定时器——否则它会在用户已经成功之后又多发一次强制取数；
//   · 定时器触发时页面已经不在了（`alive()` 为假）⇒ 作废并忘掉这一轮的记号，下次渲染可以重新安排——不能留下「已重试过」的假记号；
//   · 成功 / 失败原因变了 ⇒ `ok`：这一轮结束，记号清空（下一轮限流重新有一次自动重试）；
//   · 等多久完全由服务端给的 `retryAfterSec`（闸门冷却剩余）决定，页面只加几秒余量；超过 `maxSec` 就不自动重试，交给用户。
export function createRetryGate(timers = { set: (f, ms) => setTimeout(f, ms), clear: (h) => clearTimeout(h) }) {
  const st = new Map(); // key → { timer, fired }
  const cancelPending = (key) => {
    const s = st.get(key);
    if (s && !s.fired) { timers.clear(s.timer); st.delete(key); }
  };
  return {
    begin(key) { cancelPending(key); },
    ok(key) { cancelPending(key); st.delete(key); },
    // 返回 "none"（不是限流）/ "scheduled"（刚安排了一次自动重试）/ "tried"（这一轮已经自动重试过、仍被限流）/ "manual"（等太久，交给用户）
    plan(key, { limited, retryAfterSec, run, alive, maxSec = 300, marginSec = 3 }) {
      if (!limited) { this.ok(key); return "none"; }
      const s = st.get(key);
      if (s) return s.fired ? "tried" : "scheduled";
      if (!(typeof retryAfterSec === "number" && Number.isFinite(retryAfterSec) && retryAfterSec >= 0 && retryAfterSec <= maxSec)) return "manual";
      const entry = { fired: false, timer: null };
      entry.timer = timers.set(() => {
        if (!alive()) { st.delete(key); return; }
        entry.fired = true;
        run();
      }, (retryAfterSec + marginSec) * 1000);
      st.set(key, entry);
      return "scheduled";
    },
  };
}
