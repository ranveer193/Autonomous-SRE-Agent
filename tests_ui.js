// Offline test of the console's trace model. No browser, no cluster: node tests_ui.js
// Extracts the pure helpers from ui/index.html and feeds them a recorded-style event stream with PARALLEL subagents.
const fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "ui", "index.html"), "utf8");
const src = /<script id="pure">([\s\S]*?)<\/script>/.exec(html)[1];
const ctx = {}; vm.createContext(ctx); vm.runInContext(src + "\nthis.api={buildTrace,traceHtml,agentsHtml,statsHtml,rawRow,esc,compareHtml,modelsHtml,stepsHtml,tuningHtml,tuningCheck};", ctx);
const { buildTrace, traceHtml, agentsHtml, statsHtml, rawRow } = ctx.api;
let bad = 0; const ok = (n, c, d = "") => { console.log((c ? "PASS " : "FAIL ") + n + (c ? "" : "  " + d)); if (!c) bad++; };

const E = (t, o) => ({ t, depth: 0, kind: "x", msg: "", agent: "orchestrator", grp: "", ...o });
const evs = [
  E(0, { kind: "🚨", msg: "ALERT: something <b>bad</b>" }),
  E(1, { event: "subagent_start", rid: "A", grp: "", subagent: "log-inspector", msg: "delegate → log-inspector: look at logs", task_full: "look at logs in detail" }),
  E(1.1, { event: "subagent_start", rid: "B", grp: "", subagent: "metrics-inspector", msg: "delegate → metrics-inspector: look at metrics" }),
  E(1.2, { event: "llm_start", rid: "l1", grp: "A", agent: "log-inspector" }),
  E(1.3, { event: "llm_start", rid: "l2", grp: "B", agent: "metrics-inspector" }),
  E(2, { event: "llm_end", rid: "l1", grp: "A", agent: "log-inspector", msg: "decided: call search_logs (1.0s, 100→20 tok)", tool_calls: [{ name: "search_logs" }] }),
  E(2.1, { event: "tool_start", rid: "t1", grp: "A", agent: "log-inspector", tool: "search_logs", args: { q: "err" } }),
  E(2.2, { event: "tool_start", rid: "t2", grp: "B", agent: "metrics-inspector", tool: "query_metrics", args: { q: "up" } }),
  E(3, { event: "tool_end", rid: "t1", grp: "A", agent: "log-inspector", tool: "search_logs", out: "lines", secs: 0.9 }),
  E(4, { event: "subagent_end", rid: "A", grp: "", subagent: "log-inspector", out: "found <script>alert(1)</script>", secs: 3, sub_stats: {} }),
  E(5, { event: "subagent_end", rid: "B", grp: "", subagent: "metrics-inspector", failed: true, out: "boom", secs: 3.9 }),
  E(6, { event: "subagent_start", rid: "C", grp: "", subagent: "remediator", msg: "delegate → remediator: fix" }),
  E(7, { event: "approval_request", tool: "restart_deployment", args: { name: "x" }, agent: "remediator", msg: "approval requested: restart_deployment({'name': 'x'})" }),
];
let m = buildTrace(evs);
ok("two overlapping delegations are flagged parallel", m.subs[0].par && m.subs[1].par && !m.subs[2].par);
ok("max concurrency is 2", m.st.maxPar === 2, m.st.maxPar);
ok("tool calls credited to the right subagent", m.subs[0].n.tools === 1 && m.subs[1].n.tools === 1 && m.st.orch.n.tools === 0);
ok("tokens credited per agent", m.subs[0].n.tokIn === 100 && m.subs[1].n.tokIn === 0);
ok("failed subagent is marked failed and done", m.subs[1].failed && m.subs[1].done);
ok("running subagent counted as active", m.st.active === 1);
ok("waiting approval tracked", m.st.waiting && m.st.waiting.by === "remediator");
const open = new Map(), t = traceHtml(m, { open, running: true, detail: false });
ok("parallel group rendered", t.includes('class="par"') && t.includes("agents ran in parallel") || t.includes("working in parallel"));
ok("no raw html injection from alert or results", !t.includes("<b>bad</b>") && !t.includes("<script>alert"));
ok("approval shows who asked", t.includes("remediator"));
const a = agentsHtml(m, true);
ok("agents table lists every delegation", ["log-inspector", "metrics-inspector", "remediator", "orchestrator"].every(n => a.includes(n)));
ok("timeline lanes drawn", (a.match(/class="lane"/g) || []).length >= 4);
ok("stats render", statsHtml(m.st, 7).includes("agents working"));
ok("raw row escapes", !rawRow({ msg: "<img onerror=1>", kind: "x" }).includes("<img"));
// legacy logs without rid/grp/agent still render (depth-based nesting)
const old = [E(0, { event: "subagent_start", subagent: "k8s-inspector", msg: "delegate → k8s-inspector: t", depth: 0 }), E(1, { event: "tool_start", tool: "get_pods", args: {}, depth: 1 }), E(2, { event: "tool_end", tool: "get_pods", out: "ok", depth: 1 }), E(3, { event: "subagent_end", subagent: "k8s-inspector", out: "done", depth: 0 })];
m = buildTrace(old);
ok("legacy log without group ids still nests", m.subs.length === 1 && m.subs[0].done && m.subs[0].kids.length === 1);
// ---- comparison table
const row = (o) => ({ runs: 2, recovered: 2, mttr_s: 100, root_cause_acc: 1, distractor_rate: 0, tokens: 5000, tokens_main: 3000, tokens_sub: 2000, tool_calls: 10, tools_main: 2, tools_sub: 8, ...o });
const h = ctx.api.compareHtml({ "llm-only": row({ mttr_s: 300, tokens: 9000, root_cause_acc: 0.5 }), "<b>x</b>": row({ tokens: 4000, mttr_s: null }), memory: row({ runs: 2, recovered: 1 }) });
ok("table has a row per arm", (h.match(/<tr><td><b>/g) || []).length === 3);
ok("lowest tokens is marked best", /class="best">4\.0k/.test(h));
ok("highest root-cause accuracy is marked best, lower one is not", /class="best">100%/.test(h) && !/class="best">50%/.test(h));
ok("missing MTTR shows a dash, never zero", /<td>–<\/td>/.test(h) && !/>0 s</.test(h));
ok("recovered count shown", h.includes("1/2"));
ok("arm names are escaped", !h.includes("<b>x</b>") && h.includes("&lt;b&gt;x"));
ok("main / sub split shown", h.includes("3.0k") && h.includes("2.0k"));
ok("no data renders nothing", ctx.api.compareHtml({}) === "" && ctx.api.compareHtml(null) === "");
// ---- model check table
const mh = ctx.api.modelsHtml([
  { role: "main agent", provider: "groq", model: "m1", status: "ok", reachable: true, tool_calling: true, latency_s: 1.2 },
  { role: "subagents", provider: "groq", model: "<b>x</b>", status: "warn", reachable: true, tool_calling: false, latency_s: 0.8, hint: "pick another" },
  { role: "candidate", provider: "gemini", model: "m3", status: "fail", reachable: false, error: "401 bad <i>key</i>", hint: "key rejected" },
  { role: "idle", provider: "nvidia", model: "m4", status: "idle", key_set: false }]);
ok("model rows show Ready / Limited / Not working / No API key", ["Ready", "Limited", "Not working", "No API key"].every(x => mh.includes(x)));
ok("failed model shows its error and hint", mh.includes("key rejected") && mh.includes("401 bad"));
ok("model names and errors are escaped", !mh.includes("<b>x</b>") && !mh.includes("<i>key</i>"));
ok("no rows renders nothing", ctx.api.modelsHtml([]) === "" && ctx.api.modelsHtml(null) === "");
// ---- step timeline
const sh = ctx.api.stepsHtml([{ t: 100, stage: "baseline", msg: "NOT healthy <b>x</b>", level: "bad" }, { t: 112, stage: "agent", msg: "working", level: "info", log: "run1.jsonl" }, { t: 190, stage: "result", msg: "SKIP", level: "info" }], false);
ok("timeline shows offsets and stage names", sh.includes("+12s") && sh.includes("+1m 30s") && sh.includes(">check<") && sh.includes(">agent<"), sh.slice(0, 200));
ok("timeline escapes messages", !sh.includes("<b>x</b>"));
ok("timeline links the agent trace", sh.includes('data-trace="run1.jsonl"'));
ok("failed step is marked", sh.includes('class="bad"'));
ok("last running step shows a spinner", ctx.api.stepsHtml([{ t: 1, stage: "detect", msg: "waiting", level: "info" }], true).includes("spin"));
ok("empty steps explained", ctx.api.stepsHtml([], false).includes("No step details"));
// ---- tuning inputs (Advanced options)
const SPEC = {
  tool_budget: { min: 1, max: 200, uses: "budget", label: "Tool-call <budget>", hint: "Max calls. <script>x</script>" },
  inspector_calls: { min: 1, max: 30, uses: "", label: "Calls per inspector", hint: "Asked, not enforced." },
  report_words: { min: 30, max: 500, uses: "compact_reports", label: "Report words", hint: "Too low drops evidence." },
};
const th = ctx.api.tuningHtml(SPEC, { tool_budget: 30, inspector_calls: 8, report_words: 150 });
ok("one number input per setting, with its range", (th.match(/<input type="number"/g) || []).length === 3 && th.includes('min="1" max="200"') && th.includes('min="30" max="500"'));
ok("the default is the placeholder, so blank visibly means default", th.includes('placeholder="30"') && th.includes('placeholder="150"'));
ok("each input is tied to its label for keyboard and screen readers", th.includes('for="tn_tool_budget"') && th.includes('id="tn_tool_budget"') && th.includes('data-k="report_words"'));
ok("says which technique a number belongs to, or that it always applies", th.includes("only used when") && th.includes("always used"));
ok("labels and hints from the server are escaped", !th.includes("<budget>") && !th.includes("<script>x") && th.includes("&lt;budget&gt;"));
ok("no spec renders nothing", ctx.api.tuningHtml({}, {}) === "" && ctx.api.tuningHtml(null, null) === "");
let c = ctx.api.tuningCheck({ tool_budget: "20", inspector_calls: "", report_words: "  80 " }, SPEC);
ok("filled fields become numbers, blank fields are left out", c.error === null && c.values.tool_budget === 20 && c.values.report_words === 80 && !("inspector_calls" in c.values), JSON.stringify(c));
ok("nothing typed is valid and sends nothing", JSON.stringify(ctx.api.tuningCheck({ tool_budget: "" }, SPEC)) === JSON.stringify({ values: {}, error: null }));
ok("a number below the range is refused with the range", /Tool-call .* must be between 1 and 200/.test(ctx.api.tuningCheck({ tool_budget: "0" }, SPEC).error));
ok("a number above the range is refused", /between 30 and 500/.test(ctx.api.tuningCheck({ report_words: "501" }, SPEC).error));
ok("range limits themselves are accepted", ctx.api.tuningCheck({ tool_budget: "1" }, SPEC).error === null && ctx.api.tuningCheck({ tool_budget: "200" }, SPEC).error === null);
ok("a fraction, text or exponent is refused (the server refuses them too)", ["2.5", "abc", "1e2", "--3", "3 4"].every(v => /whole number/.test(ctx.api.tuningCheck({ tool_budget: v }, SPEC).error)));
ok("an unknown setting is refused", /unknown/.test(ctx.api.tuningCheck({ speed: "9" }, SPEC).error));
ok("a refused form sends no values at all", Object.keys(ctx.api.tuningCheck({ tool_budget: "20", report_words: "9999" }, SPEC).values).length === 0);
// ---- the page's browser-only script must at least parse (the pure block above is the only part executed here)
const scripts = [...html.matchAll(/<script(?: id="pure")?>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let parsed = true; try { scripts.forEach(sc => new vm.Script(sc)); } catch (e) { parsed = false; console.log(e.message); }
ok("every script block of the page parses", parsed && scripts.length >= 2);
ok("the suite request sends the tuning, and checks it before anything starts", /tuning=tuningVals\(\);if\(tuning===null\)return/.test(html) && /features:featVals\("sf"\),tuning\}/.test(html));
process.exit(bad ? 1 : 0);
