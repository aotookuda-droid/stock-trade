// sapporo-stv-2026.html からシミュレーション部だけを取り出して統計を取る検証スクリプト
// 使い方: node race-sim/check_sim.js [試行回数]
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "sapporo-stv-2026.html"), "utf8");
const m = html.match(/\/\/ ==SIM_START==([\s\S]*?)\/\/ ==SIM_END==/);
if (!m) throw new Error("SIM ブロックが見つかりません");
const SIM = eval(m[1] + "\nSIM");

const N = parseInt(process.argv[2] || "200", 10);
const roster = SIM.defaultRoster();

// 能力・斤量を揃え脚質だけを4頭ずつにした中立フィールド（モデル自体の偏りを見る）
const NEUTRAL = ["逃げ", "先行", "差し", "追込"].flatMap((st, si) =>
  [0, 1, 2, 3].map(k => ({
    no: si * 4 + k + 1, name: st + (k + 1), jockey: "-", weight: 55.0, style: st, rating: 50
  }))
).sort((a, b) => a.no - b.no);

// 展開バランスの掃引: node check_sim.js 300 sweep [neutral]
if (process.argv[3] === "sweep") {
  const field = process.argv[4] === "neutral" ? NEUTRAL : roster;
  console.log("draft kickMul  W  | 勝ち時計中央 | 逃げ 先行 差し 追込");
  for (const draft of [0.20, 0.28, 0.34, 0.42, 0.50]) {
    for (const kickMul of [0.7, 0.85, 1.0]) {
      SIM.TUNE.draft = draft; SIM.TUNE.kickMul = kickMul;
      const st = { "逃げ": 0, "先行": 0, "差し": 0, "追込": 0 }, ts = [];
      for (let s = 1; s <= N; s++) {
        const r = SIM.simulate(field, s);
        st[r.results[0].style]++; ts.push(r.results[0].time);
      }
      ts.sort((a, b) => a - b);
      console.log(
        draft.toFixed(2).padStart(5), kickMul.toFixed(2).padStart(7), String(SIM.TUNE.W).padStart(3),
        "|", SIM.fmtTime(ts[N >> 1]).padStart(12), "|",
        ...["逃げ", "先行", "差し", "追込"].map(k => String(Math.round(st[k] / N * 100) + "%").padStart(4))
      );
    }
  }
  process.exit(0);
}

const times = [], last3f = [], spread = [], winStyle = {}, winNo = {}, pace1000 = [], winPop = {};

for (let s = 1; s <= N; s++) {
  const r = SIM.simulate(roster, s);
  const res = r.results;
  times.push(res[0].time);
  last3f.push(res[0].last3f);
  spread.push(res[res.length - 1].time - res[0].time);
  pace1000.push(r.pace);
  winStyle[res[0].style] = (winStyle[res[0].style] || 0) + 1;
  winNo[res[0].no] = (winNo[res[0].no] || 0) + 1;
  winPop[res[0].pop] = (winPop[res[0].pop] || 0) + 1;
}

const stat = a => {
  const b = a.slice().sort((x, y) => x - y);
  return {
    min: b[0].toFixed(2),
    p50: b[Math.floor(b.length / 2)].toFixed(2),
    max: b[b.length - 1].toFixed(2),
    avg: (a.reduce((x, y) => x + y, 0) / a.length).toFixed(2)
  };
};

console.log(`試行 ${N} レース`);
console.log("勝ち時計 (秒)   ", stat(times), "→", SIM.fmtTime(+stat(times).p50));
console.log("勝ち馬 上り3F   ", stat(last3f));
console.log("1000m通過       ", stat(pace1000));
console.log("1着-最下位 着差 ", stat(spread), "秒");
console.log("勝ち馬の脚質    ", winStyle);
console.log("勝ち馬の想定人気", Object.keys(winPop).sort((a, b) => a - b).map(k => `${k}人気:${winPop[k]}`).join("  "));
console.log("勝ち馬番の分布  ", Object.keys(winNo).sort((a, b) => a - b).map(k => `${k}:${winNo[k]}`).join(" "));

// 1レース分の着順表サンプル
const one = SIM.simulate(roster, 1);
console.log("\n--- 展開 #1 着順表 ---");
console.log("着 馬番 馬名               タイム   着差   通過        上り3F");
for (const r of one.results) {
  console.log(
    String(r.place).padStart(2) + " " +
    String(r.no).padStart(4) + " " +
    r.name.padEnd(11, "　") + " " +
    SIM.fmtTime(r.time).padStart(7) + " " +
    (r.margin || "—").padStart(5) + "  " +
    r.passing.padEnd(12) + " " +
    r.last3f.toFixed(1)
  );
}
console.log("\n--- 実況 ---");
for (const e of one.events) console.log(SIM.fmtTime(e.t).padStart(7), e.text);
