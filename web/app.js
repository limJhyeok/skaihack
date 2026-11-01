const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const state = { session: null, peer: "persona", busy: false, timer: null, timerSyncedAt: 0, tick: null };

function show(id) {
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== id));
  const examBar = ["exam", "submit"].includes(id) && state.session?.status === "running";
  ["#top-timer", "#btn-submit-open"].forEach((s) => $(s).classList.toggle("hidden", !examBar));
  $("#btn-pause").classList.toggle("hidden", !(examBar && state.session?.mode === "practice"));
  $("#top-qcount").classList.toggle("hidden", id !== "exam");
  if (!examBar) stopTimer();
  if (id === "home") { location.hash = ""; $("#top-title").textContent = "AI 실무 과제 모의고사"; loadSessions(); }
}
$$("[data-go]").forEach((b) => (b.onclick = () => show(b.dataset.go)));

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

// 오래 걸리는 LLM 호출 동안 경과 시간을 보여줘 멈춘 것처럼 보이지 않게 한다
async function withProgress(statusEl, label, promise) {
  const t0 = Date.now();
  const render = () => (statusEl.textContent = `${label} — ${Math.floor((Date.now() - t0) / 1000)}초 경과`);
  render();
  const tick = setInterval(render, 1000);
  try {
    return await promise;
  } finally {
    clearInterval(tick);
  }
}
const fmtTime = (ts) => new Date(ts).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });

// ---------- 홈 / 출제 ----------

async function loadOptions() {
  const o = await api("/api/options");
  const f = $("#form-new");
  o.domains.forEach((d) => f.domain.add(new Option(d, d)));
  o.archetypes.forEach((a) => f.archetype.add(new Option(`${a.id} — ${a.description}`, a.id)));
  o.difficulties.forEach((d) => f.difficulty.add(new Option(d, d, d === "standard", d === "standard")));
}

const STATUS = { created: "생성됨", running: "응시 중", submitted: "제출됨", rehearsed: "리허설 완료", evaluated: "평가 완료", revealed: "공개됨" };

async function loadSessions() {
  const rows = await api("/api/sessions");
  $("#session-rows").innerHTML = "";
  for (const s of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${s.session_id}</code></td><td></td><td>${s.domain}</td><td>${s.difficulty}</td><td>${STATUS[s.status]}</td><td><button>열기</button></td>`;
    tr.children[1].textContent = s.title;
    tr.querySelector("button").onclick = () => openSession(s.session_id);
    $("#session-rows").append(tr);
  }
  loadRehearsals();
}

$("#btn-new").onclick = () => show("new");

async function loadRehearsals() {
  const rows = await api("/api/rehearsals");
  $("#rehearsal-rows").innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><code>${r.rehearsal_id}</code></td><td></td><td>${r.status === "graded" ? "채점 완료" : "진행 중"}</td><td>${r.total ?? "-"}</td><td><button>열기</button></td>`;
    tr.children[1].textContent = r.repo_path;
    tr.querySelector("button").onclick = () => openRehearsal(r.rehearsal_id);
    $("#rehearsal-rows").append(tr);
  }
}

$("#form-new").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  $("#gen-status").textContent = "출제 중… (1~3분 걸릴 수 있습니다)";
  try {
    const s = await post("/api/problems", {
      domain: f.domain.value || null,
      archetype: f.archetype.value || null,
      difficulty: f.difficulty.value,
      seed: f.seed.value ? Number(f.seed.value) : null,
    });
    $("#gen-status").textContent = "";
    await openSession(s.session_id);
  } catch (err) {
    $("#gen-status").textContent = `출제 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

async function openSession(id) {
  const s = await api(`/api/sessions/${id}`);
  state.session = s;
  location.hash = `s=${id}`;
  $$(".ws-path").forEach((el) => (el.textContent = s.workspace_path));
  $("#top-title").textContent = s.title;
  if (s.status === "submitted") return showSubmitted();
  if (s.status === "rehearsed") return s.has_rehearsal ? openRehearsal(s.session_id) : showSubmitted();
  if (s.status === "evaluated") return openReport();
  if (s.status === "revealed") return openReveal();
  if (s.status === "created") {
    $("#c-title").textContent = s.title;
    $("#c-domain").textContent = s.domain;
    $("#c-archetype").textContent = s.archetype;
    $("#c-difficulty").textContent = s.difficulty;
    show("created");
  } else {
    enterExam(s);
  }
}

$$(".btn-copy").forEach((b) => (b.onclick = async () => {
  await navigator.clipboard.writeText(state.session.workspace_path);
  b.textContent = "복사됨";
  setTimeout(() => (b.textContent = "복사"), 1500);
}));

$("#btn-start").onclick = async () => {
  const mode = document.querySelector("input[name=mode]:checked").value;
  const s = await post(`/api/sessions/${state.session.session_id}/start`, { mode });
  enterExam({ ...state.session, ...s });
};

// ---------- 응시 화면 (DM 패널) ----------

async function enterExam(s) {
  state.session = s;
  $("#persona-name").textContent = s.persona.name;
  $("#persona-role").textContent = s.persona.role;
  $("#readme").textContent = s.readme_markdown || (await api(`/api/sessions/${s.session_id}`)).readme_markdown;
  $("#form-chat").text.disabled = s.status !== "running";
  show("exam");
  startTimer();
  await renderPeer();
}

$$(".contact").forEach((b) => (b.onclick = () => {
  $$(".contact").forEach((c) => c.classList.toggle("active", c === b));
  state.peer = b.dataset.peer;
  renderPeer();
}));

function setQuestionCount(n) {
  $("#top-qcount").classList.remove("hidden");
  $("#top-qcount").textContent = `담당자 질문 ${n}개`;
}

function bubble(m) {
  const div = document.createElement("div");
  div.className = `msg ${m.role === "user" ? "user" : "other"}`;
  div.textContent = m.text;
  const t = document.createElement("time");
  t.textContent = fmtTime(m.ts);
  div.append(t);
  $("#messages").append(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

async function renderPeer() {
  const id = state.session.session_id;
  const data = await api(`/api/sessions/${id}/${state.peer === "persona" ? "chat" : "helper"}`);
  if (data.question_count !== undefined) setQuestionCount(data.question_count);
  $("#messages").innerHTML = "";
  data.messages.forEach(bubble);
  // 마지막이 미응답 user 메시지면 재전송 버튼
  const last = data.messages.at(-1);
  if (last && last.role === "user") showRetry(last.text, "응답을 받지 못한 메시지가 있습니다.");
}

function showRetry(text, msg) {
  const div = document.createElement("div");
  div.className = "msg error";
  div.textContent = `${msg} `;
  const b = document.createElement("button");
  b.textContent = "재전송";
  b.onclick = () => { div.remove(); send(text, true); };
  div.append(b);
  $("#messages").append(div);
  $("#messages").scrollTop = $("#messages").scrollHeight;
}

async function send(text, isRetry = false) {
  if (state.busy) return;
  state.busy = true;
  const peer = state.peer;
  if (!isRetry) bubble({ role: "user", text, ts: new Date().toISOString() });
  try {
    const r = await post(`/api/sessions/${state.session.session_id}/${peer === "persona" ? "chat" : "helper"}`, { text });
    if (peer === state.peer) bubble(r.reply);
    if (r.question_count !== undefined) setQuestionCount(r.question_count);
  } catch (err) {
    if (peer === state.peer) showRetry(text, err.message);
  } finally {
    state.busy = false;
  }
}

$("#form-chat").text.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    const text = e.target.value.trim();
    if (!text || state.busy) return;
    e.target.value = "";
    send(text);
  }
});

// ---------- 타이머 / 단계 알림 ----------

const pad = (n) => String(n).padStart(2, "0");
const hms = (sec) => `${pad(Math.floor(sec / 3600))}:${pad(Math.floor((sec % 3600) / 60))}:${pad(sec % 60)}`;

async function syncTimer() {
  state.timer = await api(`/api/sessions/${state.session.session_id}/timer`);
  state.timerSyncedAt = Date.now();
}

function remainingNow() {
  const t = state.timer;
  if (!t) return 0;
  if (t.paused) return t.remaining_sec;
  return Math.max(0, t.remaining_sec - Math.floor((Date.now() - state.timerSyncedAt) / 1000));
}

function activeElapsedNow() {
  const t = state.timer;
  return t.paused ? t.active_elapsed_sec : t.active_elapsed_sec + Math.floor((Date.now() - state.timerSyncedAt) / 1000);
}

function renderTimer() {
  const rem = remainingNow();
  const el = $("#top-timer");
  el.textContent = state.timer.paused ? `${hms(rem)} 남음 (일시정지)` : `${hms(rem)} 남음`;
  el.classList.toggle("low", rem > 0 && rem <= 15 * 60);
  el.classList.toggle("over", rem === 0);
  $("#btn-pause").textContent = state.timer.paused ? "재개" : "일시정지";
  $("#form-chat").text.disabled = rem === 0 || state.timer.paused || state.session.status !== "running";
  checkAlerts();
  if (rem === 0) onTimeOver();
}

async function startTimer() {
  timeOverHandled = false;
  stopTimer();
  await syncTimer();
  renderTimer();
  let n = 0;
  state.tick = setInterval(async () => {
    if (++n % 15 === 0) await syncTimer().catch(() => {});
    renderTimer();
  }, 1000);
}

function stopTimer() {
  if (state.tick) clearInterval(state.tick);
  state.tick = null;
}

function checkAlerts() {
  const key = `alerts_${state.session.session_id}`;
  const shown = new Set(JSON.parse(localStorage.getItem(key) || "[]"));
  const elapsedMin = activeElapsedNow() / 60;
  const due = state.timer.alerts.filter((a) => a.at_min <= elapsedMin && !shown.has(a.at_min));
  if (!due.length) return;
  due.forEach((a) => shown.add(a.at_min));
  localStorage.setItem(key, JSON.stringify([...shown]));
  const a = due.at(-1); // 늦게 열었으면 가장 최근 알림만
  toast(`[${a.at_min}분] ${a.text}`);
  post(`/api/sessions/${state.session.session_id}/alert`, { at_min: a.at_min }).catch(() => {});
}

function toast(text) {
  const div = document.createElement("div");
  div.className = "toast";
  div.textContent = text;
  const b = document.createElement("button");
  b.textContent = "닫기";
  b.onclick = () => div.remove();
  div.append(b);
  $("#toasts").append(div);
}

let timeOverHandled = false;
function onTimeOver() {
  if (timeOverHandled) return;
  timeOverHandled = true;
  toast("시간이 종료되었습니다. 제출 화면으로 이동합니다.");
  openSubmit(true);
}

$("#btn-pause").onclick = async () => {
  const action = state.timer.paused ? "resume" : "pause";
  try {
    state.timer = await post(`/api/sessions/${state.session.session_id}/${action}`, {});
    state.timerSyncedAt = Date.now();
    renderTimer();
  } catch (err) { toast(err.message); }
};

// ---------- 제출 ----------

$("#btn-submit-open").onclick = () => openSubmit(false);
$("#btn-submit-cancel").onclick = () => enterExam(state.session);

async function openSubmit(timeOver) {
  const info = await api(`/api/sessions/${state.session.session_id}/submit-info`);
  const f = $("#form-submit");
  $("#readme-run").textContent = `${info.readme_command ?? "실행 명령 없음"} (포트 ${info.readme_port ?? "없음"})`;
  $("#dirty-box").classList.toggle("hidden", !info.dirty_files.length);
  $("#dirty-list").textContent = info.dirty_files.join("\n");
  $("#submit-timeover").classList.toggle("hidden", !timeOver);
  $("#btn-submit-cancel").classList.toggle("hidden", timeOver);
  show("submit");
}

$("#form-submit").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  $("#submit-status").textContent = "제출 중… (스냅샷·로그 수집)";
  try {
    await post(`/api/sessions/${state.session.session_id}/submit`, {
      agent_tool: f.agent_tool.value,
      auto_commit: f.auto_commit.checked,
      manual_tokens: f.manual_tokens.value ? Number(f.manual_tokens.value) : null,
    });
    $("#submit-status").textContent = "";
    state.session.status = "submitted";
    state.session.has_rehearsal = false;
    await showSubmitted();
  } catch (err) {
    $("#submit-status").textContent = `제출 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

async function showSubmitted() {
  const r = await api(`/api/sessions/${state.session.session_id}/submission`);
  const tok = r.token_usage ? `${(r.token_usage.billable ?? r.token_usage.total).toLocaleString()} (출처: ${r.token_source})` : r.token_source;
  const rows = [
    ["제출 시각", r.submitted_at],
    ["자동 커밋", r.auto_committed ? "예" : "아니오"],
    ["git tag", r.tag],
    ["스냅샷 파일 수", r.snapshot_files.length],
    ["커밋 수", r.commit_count],
    ["에이전트 로그", `${r.agent_status} (세션 ${r.agent_session_count}개)`],
    ["토큰", tok],
    ["수집한 문서", r.docs_found.join(", ") || "없음"],
  ];
  $("#sub-meta").innerHTML = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    $("#sub-meta").append(dt, dd);
  }
  $("#sub-secrets").innerHTML = "";
  if (!r.secret_findings.length) $("#sub-secrets").textContent = "발견된 비밀키 없음";
  for (const s of r.secret_findings) {
    const p = document.createElement("p");
    p.className = "secret";
    p.textContent = `${s.file}${s.line ? ":" + s.line : ""} — ${s.kind} — ${s.preview}`;
    $("#sub-secrets").append(p);
  }
  $("#sub-rh-start").classList.toggle("hidden", !!state.session.has_rehearsal);
  $("#sub-rh-resume").classList.toggle("hidden", !state.session.has_rehearsal);
  show("submitted");
}

// ---------- 설명 리허설 ----------

// 모드 선택 블록(템플릿)을 두 화면에 붙인다
$$(".rh-mode").forEach((slot) => {
  slot.append($("#tpl-rh-mode").content.cloneNode(true));
  const fs = slot.querySelector("fieldset");
  const sync = () => {
    const open = fs.querySelector("input[name=rh_mode]:checked").value === "open_book";
    fs.querySelector(".nolimit").classList.toggle("hidden", !open);
    fs.querySelector(".pledge").classList.toggle("hidden", open);
  };
  fs.querySelectorAll("input[name=rh_mode]").forEach((r) => (r.onchange = sync));
});

function readMode(slot) {
  const fs = slot.querySelector("fieldset");
  const mode = fs.querySelector("input[name=rh_mode]:checked").value;
  if (mode === "closed_book" && !fs.querySelector("input[name=pledge]").checked) {
    throw new Error("closed book 서약에 체크하세요.");
  }
  return { mode, no_time_limit: mode === "open_book" && fs.querySelector("input[name=no_time_limit]").checked };
}

$("#btn-rh-start").onclick = async () => {
  const btn = $("#btn-rh-start");
  try {
    const m = readMode($("#sub-rh-start .rh-mode"));
    btn.disabled = true;
    await withProgress($("#rh-start-status"), "확인 문제 출제 중… (보통 1~3분)",
      post(`/api/sessions/${state.session.session_id}/rehearsal`, m));
    $("#rh-start-status").textContent = "";
    state.session.has_rehearsal = true;
    await openRehearsal(state.session.session_id);
  } catch (err) {
    $("#rh-start-status").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};
$("#btn-rh-resume").onclick = () => openRehearsal(state.session.session_id)
  .catch((err) => ($("#rh-start-status").textContent = err.message));

$("#btn-standalone").onclick = () => {
  const f = $("#form-standalone");
  if (!f.count.value) f.count.value = 6;
  if (!f.seconds.value) f.seconds.value = 300;
  show("standalone");
};

$("#form-standalone").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button[type=submit]");
  try {
    const m = readMode(f.querySelector(".rh-mode"));
    btn.disabled = true;
    const r = await withProgress($("#standalone-status"), "확인 문제 출제 중… (보통 1~3분)", post("/api/rehearsals", {
      repo_path: f.repo_path.value, purpose: f.purpose.value,
      count: Number(f.count.value), seconds: Number(f.seconds.value), ...m,
    }));
    $("#standalone-status").textContent = "";
    await openRehearsal(r.rehearsal_id);
  } catch (err) {
    $("#standalone-status").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

const rh = { id: null, q: null, deadline: null, tick: null, sending: false };

async function openRehearsal(rid) {
  rh.id = rid;
  const r = await api(`/api/rehearsals/${rid}`);
  if (r.status === "graded") return showResults(r);
  if (r.answered === r.total) return gradeNow();
  await askNext();
}

async function askNext() {
  const { question } = await post(`/api/rehearsals/${rh.id}/next`, {});
  if (!question) return gradeNow();
  rh.q = question;
  $("#rh-progress").textContent = `${question.followup ? "재심문 " : ""}문항 ${question.index} / ${question.total}`;
  $("#rh-type").textContent = question.type;
  $("#rh-question").textContent = question.question;
  $("#rh-answer").value = sessionStorage.getItem(`rh_${rh.id}_${question.id}`) || "";
  $("#rh-status").textContent = "";
  $("#btn-rh-answer").disabled = false;
  clearInterval(rh.tick);
  rh.deadline = question.remaining_sec === null ? null : Date.now() + question.remaining_sec * 1000;
  renderRhTimer();
  rh.tick = setInterval(renderRhTimer, 500);
  show("rehearsal");
  $("#rh-answer").focus();
}

function renderRhTimer() {
  if (rh.deadline === null) { $("#rh-timer").textContent = "제한 없음"; return; }
  const left = Math.max(0, Math.ceil((rh.deadline - Date.now()) / 1000));
  $("#rh-timer").textContent = `${Math.floor(left / 60)}:${pad(left % 60)} 남음`;
  $("#rh-timer").classList.toggle("low", left <= 30 && left > 0);
  $("#rh-timer").classList.toggle("over", left === 0);
  if (left === 0) submitAnswer(true);
}

// 붙여넣기·끌어놓기 차단 (5.8)
["paste", "drop"].forEach((ev) => $("#rh-answer").addEventListener(ev, (e) => {
  e.preventDefault();
  $("#rh-status").textContent = "붙여넣기는 허용되지 않습니다. 직접 타이핑하세요.";
}));
$("#rh-answer").addEventListener("input", () => sessionStorage.setItem(`rh_${rh.id}_${rh.q.id}`, $("#rh-answer").value));
$("#btn-rh-answer").onclick = () => submitAnswer(false);

async function submitAnswer(timedOut) {
  if (rh.sending || !rh.q) return;
  rh.sending = true;
  clearInterval(rh.tick);
  $("#btn-rh-answer").disabled = true;
  if (timedOut) $("#rh-status").textContent = "시간 초과 — 입력한 내용으로 제출합니다.";
  try {
    await post(`/api/rehearsals/${rh.id}/questions/${rh.q.id}/answer`, { answer: $("#rh-answer").value, timed_out: timedOut });
    sessionStorage.removeItem(`rh_${rh.id}_${rh.q.id}`);
    rh.q = null;
    await askNext();
  } catch (err) {
    $("#rh-status").textContent = `제출 실패: ${err.message}`;
    $("#btn-rh-answer").disabled = false;
  } finally {
    rh.sending = false;
  }
}

async function gradeNow() {
  show("rehearsal");
  clearInterval(rh.tick);
  $("#rh-progress").textContent = "채점 중…";
  $("#rh-type").textContent = "";
  $("#rh-question").textContent = "모든 문항에 답했습니다. 채점하고 있습니다 (30초~1분).";
  $("#rh-timer").textContent = "";
  $("#btn-rh-answer").disabled = true;
  try {
    showResults(await post(`/api/rehearsals/${rh.id}/grade`, {}));
  } catch (err) {
    $("#rh-question").textContent = `채점 실패: ${err.message}`;
    const b = document.createElement("button");
    b.textContent = "다시 채점";
    b.onclick = gradeNow;
    $("#rh-question").append(" ", b);
  }
}

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

function showResults(r) {
  const sc = r.scores;
  const sum = $("#rh-summary");
  sum.innerHTML = "";
  const row = el("div", "score-row");
  const tile = (label, value, sub) => {
    const t = el("div", "score");
    t.append(el("span", "muted", label), el("b", "", value ?? "-"), el("small", "muted", sub || ""));
    return t;
  };
  row.append(tile("인지 관리", sc.total, Object.entries(sc.verdict_counts).map(([k, v]) => `${k} ${v}`).join(" · ")));
  for (const c of Object.values(sc.by_category)) row.append(tile(c.label, c.score, `${c.questions}문항`));
  sum.append(row);
  if (r.followup_scores) sum.append(el("p", "muted", `재심문 점수: ${r.followup_scores.total} (본 점수에는 포함하지 않음)`));

  const cards = $("#rh-cards");
  cards.innerHTML = "";
  for (const q of [...r.questions, ...r.followups]) {
    const g = q.grade;
    const c = el("div", "card-q");
    const h = el("h3", "", `${q.followup ? "[재심문] " : ""}${q.id} · ${q.type}`);
    h.append(el("span", `badge ${g.verdict}`, g.verdict));
    c.append(h, el("p", "", q.question));
    c.append(el("p", "muted", `내 답 (${q.elapsed_sec}초${q.timed_out ? ", 시간 초과" : ""})`), el("div", "mine", q.answer || "(빈 답)"));
    const dl = el("dl", "model");
    for (const [k, label] of [["weak_point", "약점 지점"], ["impact", "파급"], ["plan", "검증·개선 계획"]]) {
      const dt = el("dt", "", `모범 ${label}`);
      dt.append(el("span", `badge ${g.steps[k]}`, g.steps[k]));
      const dd = el("dd", "", q.model_answer[k].text);
      dd.append(el("div", "ev", q.model_answer[k].evidence.join(", ")));
      dl.append(dt, dd);
    }
    c.append(dl);
    if (g.misconception) c.append(el("p", "", `오해: ${g.misconception}`));
    if (g.revisit?.length) c.append(el("p", "ev", `다시 볼 곳: ${g.revisit.join(", ")}`));
    c.append(el("p", "", `피드백: ${g.comment}`));
    cards.append(c);
  }
  $("#btn-rh-followup").classList.toggle("hidden", !r.can_followup);
  $("#btn-report").classList.toggle("hidden", r.kind !== "session");
  show("rh-results");
}

$("#btn-rh-followup").onclick = async () => {
  $("#btn-rh-followup").disabled = true;
  try {
    await withProgress($("#rh-followup-status"), "재심문 출제 중… (보통 1~2분)", post(`/api/rehearsals/${rh.id}/followup`, {}));
    $("#rh-followup-status").textContent = "";
    await askNext();
  } catch (err) {
    $("#rh-followup-status").textContent = err.message;
  } finally {
    $("#btn-rh-followup").disabled = false;
  }
};

// ---------- 평가 리포트 ----------

$("#btn-report").onclick = () => openReport();
$("#btn-reevaluate").onclick = () => openReport(true);

async function openReport(force = false) {
  const sid = state.session.session_id;
  show("report");
  $("#report-body").innerHTML = "";
  let rep = null;
  // 리포트가 있을 때만 조회한다 (없는 리포트를 조회해 404를 내지 않도록)
  if (!force && state.session.has_report) rep = await api(`/api/sessions/${sid}/report`).catch(() => null);
  if (!rep) {
    $("#btn-reveal").disabled = $("#btn-reevaluate").disabled = true;
    try {
      rep = await withProgress($("#report-status"),
        "평가 중… 제출물 자동 실행(최대 60초) + 테스트 + 3축 LLM 평가 (보통 2~4분)", post(`/api/sessions/${sid}/evaluate`, {}));
      state.session.has_report = true;
    } catch (err) {
      $("#report-status").textContent = `평가 실패: ${err.message}`;
      $("#btn-reevaluate").disabled = false;
      return;
    }
  }
  $("#report-status").textContent = `생성: ${rep.generated_at}`;
  $("#btn-reveal").disabled = $("#btn-reevaluate").disabled = false;
  renderReport(rep);
}

const fmtSec = (t) => (t == null ? "-" : hms(Math.max(0, t)));

function itemCard(it) {
  const c = el("div", "item");
  const head = el("div", "head");
  const title = el("b", "", it.label);
  title.append(el("span", `badge ${it.verdict}`, it.verdict));
  head.append(title, el("span", "pts", it.score == null ? "-" : `${it.score} / ${it.max}`));
  c.append(head, el("p", "", it.rationale));
  if (it.evidence?.length) c.append(el("div", "ev", "증거: " + it.evidence.join(" · ")));
  if (it.suggestion) c.append(el("p", "muted", "개선: " + it.suggestion));
  return c;
}

function kvList(rows) {
  const dl = el("dl", "kv");
  for (const [k, v] of rows) dl.append(el("dt", "", k), el("dd", "", v));
  return dl;
}

function listBlock(title, items) {
  const d = el("div");
  d.append(el("h3", "", title));
  const ul = el("ul");
  (items.length ? items : ["없음"]).forEach((t) => ul.append(el("li", "", t)));
  d.append(ul);
  return d;
}

function details(summary, text) {
  const d = el("details");
  d.append(el("summary", "", summary), el("pre", "", text || "(없음)"));
  return d;
}

function axis(title, score, max, items) {
  const sec = el("section", "axis");
  const h = el("h2", "", title);
  h.append(el("span", "pts", score == null ? "-" : `${score} / ${max}`));
  sec.append(h);
  items.forEach((it) => sec.append(itemCard(it)));
  return sec;
}

function renderTimeline(tl) {
  const box = el("div", "tl");
  const lanes = [["chat", "담당자 질문"], ["fact", "사실 공개"], ["session", "에이전트 세션"], ["prompt", "프롬프트"], ["verify", "테스트·확인"], ["commit", "커밋"], ["alert", "알림·제출"]];
  for (const [lane, label] of lanes) {
    const row = el("div", "tl-row");
    const track = el("div", "tl-track");
    for (const e of tl.events.filter((x) => x.lane === lane)) {
      const dot = el("div", `tl-dot lane-${lane}${e.ok === false ? " fail" : ""}`);
      dot.style.left = `${(100 * e.t) / tl.duration_sec}%`;
      dot.title = `+${fmtSec(e.t)} ${e.label}`;
      track.append(dot);
    }
    row.append(el("span", "", label), track);
    box.append(row);
  }
  const ax = el("div", "tl-axis");
  const ticks = el("div");
  for (let i = 0; i <= 4; i++) ticks.append(el("span", "", fmtSec(Math.round((tl.duration_sec * i) / 4))));
  ax.append(el("span"), ticks);
  box.append(ax);
  return box;
}

function renderReport(rep) {
  const b = $("#report-body");
  b.innerHTML = "";
  const sc = rep.scores;
  // 1. 요약
  const row = el("div", "score-row");
  const tile = (label, v, sub) => {
    const t = el("div", "score");
    t.append(el("span", "muted", label), el("b", "", v ?? "-"), el("small", "muted", sub || ""));
    return t;
  };
  row.append(tile("기술 관리", sc.technical, "/ 100"), tile("의도 관리", sc.intent, "/ 100"),
    tile("인지 관리", sc.cognitive, "/ 100"), tile("소통 (참고)", sc.communication, "/ 100"));
  b.append(row, el("p", "rh-question", rep.summary.one_liner));
  b.append(kvList([["가장 큰 강점", rep.summary.top_strength], ["가장 큰 약점", rep.summary.top_weakness]]));

  // 2. 축별 상세
  const t = rep.technical;
  const tech = axis("기술 관리", sc.technical, 100, t.items);
  const run = t.run;
  tech.append(el("h3", "", "자동 실행"));
  tech.append(kvList([
    ["실행 명령 (README)", `${run.command ?? "없음"} (포트 ${run.port ?? "없음"})`],
    ["결과", run.started ? `기동 ${run.startup_sec}초, GET / → ${run.http_status ?? "응답 없음"}` : `실패: ${run.error}`],
    ["0.0.0.0 바인딩", run.bound_all_interfaces == null ? "확인 안 됨"
      : run.bound_all_interfaces ? "예" : "아니오: 127.0.0.1에만 열림 → 실전 브라우저 미리보기에서 열리지 않음"],
    ["테스트", run.tests.found ? `${run.tests.command} → exit ${run.tests.exit_code}` : "테스트 없음"],
    ["스크린샷", run.screenshot ? "저장됨 (data/sessions/…/screenshot.png)" : "없음 (Playwright 미설치)"],
    ["비밀키 검사", t.secret_findings.length ? t.secret_findings.map((f) => `${f.file}:${f.line ?? "-"} ${f.kind}`).join(", ") : "발견 없음"],
  ]));
  tech.append(details("실행 로그 마지막 100줄", run.log_tail), details("루트 페이지 응답 앞부분", run.body_head));
  if (run.tests.found) tech.append(details("테스트 출력", run.tests.output_tail));
  tech.append(el("p", "muted", "발견 못 한 요구의 영향: " + t.undiscovered_note));
  tech.append(listBlock("강점", t.strengths), listBlock("개선할 행동", t.improvements));
  b.append(tech);

  const i = rep.intent;
  const m = i.metrics;
  const intent = axis("의도 관리", sc.intent, 100, i.items);
  const dv = m.interview.discovery;
  const vr = m.verification_runs, cs = m.commit_sizes;  // 이전 리포트에는 없음
  const rate = (x) => `${x.found}/${x.total}`;
  intent.append(el("h3", "", "정량 지표"));
  intent.append(kvList([
    ["담당자 질문 수 / 평균 구체성", `${m.interview.questions}개 / ${m.interview.avg_specificity ?? "-"}`],
    ["덤프 요청", `${m.interview.dump_requests}회`],
    ["숨은 사실 발견 (전체·critical·constraint·conflict)", `${rate(dv.all)} · ${rate(dv.critical)} · ${rate(dv.constraint)} · ${rate(dv.conflict)}`],
    ["첫 구현 커밋 전 질문 수", `${m.interview.questions_before_first_impl_commit}개 (첫 구현 커밋 +${fmtSec(m.first_impl_commit?.elapsed_sec)})`],
    ["서면 합의", m.interview.written_agreement.exists ? `있음 (+${fmtSec(m.interview.written_agreement.elapsed_sec)})` : "없음"],
    ["ACCEPTANCE.md", m.acceptance.exists ? `있음${m.acceptance.before_first_impl_commit ? " (구현 전 작성)" : ""}` : "없음"],
    ["에이전트 프롬프트 수 / 평균 길이", `${m.agent.prompts}개 / ${m.agent.avg_prompt_chars ?? "-"}자 (${m.agent.status})`],
    ["README 통째 붙여넣기", m.agent.readme_paste.length ? m.agent.readme_paste.map((p) => `S${p.session} +${fmtSec(p.elapsed_sec)} (겹침 ${Math.round(p.overlap * 100)}%)`).join(", ") : "없음"],
    ["구현 전 계획·대안 요청", `${m.agent.planning_before_impl.length}회`],
    ["검증 관련 프롬프트 비율", m.agent.verification_ratio == null ? "-" : `${Math.round(m.agent.verification_ratio * 100)}%`],
    ["테스트 실행 (통과·실패)", vr ? `${vr.test_runs}회 (${vr.test_passed}·${vr.test_failed})${vr.fail_then_pass ? ", 실패 후 통과 있음" : ""}${vr.last_test ? `, 마지막 +${fmtSec(vr.last_test.elapsed_sec)} ${vr.last_test.ok === false ? "실패" : vr.last_test.ok ? "통과" : "결과 모름"}` : ""}` : "-"],
    ["마지막 통과 뒤 코드 커밋 / 서버 확인", vr ? `${vr.code_commits_after_last_pass ?? "-"}개 / ${vr.smoke_checks}회` : "-"],
    ["커밋 크기 (개수·중앙값·최대)", cs ? (cs.total_lines ? `${cs.commits}개 · ${cs.median_lines}줄 · ${cs.max_lines}줄 (전체의 ${Math.round(cs.max_share * 100)}%)${cs.big_bang ? " ⚠ 한 커밋에 몰림" : ""}` : `${cs.commits}개`) : "-"],
    ["마지막 30분 변경 비중", cs?.final_30min_share == null ? "-" : `${Math.round(cs.final_30min_share * 100)}%`],
    ["전체 재생성 감지", m.agent.full_regeneration.length ? m.agent.full_regeneration.map((p) => `S${p.session} +${fmtSec(p.elapsed_sec)} "${p.text.slice(0, 40)}"`).join(" / ") : "없음"],
    ["토큰 급증 구간", m.agent.token_spikes.length ? m.agent.token_spikes.map((p) => `S${p.session} +${fmtSec(p.elapsed_sec)} ${p.tokens.toLocaleString()}`).join(", ") : "없음"],
    ["세션 수 / 전환 전 HANDOFF 갱신", `${m.agent.sessions}개 / ${m.agent.session_switches.map((x) => (x.handoff_committed_before || x.handoff_edited_in_prev_session ? "O" : "X")).join(" ") || "-"}`],
    ["총 토큰 (billable)", m.tokens ? `${(m.tokens.billable ?? m.tokens.total).toLocaleString()} (${m.token_source})` : m.token_source],
  ]));
  intent.append(kvList([["토큰 효율", i.cost_note], ["가장 잘한 장면", i.best_moment], ["가장 아쉬운 장면", i.worst_moment]]));
  b.append(intent);

  b.append(axis("인지 관리", sc.cognitive, 100, rep.cognitive.items));
  const comm = rep.communication;
  b.append(axis("소통 (참고 점수)", comm.score, 100, [{ label: "인계 가능성", max: 100, ...comm, suggestion: "" }]));

  // 2-1. 문서-코드 불일치
  const cons = el("section", "axis");
  cons.append(el("h2", "", `문서-코드 일치 검사 (불일치·부분 ${rep.consistency.mismatches}건)`));
  const tbl = el("table", "grid");
  tbl.innerHTML = "<tr><th>문서</th><th>주장</th><th>판정</th><th>근거</th><th>메모</th></tr>";
  for (const c of rep.consistency.claims) {
    const tr = el("tr", c.verdict === "inconsistent" || c.verdict === "partially" ? "bad" : "");
    [c.doc, c.claim, c.verdict, c.evidence.join(", "), c.note].forEach((v) => tr.append(el("td", "", v)));
    tbl.append(tr);
  }
  if (!rep.consistency.claims.length) cons.append(el("p", "muted", "검사할 문서(RISKS/DECISIONS/ACCEPTANCE/README 수정본)가 없습니다."));
  else cons.append(tbl);
  b.append(cons);

  // 3. 타임라인
  const tl = el("section", "axis");
  tl.append(el("h2", "", "타임라인"), el("p", "muted", "점에 마우스를 올리면 내용이 보입니다."), renderTimeline(rep.timeline));
  b.append(tl);

  // 4. 다음에 바꿀 행동
  b.append(listBlock("다음 모의고사에서 바꿀 행동 3가지", i.next_actions));
}

// ---------- 정답 공개 / 회고 ----------

$("#btn-reveal").onclick = async () => {
  $("#btn-reveal").disabled = true;
  $("#report-status").textContent = "공개 준비 중… (놓친 질문 분석)";
  try {
    await post(`/api/sessions/${state.session.session_id}/reveal`, {});
    await openReveal();
  } catch (err) {
    $("#report-status").textContent = `공개 실패: ${err.message}`;
  } finally {
    $("#btn-reveal").disabled = false;
  }
};
$("#btn-to-report").onclick = () => openReport();

async function openReveal() {
  const rv = await api(`/api/sessions/${state.session.session_id}/reveal`);
  const b = $("#reveal-body");
  b.innerHTML = "";
  const facts = rv.facts;
  const crit = facts.filter((f) => f.importance === "critical");
  b.append(el("p", "rh-question", `발견 ${facts.filter((f) => f.discovered).length}/${facts.length} · critical ${crit.filter((f) => f.discovered).length}/${crit.length}`));
  const tbl = el("table", "grid");
  tbl.innerHTML = "<tr><th>ID</th><th>내용</th><th>유형</th><th>중요도</th><th>발견</th></tr>";
  for (const f of facts) {
    const tr = el("tr", f.discovered ? "" : "bad");
    tr.append(el("td", "", f.id), el("td", "", f.statement), el("td", "", f.type_label), el("td", "", f.importance));
    tr.append(el("td", f.discovered ? "found" : "notfound",
      f.discovered ? `질문 #${f.question_no} (채팅 #${f.chat_turn})` : f.partial ? "부분 언급" : "놓침"));
    tbl.append(tr);
  }
  b.append(tbl);
  const missed = facts.filter((f) => !f.discovered);
  if (missed.length) {
    b.append(el("h2", "", "이런 질문을 했으면 찾았을 것"));
    for (const f of missed) {
      const c = el("div", "item");
      c.append(el("b", "", `${f.id} · ${f.statement}`));
      c.append(kvList([
        ["출제자 예시 질문", f.example_question],
        ["내 질문 중 가장 가까웠던 것", f.closest_user_question || "없음"],
        ["빗나간 이유", f.why_missed || "-"],
        ["더 나은 질문 (better_question)", f.better_question || "-"],
      ]));
      b.append(c);
    }
  }
  b.append(listBlock("과설계 함정", rv.overengineering_traps), listBlock("담당자의 우선순위 혼란", rv.priority_confusion),
    listBlock("좋은 해법 기준 (평가자 참고)", rv.good_solution_criteria));
  if (rv.data_file_notes) b.append(kvList([["데이터 파일 메모", rv.data_file_notes]]));
  if (rv.ai_fit) {
    b.append(kvList([
      ["AI 적합도", rv.ai_fit_label || rv.ai_fit],
      ["기술 조건", rv.tech_requirement || "없음"],
      ["숨은 사실 주제", (rv.themes || []).join(", ") || "-"],
    ]));
  }
  $("#retro").value = rv.retro_md;
  $("#retro-status").textContent = "";
  show("reveal");
}

$("#btn-retro-save").onclick = async () => {
  await api(`/api/sessions/${state.session.session_id}/retro`, { method: "PUT", body: JSON.stringify({ text: $("#retro").value }) });
  $("#retro-status").textContent = "retro.md에 저장했습니다.";
};

// ---------- 기록 대시보드 ----------

const SERIES = [["technical", "기술", "--series-1"], ["intent", "의도", "--series-2"], ["cognitive", "인지", "--series-3"]];
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

$("#btn-dashboard").onclick = openDashboard;

async function openDashboard() {
  location.hash = "dashboard";
  const d = await api("/api/dashboard");
  renderTrend(d.trend);
  renderSessionTable(d.sessions);
  renderTypes(d);
  show("dashboard");
}

function renderTrend(trend) {
  const box = $("#dash-chart");
  box.innerHTML = "";
  if (!trend.length) {
    box.append(el("p", "muted", "평가 리포트가 있는 세션이 아직 없습니다."));
    return;
  }
  const root = el("div", "viz-root");
  const legend = el("div", "viz-legend");
  for (const [, label, v] of SERIES) {
    const item = el("span", "", label);
    const sw = el("i");
    sw.style.background = `var(${v})`;
    item.prepend(sw);
    legend.append(item);
  }
  root.append(legend);

  const W = 760, H = 260, L = 36, R = 64, T = 12, B = 34;
  const pw = W - L - R, ph = H - T - B;
  const n = trend.length;
  const x = (i) => L + (n === 1 ? pw / 2 : (pw * i) / (n - 1));
  const y = (v) => T + ph - (ph * v) / 100;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "세션별 기술·의도·인지 점수 추이" });
  for (const g of [0, 25, 50, 75, 100]) {
    svg.append(svgEl("line", { class: "viz-grid", x1: L, x2: W - R, y1: y(g), y2: y(g) }));
    const t = svgEl("text", { class: "viz-axis-text", x: L - 6, y: y(g) + 4, "text-anchor": "end" });
    t.textContent = g;
    svg.append(t);
  }
  trend.forEach((p, i) => {
    const t = svgEl("text", { class: "viz-axis-text", x: x(i), y: H - 12, "text-anchor": "middle" });
    t.textContent = `#${i + 1} ${(p.date || "").slice(5).replace("-", "/")}`;
    svg.append(t);
  });
  const cross = svgEl("line", { class: "viz-cross", y1: T, y2: T + ph, visibility: "hidden" });
  svg.append(cross);
  const lastLabels = [];
  for (const [key, label, v] of SERIES) {
    const color = `var(${v})`;
    // 값이 없는 세션(리허설 안 함 등)에서는 선을 끊는다
    let seg = [];
    const flush = () => {
      if (seg.length > 1) svg.append(svgEl("polyline", { points: seg.join(" "), fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      seg = [];
    };
    trend.forEach((p, i) => (p[key] == null ? flush() : seg.push(`${x(i)},${y(p[key])}`)));
    flush();
    trend.forEach((p, i) => {
      if (p[key] != null) svg.append(svgEl("circle", { cx: x(i), cy: y(p[key]), r: 4, fill: color, stroke: "var(--chart-surface)", "stroke-width": 2 }));
    });
    const lastI = trend.map((p) => p[key]).findLastIndex((val) => val != null);
    if (lastI >= 0) lastLabels.push({ label, v: trend[lastI][key], i: lastI, color });
  }
  // 직접 라벨: 마지막 점 오른쪽, 겹치면 14px 간격으로 밀어냄. 글자는 텍스트 색, 계열색은 옆 짧은 선이 담당
  lastLabels.sort((a, b) => y(a.v) - y(b.v));
  let prevY = -Infinity;
  for (const lb of lastLabels) {
    const ly = Math.max(y(lb.v) + 4, prevY + 14);
    prevY = ly;
    svg.append(svgEl("line", { x1: x(lb.i) + 8, x2: x(lb.i) + 16, y1: ly - 4, y2: ly - 4, stroke: lb.color, "stroke-width": 2 }));
    const t = svgEl("text", { class: "viz-label", x: x(lb.i) + 20, y: ly });
    t.textContent = `${lb.label} ${lb.v}`;
    svg.append(t);
  }
  root.append(svg);

  // 호버: 세션 열마다 넓은 투명 영역 → 십자선 + 툴팁
  const tip = el("div", "viz-tip");
  root.append(tip);
  const colW = n === 1 ? pw : pw / (n - 1);
  trend.forEach((p, i) => {
    const hit = svgEl("rect", { x: x(i) - colW / 2, y: T, width: colW, height: ph, fill: "transparent" });
    hit.addEventListener("mouseenter", () => {
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      tip.innerHTML = "";
      tip.append(el("div", "", `#${i + 1} ${p.date || ""} · ${p.domain} · ${p.difficulty}`));
      for (const [key, label, v] of SERIES) {
        const r = el("div", "", `${label} ${p[key] ?? "-"}`);
        const sw = el("i");
        sw.style.background = `var(${v})`;
        r.prepend(sw);
        tip.append(r);
      }
      const rect = svg.getBoundingClientRect();
      const px = (x(i) / W) * rect.width;
      tip.style.display = "block";
      tip.style.top = "40px";
      tip.style.left = `${Math.min(px + 24, rect.width - 170)}px`;
    });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
    svg.append(hit);
  });
  box.append(root);
}

function renderSessionTable(rows) {
  const t = $("#dash-sessions");
  t.innerHTML = "<tr><th>날짜</th><th>제목</th><th>도메인</th><th>유형</th><th>난이도</th><th>기술</th><th>의도</th><th>인지</th><th>발견률</th><th>토큰</th><th>상태</th></tr>";
  for (const r of rows) {
    const tr = el("tr");
    const disc = r.discovery ? `${r.discovery.found}/${r.discovery.total}` : "-";
    [r.date || "-", r.title, r.domain, r.archetype, r.difficulty, r.technical ?? "-", r.intent ?? "-", r.cognitive ?? "-",
      disc, r.tokens != null ? r.tokens.toLocaleString() : "-", STATUS[r.status]].forEach((v) => tr.append(el("td", "", v)));
    tr.style.cursor = "pointer";
    tr.onclick = () => openSession(r.session_id);
    t.append(tr);
  }
}

function renderTypes(d) {
  $("#dash-types-note").textContent = `숨은 사실이 공개된 세션 ${d.revealed_sessions}개 기준 (공개 전 세션은 유형 구성이 봉인 정보라 제외)`;
  const t = $("#dash-types");
  t.innerHTML = "<tr><th>유형</th><th>발견 / 전체</th><th>발견률</th></tr>";
  for (const r of d.fact_types) {
    const tr = el("tr", r.rate != null && r.rate < 0.5 && r.total >= 2 ? "bad" : "");
    [r.label, `${r.found} / ${r.total}`, r.rate == null ? "-" : `${Math.round(r.rate * 100)}%`].forEach((v) => tr.append(el("td", "", v)));
    t.append(tr);
  }
  const tips = $("#dash-tips");
  tips.innerHTML = "";
  for (const tp of d.tips) tips.append(el("div", "tip-row", `${tp.label} 발견률 ${Math.round(tp.rate * 100)}% → ${tp.tip}`));
}

// ---------- 설정 ----------

$("#btn-settings").onclick = async () => {
  const s = await api("/api/settings");
  const f = $("#form-settings");
  f.codex_sessions_dir.value = s.codex_sessions_dir;
  f.claude_projects_dir.value = s.claude_projects_dir;
  $("#settings-status").textContent = "";
  show("settings");
};

$("#form-settings").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  await api("/api/settings", { method: "PUT", body: JSON.stringify({
    codex_sessions_dir: f.codex_sessions_dir.value, claude_projects_dir: f.claude_projects_dir.value }) });
  $("#settings-status").textContent = "저장했습니다.";
};

// ---------- 시작 ----------

loadOptions();
const m = location.hash.match(/s=([\w]+)/);
if (m) openSession(m[1]).catch(() => show("home"));
else if (location.hash === "#dashboard") openDashboard();
else loadSessions();
