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
  const facts = rv.facts.filter((f) => f.type !== "buried");
  const buried = rv.facts.filter((f) => f.type === "buried");
  const crit = facts.filter((f) => f.importance === "critical");
  const reflected = buried.filter((f) => (f.reflection || {}).verdict === "반영");
  b.append(el("p", "rh-question", `발견 ${facts.filter((f) => f.discovered).length}/${facts.length} · critical ${crit.filter((f) => f.discovered).length}/${crit.length}`
    + (buried.length ? ` · README에 묻힌 사실 반영 ${reflected.length}/${buried.length}` : "")));
  const tbl = el("table", "grid");
  tbl.innerHTML = "<tr><th>ID</th><th>내용</th><th>유형</th><th>중요도</th><th>발견·반영</th></tr>";
  for (const f of rv.facts) {
    const isBuried = f.type === "buried";
    const verdict = isBuried ? ((f.reflection || {}).verdict || "판정 없음") : null;
    const ok = isBuried ? verdict === "반영" : f.discovered;
    const tr = el("tr", ok ? "" : "bad");
    tr.append(el("td", "", f.id), el("td", "", f.statement), el("td", "", f.type_label), el("td", "", f.importance));
    tr.append(el("td", ok ? "found" : "notfound", isBuried ? `README 명시 · ${verdict}`
      : f.discovered ? `질문 #${f.question_no} (채팅 #${f.chat_turn})` : f.partial ? "부분 언급" : "놓침"));
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
  const buriedMiss = buried.filter((f) => (f.reflection || {}).verdict !== "반영");
  if (buriedMiss.length) {
    b.append(el("h2", "", "README에 있었지만 반영하지 못한 것"));
    for (const f of buriedMiss) {
      const c = el("div", "item");
      c.append(el("b", "", `${f.id} · ${f.statement}`));
      const r = f.reflection || {};
      c.append(kvList([
        ["README 원문", f.readme_quote || "-"],
        ["판정", r.verdict || "판정 없음 (평가 전 생성된 세션)"],
        ["근거", (r.evidence || []).join(", ") || "-"],
        ["설명", r.note || "-"],
      ]));
      b.append(c);
    }
  }
  if ((rv.concepts || []).length) {
    b.append(el("h2", "", "설계 판단 복습 (순진한 구현 → 결함 → 개선)"));
    for (const k of rv.concepts) {
      const c = el("div", "item");
      const chk = k.check || {};
      c.append(el("b", "", `[${k.role_label}] ${k.name}` + (k.role === "qa_only" ? "" : ` · ${chk.verdict || "판정 없음"}`)));
      c.append(kvList([
        ["순진한 구현의 결함", k.naive_failure],
        ["2시간 간소화 허용선", k.simplification || "-"],
        ["개선", k.plan],
        ["연결된 사실", (k.fact_ids || []).join(", ") || "-"],
        ["내 결과물", chk.note ? `${chk.note}${(chk.evidence || []).length ? " (" + chk.evidence.join(", ") + ")" : ""}` : "-"],
        ["서술형 질문 씨앗", (k.qa_seeds || []).join(" / ") || "-"],
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

// ---------- 동작 추적 카드 드릴 (docs/drill_design.md) ----------

const dc = { run: null, n: null, deadline: null, tick: null, sending: false };

$("#btn-drill").onclick = openDrillSetup;
$("#btn-ds-setup").onclick = openDrillSetup;

async function openDrillSetup() {
  location.hash = "drill";
  clearInterval(dc.tick);
  show("drill-setup");
  const d = await api("/api/drills/sources");
  const tb = $("#drill-sources");
  tb.innerHTML = "";
  const sel = $("#form-run").session_id;
  sel.length = 1;
  for (const s of d.sources) {
    const tr = el("tr");
    const name = el("td", "", s.label);
    if (s.path) {
      name.append(el("br"), el("code", "", s.path));
    }
    const runnable = s.runnable ? "가능" : s.kind === "fresh" ? "불가 (저장소 없음)" : "불가 (Python 아님)";
    tr.append(name, el("td", "", runnable), el("td", "", `${s.cards}장`));
    const td = el("td");
    if (s.runnable) {
      const b = el("button", "", s.cards ? "카드 더 만들기" : "카드 만들기");
      b.onclick = () => generateDeck(s, b);
      td.append(b);
    }
    tr.append(td);
    tb.append(tr);
    if (s.cards) sel.add(new Option(`${s.label} — ${s.cards}장`, s.key));
  }
  const rb = $("#drill-runs");
  rb.innerHTML = "";
  for (const r of d.runs) {
    const tr = el("tr");
    const pct = (v) => (v == null ? "-" : `${v}%`);
    tr.append(el("td", "", r.run_id), el("td", "", r.mode === "open" ? "코드 보고" : "코드 닫고"),
      el("td", "", `${r.done} / ${r.total}`), el("td", "", pct(r.card_rate)), el("td", "", pct(r.slot_rate)));
    const td = el("td");
    const b = el("button", "", r.status === "done" ? "보기" : "이어하기");
    b.onclick = () => openRun(r.run_id);
    td.append(b);
    tr.append(td);
    rb.append(tr);
  }
}

async function generateDeck(s, btn) {
  btn.disabled = true;
  try {
    const r = await withProgress($("#drill-gen-status"), `${s.label}: 카드 출제 → 실행 검증 중… (보통 2~5분)`,
      post("/api/drills/decks", s.kind === "fresh" ? { repo_id: s.key } : { session_id: s.session_id }));
    const g = r.generation;
    await openDrillSetup();
    $("#drill-gen-status").textContent = `카드 ${g.requested}장 출제 → 실행 검증 통과 ${g.verified}장 (폐기 ${g.dropped.length}장)`;
  } catch (err) {
    $("#drill-gen-status").textContent = `생성 실패: ${err.message}`;
    btn.disabled = false;
  }
}

$("#btn-fresh").onclick = async () => {
  const btn = $("#btn-fresh");
  btn.disabled = true;
  try {
    const r = await withProgress($("#drill-fresh-status"), "저장소 생성 → 테스트 → 카드 출제 → 실행 검증 중…",
      post("/api/drills/fresh", {}));
    const g = r.generation;
    await openDrillSetup();
    $("#drill-fresh-status").textContent =
      `"${r.title}" 저장소: ${r.root} — 카드 ${g.requested}장 출제 → 검증 통과 ${g.verified}장. 저장소를 15분 읽은 뒤 위 재료를 골라 드릴을 시작하세요.`;
    $("#form-run").session_id.value = r.repo_id;
  } catch (err) {
    $("#drill-fresh-status").textContent = `생성 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

$("#form-run").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await post("/api/drills/runs", {
      mode: f.mode.value, count: Number(f.count.value), session_id: f.session_id.value || null });
    await openRun(r.run_id);
  } catch (err) {
    $("#drill-run-status").textContent = err.message;
  }
};

async function openRun(runId) {
  const r = await api(`/api/drills/runs/${runId}`);
  dc.run = r;
  location.hash = `run=${runId}`;
  if (r.current === null) return showRunSummary(r);
  const q = await post(`/api/drills/runs/${runId}/cards/${r.current}/start`, {});
  showCard(q);
}

function codeBlock(file, lines, startLabel) {
  const wrap = el("div", "snip");
  wrap.append(el("div", "snip-h", startLabel || file));
  const pre = el("pre");
  for (const l of lines) {
    const line = el("span", `ln${l.hit ? " hit" : ""}`);
    line.append(el("i", "", String(l.n)), document.createTextNode(l.text));
    pre.append(line);
  }
  wrap.append(pre);
  return wrap;
}

function showCard(q) {
  dc.n = q.n;
  const open = dc.run.mode === "open";
  $("#dc-progress").textContent = `카드 ${q.n} / ${q.total}`;
  $("#dc-source").textContent = `재료: ${q.source_label} · ${open ? "코드 보고" : "코드 닫고"}`;
  $("#dc-context").textContent = q.context ? `기능 흐름: ${q.context}` : "";
  $("#dc-scenario").textContent = `상황: ${q.scenario}`;
  $("#dc-layout").classList.toggle("open", open);
  const code = $("#dc-code");
  code.innerHTML = "";
  code.classList.toggle("hidden", !open);
  if (open) q.code.forEach((f) => code.append(codeBlock(f.file, f.lines)));
  const form = $("#form-card");
  form.innerHTML = "";
  form.classList.remove("hidden");
  for (const s of q.slots) {
    const box = el("div", "slot");
    box.append(el("p", "", s.prompt));
    if (s.kind === "choice") {
      for (const o of s.options) {
        const lab = el("label");
        const inp = el("input");
        inp.type = "radio"; inp.name = s.id; inp.value = o;
        lab.append(inp, o);
        box.append(lab);
      }
    } else {
      const inp = el("input");
      inp.name = s.id; inp.autocomplete = "off"; inp.placeholder = "값 (숫자·상태 코드·짧은 토큰)";
      box.append(inp);
    }
    form.append(box);
  }
  const row = el("div", "row");
  const b = el("button", "primary", "제출");
  b.type = "submit";
  row.append(b);
  form.append(row);
  $("#dc-status").textContent = "";
  $("#dc-result").innerHTML = "";
  clearInterval(dc.tick);
  dc.deadline = q.remaining_sec == null ? null : Date.now() + q.remaining_sec * 1000;
  renderCardTimer();
  dc.tick = setInterval(renderCardTimer, 500);
  show("drill-card");
}

function renderCardTimer() {
  if (dc.deadline === null) { $("#dc-timer").textContent = ""; return; }
  const left = Math.max(0, Math.ceil((dc.deadline - Date.now()) / 1000));
  $("#dc-timer").textContent = `${Math.floor(left / 60)}:${pad(left % 60)}`;
  $("#dc-timer").classList.toggle("low", left <= 15 && left > 0);
  $("#dc-timer").classList.toggle("over", left === 0);
  if (left === 0) submitCard(true);
}

$("#form-card").onsubmit = (e) => { e.preventDefault(); submitCard(false); };

async function submitCard(timedOut) {
  if (dc.sending || dc.n === null) return;
  dc.sending = true;
  clearInterval(dc.tick);
  const form = $("#form-card");
  const answers = {};
  for (const s of form.querySelectorAll(".slot")) {
    const checked = s.querySelector("input[type=radio]:checked");
    const text = s.querySelector("input:not([type=radio])");
    const name = (checked || text || s.querySelector("input")).name;
    answers[name] = checked ? checked.value : text ? text.value : "";
  }
  try {
    const r = await post(`/api/drills/runs/${dc.run.run_id}/cards/${dc.n}/answer`, { answers, timed_out: timedOut });
    showCardResult(r);
  } catch (err) {
    $("#dc-status").textContent = `제출 실패: ${err.message}`;
  } finally {
    dc.sending = false;
  }
}

function showCardResult(r) {
  clearInterval(dc.tick);
  dc.n = null;
  $("#dc-timer").textContent = r.timed_out ? "시간 초과" : `${r.elapsed_sec}초`;
  $("#dc-progress").textContent = `카드 ${r.n} / ${r.total} — ${r.all_correct ? "맞음" : "틀림"}`;
  $("#dc-context").textContent = r.context ? `기능 흐름: ${r.context}` : "";
  $("#dc-scenario").textContent = `상황: ${r.scenario}`;
  $("#form-card").classList.add("hidden");
  const box = $("#dc-result");
  box.innerHTML = "";
  for (const s of r.slots) {
    const d = el("div", `slot ${s.correct ? "ok" : "bad"}`);
    d.append(el("p", "", `${s.correct ? "✓" : "✗"} ${s.prompt}`));
    d.append(el("div", "ans", `내 답: ${s.mine || "(빈 답)"}`), el("div", "ans", `실행 결과: ${s.answer}`));
    box.append(d);
  }
  if (r.surprise) box.append(el("p", "surprise", "출제자(AI)도 예상을 틀렸던 카드입니다. 실행 결과가 직관과 다릅니다."));
  box.append(el("h3", "", "왜 그렇게 동작하나"), el("p", "", r.explanation));
  if (r.defect_tags.length) box.append(el("p", "ev", `패턴: ${r.defect_tags.join(", ")}`));
  r.snippets.forEach((sn) => box.append(codeBlock(sn.file, sn.lines, `${sn.file}:${sn.start}-${sn.end}`)));
  const det = el("details");
  det.append(el("summary", "", "검증에 쓴 probe 코드"), el("pre", "", r.probe));
  box.append(det);
  const row = el("div", "row");
  const flag = el("button", "ghost", r.flagged ? "신고 취소" : "정답이 이상함");
  flag.onclick = async () => {
    const { flagged } = await post(`/api/drills/runs/${dc.run.run_id}/cards/${r.n}/flag`, {});
    flag.textContent = flagged ? "신고 취소" : "정답이 이상함";
    toast(flagged ? "이 카드는 통계와 다음 드릴에서 빠집니다." : "신고를 취소했습니다.");
  };
  const next = el("button", "primary", r.run_done ? "요약 보기" : "다음 카드");
  next.onclick = () => openRun(dc.run.run_id);
  row.append(flag, next);
  box.append(row);
  next.focus();
}

function showRunSummary(r) {
  location.hash = `run=${r.run_id}`;
  const s = r.summary;
  const body = $("#ds-body");
  body.innerHTML = "";
  const row = el("div", "score-row");
  const tile = (label, value, sub) => {
    const t = el("div", "score");
    t.append(el("span", "muted", label), el("b", "", value ?? "-"), el("small", "muted", sub || ""));
    return t;
  };
  const pct = (v) => (v == null ? "-" : `${v}%`);
  row.append(tile("카드 정답률", pct(s.card_rate), `${s.cards_correct} / ${s.cards}장 모두 맞음`),
    tile("칸 정답률", pct(s.slot_rate), ""), tile("평균 시간", s.avg_sec == null ? "-" : `${s.avg_sec}초`, `시간 초과 ${s.timed_out}장`));
  body.append(row);
  const tags = Object.entries(s.missed_tags);
  if (tags.length) body.append(el("p", "", "틀린 카드의 패턴: " + tags.map(([k, v]) => `${k} ${v}`).join(" · ")));
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>카드</th><th>재료</th><th>결과</th><th></th></tr></thead>";
  const tb = el("tbody");
  for (const it of r.items) {
    const tr = el("tr");
    tr.append(el("td", "", String(it.n)), el("td", "", it.source_label),
      el("td", "", (it.all_correct ? "맞음" : "틀림") + (it.flagged ? " (신고)" : "")));
    const td = el("td");
    const b = el("button", "", "다시 보기");
    b.onclick = async () => { showCard0(); showCardResult(await api(`/api/drills/runs/${r.run_id}/cards/${it.n}/result`)); };
    td.append(b);
    tr.append(td);
    tb.append(tr);
  }
  t.append(tb);
  body.append(t);
  show("drill-summary");
}

// 결과 다시 보기용: 카드 화면을 비운 상태로 연다
function showCard0() {
  $("#dc-layout").classList.remove("open");
  $("#dc-code").classList.add("hidden");
  $("#dc-progress").textContent = "카드 다시 보기";
  $("#dc-source").textContent = "";
  $("#dc-scenario").textContent = "";
  $("#dc-context").textContent = "";
  $("#dc-status").textContent = "";
  show("drill-card");
}

// ---------- 성공 기준 드릴 (docs/flow_drills_design.md 4절 B) ----------

const cs = { sheet: null, rows: [], deadline: null, tick: null, sending: false, saveTimer: null };
const pctText = (v) => (v == null ? "-" : `${v}%`);
const TYPE_HINT = { int: "정수", number: "숫자", str: "글자", bool: "참/거짓", date: "날짜", time: "시각", datetime: "날짜·시각", choice: "선택", list: "쉼표로 구분" };

$("#btn-criteria").onclick = openCriteria;
$("#btn-cr-setup").onclick = openCriteria;

function emptyRow(cols, text) {
  const tr = el("tr");
  const td = el("td", "muted", text);
  td.colSpan = cols;
  tr.append(td);
  return tr;
}

async function openCriteria() {
  location.hash = "criteria";
  clearInterval(cs.tick);
  show("crit-setup");
  const d = await api("/api/criteria");
  const tb = $("#crit-bundles");
  tb.innerHTML = "";
  for (const b of d.bundles) {
    const tr = el("tr");
    const name = el("td", "", b.title);
    name.append(el("br"), el("small", "muted", `${b.domain} · ${b.concepts.join(", ")} · ${b.doc_chars.toLocaleString()}자`));
    tr.append(name, el("td", "", `정책 ${b.policies} · 결함 ${b.defects}`), el("td", "", `${b.sheets}회${b.seen ? " · 이미 봄" : ""}`),
      el("td", "", pctText(b.best_coverage)));
    const td = el("td");
    const btn = el("button", "primary", "시작");
    btn.onclick = () => startSheet(b.bundle_id, btn);
    td.append(btn);
    tr.append(td);
    tb.append(tr);
  }
  if (!d.bundles.length) tb.append(emptyRow(5, "아직 정책 묶음이 없습니다. 위에서 새로 만드세요."));
  const sb = $("#crit-sheets");
  sb.innerHTML = "";
  for (const s of d.sheets) {
    const tr = el("tr");
    tr.append(el("td", "", s.sheet_id), el("td", "", s.title), el("td", "", `${s.rows}개`),
      el("td", "", pctText(s.accuracy)), el("td", "", pctText(s.coverage)));
    const td = el("td");
    const btn = el("button", "", s.status === "done" ? "보기" : "이어하기");
    btn.onclick = () => openSheet(s.sheet_id);
    td.append(btn);
    tr.append(td);
    sb.append(tr);
  }
  if (!d.sheets.length) sb.append(emptyRow(6, "아직 없습니다."));
}

$("#btn-bundle").onclick = async () => {
  const btn = $("#btn-bundle");
  btn.disabled = true;
  try {
    const b = await withProgress($("#crit-bundle-status"), "명세 문서·정책·정답 구현·결함 출제 → 실행 검증 중…",
      post("/api/criteria/bundles", {}));
    await openCriteria();
    $("#crit-bundle-status").textContent = `"${b.title}" — 정책 ${b.policies}개, 결함 ${b.defects}개, 문서 ${b.doc_chars.toLocaleString()}자. 시작을 눌러 푸세요.`;
  } catch (err) {
    $("#crit-bundle-status").textContent = `생성 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

async function startSheet(bundleId, btn) {
  btn.disabled = true;
  try {
    renderSheet(await post("/api/criteria/sheets", { bundle_id: bundleId, seconds: Number($("#crit-seconds").value) }));
  } catch (err) {
    $("#crit-start-status").textContent = err.message;
    btn.disabled = false;
  }
}

async function openSheet(id) {
  const v = await api(`/api/criteria/sheets/${id}`);
  if (v.status === "done") showCritResult(v);
  else renderSheet(v);
}

function inputFor(type, options, name) {
  let inp;
  if (type === "choice" || type === "bool") {
    inp = el("select");
    inp.add(new Option("(선택)", ""));
    for (const o of type === "bool" ? ["true", "false"] : options) inp.add(new Option(o, o));
  } else {
    inp = el("input");
    inp.autocomplete = "off";
    inp.type = { int: "number", number: "number", date: "date", time: "time", datetime: "datetime-local" }[type] || "text";
    if (type === "number") inp.step = "any";
    if (type === "list") inp.placeholder = "쉼표로 구분 (예: 김하나, 이두리). 없으면 비움";
  }
  inp.name = name;
  return inp;
}

function renderSheet(v) {
  cs.sheet = v;
  cs.rows = v.rows.slice();
  location.hash = `sheet=${v.sheet_id}`;
  $("#cs-title").textContent = v.title;
  $("#cs-doc").textContent = v.spec_doc;
  $("#cs-doc").scrollTop = 0;
  const box = $("#cs-funcs");
  box.innerHTML = "";
  for (const f of v.functions) {
    const fb = el("div", "fn-box");
    const h = el("h3", "", f.summary);
    h.append(" ", el("code", "muted", f.name));
    fb.append(h);
    const form = el("form");
    for (const p of f.params) {
      const lab = el("label", "", `${p.label} (${TYPE_HINT[p.type]})`);
      lab.append(inputFor(p.type, p.options, `a_${p.name}`));
      form.append(lab);
    }
    const out = el("label", "", `→ 기대 ${f.returns.label} (${TYPE_HINT[f.returns.type]})`);
    out.append(inputFor(f.returns.type, f.returns.options, "_expected"));
    form.append(out);
    const note = el("label", "wide2", "메모 (어느 정책·경계를 확인하나, 선택)");
    const ni = el("input");
    ni.name = "_note";
    note.append(ni);
    form.append(note);
    const row = el("div", "row wide2");
    const add = el("button", "primary", "예시 추가");
    add.type = "submit";
    row.append(add);
    form.append(row);
    form.onsubmit = (e) => {
      e.preventDefault();
      const args = {};
      for (const p of f.params) args[p.name] = form[`a_${p.name}`].value;
      const missing = f.params.filter((p) => !["str", "list"].includes(p.type) && !args[p.name]).map((p) => p.label);
      if (!form._expected.value) missing.push("기대 출력");
      if (missing.length) {
        $("#cs-status").textContent = `${f.summary}: ${missing.join(", ")}을(를) 채우세요.`;
        return;
      }
      cs.rows.push({ func: f.name, args, expected: form._expected.value, note: form._note.value });
      form._note.value = "";
      renderRows();
      scheduleSave();
    };
    fb.append(form);
    box.append(fb);
  }
  renderRows();
  $("#cs-status").textContent = "";
  clearInterval(cs.tick);
  cs.deadline = v.remaining_sec == null ? null : Date.now() + v.remaining_sec * 1000;
  renderSheetTimer();
  cs.tick = setInterval(renderSheetTimer, 500);
  show("crit-sheet");
}

const showVal = (x) => String(x).replace("T", " ");

function renderRows() {
  const fns = Object.fromEntries(cs.sheet.functions.map((f) => [f.name, f]));
  const tb = $("#cs-rows");
  tb.innerHTML = "";
  cs.rows.forEach((r, i) => {
    const f = fns[r.func];
    const tr = el("tr");
    const call = f.params.map((p) => `${p.label} ${showVal(r.args[p.name] ?? "") || "(없음)"}`).join(", ");
    const td = el("td");
    const del = el("button", "ghost", "✕");
    del.title = "삭제";
    del.onclick = () => { cs.rows.splice(i, 1); renderRows(); scheduleSave(); };
    td.append(del);
    tr.append(el("td", "", String(i + 1)), el("td", "", f.summary), el("td", "", call), el("td", "", showVal(r.expected)),
      el("td", "muted", r.note), td);
    tb.append(tr);
  });
  if (!cs.rows.length) tb.append(emptyRow(6, "아직 예시가 없습니다."));
  $("#cs-count").textContent = `${cs.rows.length}개`;
}

function scheduleSave() {
  clearTimeout(cs.saveTimer);
  cs.saveTimer = setTimeout(async () => {
    try {
      await api(`/api/criteria/sheets/${cs.sheet.sheet_id}/rows`, { method: "PUT", body: JSON.stringify({ rows: cs.rows }) });
      $("#cs-status").textContent = `저장됨 ${fmtTime(Date.now())}`;
    } catch (err) {
      $("#cs-status").textContent = `저장 실패: ${err.message}`;
    }
  }, 500);
}

function renderSheetTimer() {
  if (cs.deadline === null) { $("#cs-timer").textContent = "제한 없음"; return; }
  const left = Math.max(0, Math.ceil((cs.deadline - Date.now()) / 1000));
  $("#cs-timer").textContent = `${Math.floor(left / 60)}:${pad(left % 60)}`;
  $("#cs-timer").classList.toggle("low", left <= 120 && left > 0);
  $("#cs-timer").classList.toggle("over", left === 0);
  if (left === 0) submitSheet(true);
}

$("#btn-cs-submit").onclick = () => submitSheet(false);
$("#btn-cs-leave").onclick = async () => {
  clearTimeout(cs.saveTimer);
  clearInterval(cs.tick);
  await api(`/api/criteria/sheets/${cs.sheet.sheet_id}/rows`, { method: "PUT", body: JSON.stringify({ rows: cs.rows }) }).catch(() => {});
  openCriteria();
};

async function submitSheet(timedOut) {
  if (cs.sending) return;
  if (!timedOut && !cs.rows.length) { $("#cs-status").textContent = "예시를 하나 이상 추가하세요."; return; }
  cs.sending = true;
  clearInterval(cs.tick);
  clearTimeout(cs.saveTimer);
  try {
    const v = await withProgress($("#cs-status"), "정답 구현과 결함 버전에 예시를 실행하는 중…",
      post(`/api/criteria/sheets/${cs.sheet.sheet_id}/submit`, { rows: cs.rows, timed_out: timedOut }));
    showCritResult(v);
  } catch (err) {
    $("#cs-status").textContent = `제출 실패: ${err.message}`;
  } finally {
    cs.sending = false;
  }
}

function policyBox(d, cls) {
  const b = el("div", `pol ${cls}`);
  b.append(el("p", "", `${d.policy_title} (${d.kind_label}) — ${d.location}`));
  b.append(el("blockquote", "", d.doc_quote));
  b.append(el("p", "muted", `정책: ${d.statement}`));
  return b;
}

function showCritResult(v) {
  location.hash = `sheet=${v.sheet_id}`;
  clearInterval(cs.tick);
  const r = v.result;
  const s = r.summary;
  $("#cr-title").textContent = `채점 결과 — ${v.title}`;
  const body = $("#cr-body");
  body.innerHTML = "";
  const row = el("div", "score-row");
  const tile = (label, value, sub) => {
    const t = el("div", "score");
    t.append(el("span", "muted", label), el("b", "", value ?? "-"), el("small", "muted", sub || ""));
    return t;
  };
  const mins = v.elapsed_sec == null ? "-" : `${Math.floor(v.elapsed_sec / 60)}분 ${v.elapsed_sec % 60}초`;
  row.append(
    tile("정책 이해 정확도", pctText(s.accuracy), `맞은 예시 ${s.correct} / ${s.valid}`),
    tile("결함 검출률", pctText(s.coverage), `잡은 결함 ${s.killed} / ${s.defects}`),
    tile("예시", `${s.rows}개`, `형식·실행 오류 ${s.invalid} · 아무 결함도 못 잡은 맞는 예시 ${s.idle}`),
    tile("시간", mins, v.timed_out ? "시간 초과로 자동 제출" : ""));
  body.append(row);
  body.append(el("p", "muted", "정확도: 내 기대값이 정답 구현의 실제 출력과 같은 비율. 검출률: 맞는 예시 중 하나라도 결함 버전에서 다른 값이 나오면 그 결함을 잡은 것."));

  body.append(el("h2", "", "내 예시"));
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>#</th><th>입력</th><th>내 기대</th><th>정답 구현</th><th>잡은 결함</th></tr></thead>";
  const tb = el("tbody");
  for (const g of r.rows) {
    const cls = { correct: "ok", wrong: "bad" }[g.verdict] || "err";
    const tr = el("tr", cls);
    const actual = g.verdict === "correct" || g.verdict === "wrong" ? g.actual : g.verdict === "error" ? "실행 오류" : "형식 오류";
    tr.append(el("td", "", String(g.n)), el("td", "", g.call), el("td", "", showVal(g.expected)), el("td", "", actual),
      el("td", "", g.kills.join(", ") || (g.verdict === "correct" ? "없음" : "-")));
    tb.append(tr);
    const notes = [];
    if (g.error) notes.push(g.error);
    for (const d of g.diagnosis) notes.push(`내 기대값은 "${d.defect_summary}" 구현의 출력과 같습니다 → 오해한 정책: ${d.policy_title} (${d.location})`);
    if (g.verdict === "correct" && !g.kills.length) notes.push("맞지만 어떤 결함도 잡지 못한 평범한 경우입니다. 경계 쪽으로 옮겨 보세요.");
    if (notes.length) {
      const nr = el("tr", cls);
      const td = el("td", "muted", notes.join(" / "));
      td.colSpan = 5;
      nr.append(td);
      tb.append(nr);
    }
  }
  t.append(tb);
  body.append(t);

  const missed = r.defects.filter((d) => !d.killed_by.length);
  body.append(el("h2", "", `놓친 결함 ${missed.length}개`));
  if (!missed.length) body.append(el("p", "", "모든 결함 버전을 잡았습니다."));
  for (const d of missed) {
    const b = policyBox(d, "missed");
    b.prepend(el("p", "", `✗ ${d.id} ${d.defect_summary}`));
    for (const ex of d.model_examples) b.append(el("p", "ev", `이 예시라면 잡았음: ${ex.call} → ${ex.expected}${ex.note ? ` (${ex.note})` : ""}`));
    body.append(b);
  }
  const caught = r.defects.filter((d) => d.killed_by.length);
  if (caught.length) {
    body.append(el("h2", "", `잡은 결함 ${caught.length}개`));
    for (const d of caught) {
      const b = policyBox(d, "caught");
      b.prepend(el("p", "", `✓ ${d.id} ${d.defect_summary} — 예시 #${d.killed_by.join(", #")}`));
      body.append(b);
    }
  }
  const all = el("details");
  all.append(el("summary", "", `정책 목록 전체 (${r.policies.length}개, 복습용)`));
  for (const p of r.policies) {
    const b = el("div", "pol");
    b.append(el("p", "", `[${p.kind_label}] ${p.title} — ${p.location}${p.has_defect ? "" : " (결함 버전 없음)"}`),
      el("blockquote", "", p.doc_quote), el("p", "muted", p.statement));
    all.append(b);
  }
  body.append(all);
  const doc = el("details");
  const dd = el("div", "readme", v.spec_doc);
  doc.append(el("summary", "", "명세 문서 다시 보기"), dd);
  body.append(doc);
  show("crit-result");
}

// ---------- 읽기·정리 드릴 (docs/flow_drills_design.md 4절 A) ----------

const rs = { sheet: null, deadline: null, tick: null, sending: false, saveTimer: null };

$("#btn-reading").onclick = openReading;
$("#btn-rr-setup").onclick = openReading;

// 서버 reading.parse_items와 같은 규칙: 한 줄 = 한 항목, '#' 줄은 제목
const countItems = (text) => text.split("\n").map((l) => l.trim())
  .filter((l) => l && !l.startsWith("#") && l.replace(/^(?:[-*•·]|\d+[.)]|\[[ xX]?\])\s*/, "")).length;

async function openReading() {
  location.hash = "reading";
  clearInterval(rs.tick);
  show("read-setup");
  const d = await api("/api/reading");
  const tb = $("#read-bundles");
  tb.innerHTML = "";
  for (const b of d.bundles) {
    const tr = el("tr");
    const name = el("td", "", b.title);
    name.append(el("br"), el("small", "muted", `${b.domain} · ${b.concepts.join(", ")} · ${b.doc_chars.toLocaleString()}자`));
    tr.append(name, el("td", "", `${b.policies}개`), el("td", "", `${b.reads}회${b.seen ? " · 이미 봄" : ""}`),
      el("td", "", pctText(b.best_recall)));
    const td = el("td");
    const btn = el("button", b.seen ? "" : "primary", "시작");
    btn.onclick = () => startRead(b.bundle_id, btn);
    td.append(btn);
    tr.append(td);
    tb.append(tr);
  }
  if (!d.bundles.length) tb.append(emptyRow(5, "아직 정책 묶음이 없습니다. 위에서 새로 만드세요."));
  const sb = $("#read-sheets");
  sb.innerHTML = "";
  for (const s of d.reads) {
    const tr = el("tr");
    tr.append(el("td", "", s.read_id), el("td", "", s.title), el("td", "", `${s.items}개`), el("td", "", pctText(s.recall)),
      el("td", "", s.misread ?? "-"), el("td", "", s.invented ?? "-"));
    const td = el("td");
    const btn = el("button", "", s.status === "done" ? "보기" : "이어하기");
    btn.onclick = () => openRead(s.read_id);
    td.append(btn);
    tr.append(td);
    sb.append(tr);
  }
  if (!d.reads.length) sb.append(emptyRow(7, "아직 없습니다."));
}

$("#btn-read-bundle").onclick = async () => {
  const btn = $("#btn-read-bundle");
  btn.disabled = true;
  try {
    const b = await withProgress($("#read-bundle-status"), "명세 문서·정책·정답 구현·결함 출제 → 실행 검증 중…",
      post("/api/criteria/bundles", {}));
    await openReading();
    $("#read-bundle-status").textContent = `"${b.title}" — 정책 ${b.policies}개, 문서 ${b.doc_chars.toLocaleString()}자. 시작을 눌러 푸세요.`;
  } catch (err) {
    $("#read-bundle-status").textContent = `생성 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
};

async function startRead(bundleId, btn) {
  btn.disabled = true;
  try {
    renderRead(await post("/api/reading/sheets", { bundle_id: bundleId, seconds: Number($("#read-seconds").value) }));
  } catch (err) {
    $("#read-start-status").textContent = err.message;
    btn.disabled = false;
  }
}

async function openRead(id) {
  const v = await api(`/api/reading/sheets/${id}`);
  if (v.status === "done") showReadResult(v);
  else renderRead(v);
}

function renderRead(v) {
  rs.sheet = v;
  location.hash = `read=${v.read_id}`;
  $("#rs-title").textContent = v.title;
  $("#rs-doc").textContent = v.spec_doc;
  $("#rs-doc").scrollTop = 0;
  $("#rs-text").value = v.text;
  $("#rs-text").disabled = false;
  $("#rs-count").textContent = `항목 ${countItems(v.text)}개`;
  $("#rs-status").textContent = "";
  clearInterval(rs.tick);
  rs.deadline = v.remaining_sec == null ? null : Date.now() + v.remaining_sec * 1000;
  renderReadTimer();
  rs.tick = setInterval(renderReadTimer, 500);
  show("read-sheet");
}

$("#rs-text").oninput = () => {
  $("#rs-count").textContent = `항목 ${countItems($("#rs-text").value)}개`;
  clearTimeout(rs.saveTimer);
  rs.saveTimer = setTimeout(saveReadText, 800);
};

async function saveReadText() {
  try {
    await api(`/api/reading/sheets/${rs.sheet.read_id}/text`, { method: "PUT", body: JSON.stringify({ text: $("#rs-text").value }) });
    $("#rs-status").textContent = `저장됨 ${fmtTime(Date.now())}`;
  } catch (err) {
    $("#rs-status").textContent = `저장 실패: ${err.message}`;
  }
}

function renderReadTimer() {
  if (rs.deadline === null) { $("#rs-timer").textContent = "제한 없음"; return; }
  const left = Math.max(0, Math.ceil((rs.deadline - Date.now()) / 1000));
  $("#rs-timer").textContent = `${Math.floor(left / 60)}:${pad(left % 60)}`;
  $("#rs-timer").classList.toggle("low", left <= 120 && left > 0);
  $("#rs-timer").classList.toggle("over", left === 0);
  if (left === 0) submitRead(true);
}

$("#btn-rs-submit").onclick = () => submitRead(false);
$("#btn-rs-leave").onclick = async () => {
  clearTimeout(rs.saveTimer);
  clearInterval(rs.tick);
  await saveReadText().catch(() => {});
  openReading();
};

async function submitRead(timedOut) {
  if (rs.sending) return;
  const text = $("#rs-text").value;
  if (!timedOut && !countItems(text)) { $("#rs-status").textContent = "요구사항을 한 줄 이상 적으세요."; return; }
  rs.sending = true;
  clearInterval(rs.tick);
  clearTimeout(rs.saveTimer);
  $("#rs-text").disabled = true;
  try {
    const v = await withProgress($("#rs-status"), "정책 목록과 대조해 채점하는 중…",
      post(`/api/reading/sheets/${rs.sheet.read_id}/submit`, { text, timed_out: timedOut }));
    showReadResult(v);
  } catch (err) {
    // 채점 실패: 답안은 저장됨. 다시 제출할 수 있게 둔다 (시간은 더 흐르지 않음)
    $("#rs-status").textContent = `제출 실패: ${err.message}`;
    $("#rs-timer").textContent = "제출 대기";
  } finally {
    rs.sending = false;
  }
}

function showReadResult(v) {
  location.hash = `read=${v.read_id}`;
  clearInterval(rs.tick);
  const r = v.result;
  const s = r.summary;
  $("#rr-title").textContent = `채점 결과 — ${v.title}`;
  const body = $("#rr-body");
  body.innerHTML = "";
  const row = el("div", "score-row");
  const tile = (label, value, sub) => {
    const t = el("div", "score");
    t.append(el("span", "muted", label), el("b", "", value ?? "-"), el("small", "muted", sub || ""));
    return t;
  };
  const mins = v.elapsed_sec == null ? "-" : `${Math.floor(v.elapsed_sec / 60)}분 ${v.elapsed_sec % 60}초`;
  row.append(
    tile("정책 포착률", pctText(s.recall), `반영 ${s.covered} · 부분 ${s.partial} / ${s.policies}`),
    tile("잘못 해석", `${s.misread}개`, `누락 ${s.missed}개`),
    tile("근거 없음·어긋남", `${s.unsupported + s.contradicts}개`, `내 항목 ${s.items}개 중 · 채점 외 ${s.extra}개`),
    tile("시간", mins, v.timed_out ? "시간 초과로 자동 제출" : ""));
  body.append(row);
  body.append(el("p", "muted", "포착률 = (반영 + 부분 반영 × 0.5) / 정책 수. 정책 목록에 없지만 문서에 근거가 있는 항목은 감점하지 않습니다."));
  body.append(el("p", "", "종류별 반영: " + s.by_kind.map((k) => `${k.label} ${k.covered}/${k.total}`).join(" · ")));
  if (r.overall) body.append(el("p", "context", r.overall));

  const order = { missed: 0, misread: 1, partial: 2, covered: 3 };
  const pols = r.policies.slice().sort((a, b) => order[a.verdict] - order[b.verdict]);
  const bad = pols.filter((p) => p.verdict !== "covered");
  body.append(el("h2", "", `놓치거나 어긋난 정책 ${bad.length}개`));
  if (!bad.length) body.append(el("p", "", "모든 정책을 정확히 옮겼습니다."));
  const polCard = (p) => {
    const b = el("div", `pol ${p.verdict === "covered" ? "caught" : p.verdict}`);
    b.append(el("p", "", `${{ covered: "✓", partial: "△", misread: "✗", missed: "✗" }[p.verdict]} ${p.verdict_label} — ${p.title} (${p.kind_label}) — ${p.location}`));
    b.append(el("blockquote", "", p.doc_quote));
    b.append(el("p", "muted", `정책: ${p.statement}`));
    for (const it of p.items) b.append(el("p", "mine", `내 항목 #${it.n}: ${it.text}`));
    if (p.comment) b.append(el("p", "", p.comment));
    if (p.if_missed && p.verdict !== "covered") b.append(el("p", "ev", `이 정책을 놓친 구현에서 생기는 일: ${p.if_missed}`));
    return b;
  };
  for (const p of bad) body.append(polCard(p));
  const good = pols.filter((p) => p.verdict === "covered");
  if (good.length) {
    const det = el("details");
    det.append(el("summary", "", `정확히 옮긴 정책 ${good.length}개`));
    for (const p of good) det.append(polCard(p));
    body.append(det);
  }

  body.append(el("h2", "", `내 항목 ${r.items.length}개`));
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>#</th><th>항목</th><th>판정</th><th>설명</th></tr></thead>";
  const tb = el("tbody");
  for (const it of r.items) {
    const cls = { matched: "ok", extra: "", unsupported: "bad", contradicts: "bad" }[it.verdict];
    const tr = el("tr", cls);
    tr.append(el("td", "", String(it.n)), el("td", "", (it.section ? `[${it.section}] ` : "") + it.text),
      el("td", "", it.verdict_label), el("td", "muted", it.comment));
    tb.append(tr);
  }
  if (!r.items.length) tb.append(emptyRow(4, "제출한 항목이 없습니다."));
  t.append(tb);
  body.append(t);

  const doc = el("details");
  doc.append(el("summary", "", "명세 문서 다시 보기"), el("div", "readme", v.spec_doc));
  body.append(doc);
  show("read-result");
}

// ---------- 공통: C·D 드릴 ----------

const scoreTile = (label, value, sub) => {
  const t = el("div", "score");
  t.append(el("span", "muted", label), el("b", "", value ?? "-"), el("small", "muted", sub || ""));
  return t;
};
// 위로 세는 시계: soft초를 넘으면 색만 바꾼다 (자동 제출 없음)
function countUp(o, timerSel, elapsed, soft) {
  clearInterval(o.tick);
  const t0 = Date.now() - elapsed * 1000;
  const render = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $(timerSel).textContent = fmtSec(s);
    $(timerSel).classList.toggle("low", s > soft);
  };
  render();
  o.tick = setInterval(render, 500);
}
function polBox(p, cls = "") {
  const b = el("div", `pol ${cls}`);
  b.append(el("p", "", `${p.id} ${p.title} (${p.kind_label}) — ${p.location}`));
  if (p.doc_quote) b.append(el("blockquote", "", p.doc_quote));
  b.append(el("p", "muted", p.statement));
  return b;
}
async function bundleButton(btn, statusEl, reload) {
  btn.disabled = true;
  try {
    const b = await withProgress(statusEl, "명세 문서·정책·정답 구현·결함 출제 → 실행 검증 중…", post("/api/criteria/bundles", {}));
    await reload();
    statusEl.textContent = `"${b.title}" — 정책 ${b.policies}개, 결함 ${b.defects}개. 시작을 눌러 푸세요.`;
  } catch (err) {
    statusEl.textContent = `생성 실패: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}
const drillsText = (ds) => (ds.length ? ds.join("·") : "-");

// ---------- AI 변경 검토 드릴 (docs/flow_drills_design.md 4절 C) ----------

const rv = { deck: null, q: null, line: null, tick: null, sending: false };

$("#btn-review").onclick = openReview;
$("#btn-rev-setup").onclick = openReview;
$("#btn-rev-bundle").onclick = () => bundleButton($("#btn-rev-bundle"), $("#rev-bundle-status"), openReview);

async function openReview() {
  location.hash = "review";
  clearInterval(rv.tick);
  show("rev-setup");
  const d = await api("/api/review");
  const tb = $("#rev-bundles");
  tb.innerHTML = "";
  for (const b of d.bundles) {
    const tr = el("tr");
    const name = el("td", "", b.title);
    name.append(el("br"), el("small", "muted", `${b.domain} · ${b.concepts.join(", ")} · 함수 ${b.functions}개`));
    tr.append(name, el("td", "", `${b.defects}개`), el("td", "", `${b.defects_seen} / ${b.defects}`), el("td", "", drillsText(b.drills)));
    const td = el("td");
    const btn = el("button", b.drills.length ? "" : "primary", "시작");
    btn.onclick = async () => {
      btn.disabled = true;
      try { renderReview(await post("/api/review/decks", { bundle_id: b.bundle_id })); }
      catch (err) { $("#rev-start-status").textContent = err.message; btn.disabled = false; }
    };
    td.append(btn);
    tr.append(td);
    tb.append(tr);
  }
  if (!d.bundles.length) tb.append(emptyRow(5, "아직 정책 묶음이 없습니다. 위에서 새로 만드세요."));
  const sb = $("#rev-decks");
  sb.innerHTML = "";
  for (const r of d.reviews) {
    const tr = el("tr");
    tr.append(el("td", "", r.review_id), el("td", "", r.title), el("td", "", `${r.answered} / ${r.cards}장`),
      el("td", "", `${r.all_correct}장`), el("td", "", `${r.caught} / ${r.defects}`), el("td", "", `${r.false_alarm}`));
    const td = el("td");
    const btn = el("button", "", r.done ? "보기" : "이어하기");
    btn.onclick = () => openReviewDeck(r.review_id);
    td.append(btn);
    tr.append(td);
    sb.append(tr);
  }
  if (!d.reviews.length) sb.append(emptyRow(7, "아직 없습니다."));
}

async function openReviewDeck(id) {
  renderReview(await api(`/api/review/decks/${id}`));
}

function renderReview(v) {
  rv.deck = v;
  location.hash = `review=${v.review_id}`;
  if (!v.question) return showReviewSummary(v);
  const q = v.question;
  rv.q = q;
  rv.line = null;
  $("#rv-progress").textContent = `카드 ${q.n} / ${q.total}`;
  $("#rv-agent").textContent = q.agent_note;
  $("#rv-ask").classList.remove("hidden");
  $("#rv-result").innerHTML = "";
  $("#rv-doc").textContent = v.spec_doc;
  const code = $("#rv-code");
  code.innerHTML = "";
  for (const s of q.code) {
    const wrap = el("div", "snip");
    wrap.append(el("div", "snip-h", `${s.name} (${s.start}~${s.end}행)`));
    const pre = el("pre");
    for (const l of s.lines) {
      const line = el("span", "ln pick");
      line.dataset.n = l.n;
      line.append(el("i", "", String(l.n)), document.createTextNode(l.text));
      line.onclick = () => {
        rv.line = l.n;
        $$("#rv-code .ln").forEach((x) => x.classList.toggle("sel", Number(x.dataset.n) === l.n));
        $("input[name=rv-verdict][value=violation]").checked = true;
        renderPick();
      };
      pre.append(line);
    }
    wrap.append(pre);
    code.append(wrap);
  }
  const pols = $("#rv-policies");
  pols.innerHTML = "";
  for (const p of q.policies) {
    const b = polBox(p);
    const lab = el("label");
    const inp = el("input");
    inp.type = "radio"; inp.name = "rv-policy"; inp.value = p.id;
    inp.onchange = () => { $("input[name=rv-verdict][value=violation]").checked = true; renderPick(); };
    lab.append(inp, " 이 요구사항을 어김");
    b.append(lab);
    pols.append(b);
  }
  $$("input[name=rv-verdict]").forEach((x) => { x.checked = false; x.onchange = renderPick; });
  $("#rv-status").textContent = "";
  renderPick();
  countUp(rv, "#rv-timer", q.elapsed_sec, q.soft_sec);
  show("rev-card");
}

function renderPick() {
  const v = $("input[name=rv-verdict]:checked")?.value;
  const p = $("input[name=rv-policy]:checked")?.value;
  $("#rv-pick").textContent = v === "violation" ? `요구사항: ${p || "(고르세요)"} · 줄: ${rv.line ?? "(클릭하세요)"}` : "";
}

$("#btn-rv-submit").onclick = async () => {
  if (rv.sending) return;
  const verdict = $("input[name=rv-verdict]:checked")?.value;
  const policy_id = $("input[name=rv-policy]:checked")?.value || null;
  if (!verdict) { $("#rv-status").textContent = "위반 여부를 고르세요."; return; }
  if (verdict === "violation" && !policy_id) { $("#rv-status").textContent = "어긴 요구사항을 고르세요."; return; }
  rv.sending = true;
  try {
    const r = await post(`/api/review/decks/${rv.deck.review_id}/cards/${rv.q.n}/answer`,
      { verdict, policy_id, line: verdict === "violation" ? rv.line : null });
    showReviewResult(r);
  } catch (err) {
    $("#rv-status").textContent = `제출 실패: ${err.message}`;
  } finally {
    rv.sending = false;
  }
};

function showReviewResult(r) {
  clearInterval(rv.tick);
  $("#rv-timer").textContent = `${r.elapsed_sec}초`;
  $("#rv-timer").classList.remove("low");
  $("#rv-progress").textContent = `카드 ${r.n} / ${r.total} — ${r.all_correct ? "맞음" : "틀림"}`;
  $("#rv-agent").textContent = r.agent_note;
  $("#rv-ask").classList.add("hidden");
  const box = $("#rv-result");
  box.innerHTML = "";
  const a = r.answer;
  const mine = a.verdict === "ok" ? "위반 없음" : `위반 — ${a.policy_id}${a.line ? `, ${a.line}행` : ", 줄 안 고름"}`;
  const truth = r.kind === "correct" ? "위반 없음 (정책을 지킨 코드)" : `위반 — ${r.accepted.map((p) => p.id).join(" 또는 ")}, ${r.hits.join("·")}행`;
  const marks = [["위반 여부", r.verdict_ok]];
  if (r.kind === "defect") marks.push(["요구사항", r.policy_ok], ["줄", r.line_ok]);
  const d = el("div", `slot ${r.all_correct ? "ok" : "bad"}`);
  d.append(el("p", "", marks.map(([k, ok]) => `${ok ? "✓" : "✗"} ${k}`).join("  ")),
    el("div", "ans", `내 답: ${mine}`), el("div", "ans", `정답: ${truth}`));
  box.append(d);
  if (r.kind === "correct") {
    box.append(el("p", "", `이 코드는 관련 요구사항의 확인 예시 ${r.passed}개를 모두 통과합니다.`));
    if (a.verdict === "violation" && r.mine_policy) {
      box.append(el("p", "", "고른 요구사항을 다시 읽고, 코드의 어느 분기가 그 요구사항을 지키는지 찾아보세요."), polBox(r.mine_policy));
    }
    r.code.forEach((s) => box.append(codeBlock(s.name, s.lines, `${s.name} (${s.start}~${s.end}행)`)));
  } else {
    box.append(el("h3", "", "이 코드가 업무에서 일으키는 일"), el("p", "", r.defect_summary));
    box.append(el("h3", "", "어긴 요구사항"), polBox(r.policy, "missed"));
    const others = r.accepted.filter((p) => p.id !== r.policy.id);
    if (others.length) box.append(el("p", "muted", `실행해 보니 이 요구사항의 예시도 틀립니다 (골라도 정답): ${others.map((p) => `${p.id} ${p.title}`).join(", ")}`));
    if (r.evidence.length) {
      box.append(el("h3", "", "실행으로 확인"));
      const t = el("table", "list");
      t.innerHTML = "<thead><tr><th>예시</th><th>입력</th><th>요구사항대로</th><th>이 코드</th></tr></thead>";
      const tb = el("tbody");
      for (const e of r.evidence) {
        const tr = el("tr", "bad");
        tr.append(el("td", "", e.eid), el("td", "", e.call), el("td", "", e.expected), el("td", "", e.actual));
        tb.append(tr);
      }
      t.append(tb);
      box.append(t);
    }
    box.append(el("h3", "", "에이전트 코드 (바뀐 줄 표시)"));
    r.code.forEach((s) => box.append(codeBlock(s.name, s.lines, `${s.name} (${s.start}~${s.end}행)`)));
    const det = el("details");
    det.append(el("summary", "", "요구사항을 지킨 코드와 비교"));
    r.ref_code.forEach((s) => det.append(codeBlock(s.name, s.lines, `정답 ${s.name}`)));
    box.append(det);
  }
  const row = el("div", "row");
  const next = el("button", "primary", r.done ? "요약 보기" : "다음 카드");
  next.onclick = () => openReviewDeck(rv.deck.review_id);
  row.append(next);
  box.append(row);
  show("rev-card");
  next.focus();
}

function showReviewSummary(v) {
  clearInterval(rv.tick);
  const s = v.summary;
  $("#rvs-title").textContent = `검토 결과 — ${v.title}`;
  const body = $("#rvs-body");
  body.innerHTML = "";
  const row = el("div", "score-row");
  row.append(scoreTile("모두 맞음", `${s.all_correct} / ${s.cards}장`, ""),
    scoreTile("결함 발견", pctText(s.catch_rate), `${s.caught} / ${s.defects}장`),
    scoreTile("발견한 결함 중", `정책 ${s.policy_ok} · 줄 ${s.line_ok}`, `/ ${s.caught}장`),
    scoreTile("오경보", `${s.false_alarm}장`, `정상 코드 ${s.clean}장 중`),
    scoreTile("평균 시간", s.avg_sec == null ? "-" : `${s.avg_sec}초`, "목표 장당 2~3분"));
  body.append(row);
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>카드</th><th>코드</th><th>종류</th><th>결과</th><th></th></tr></thead>";
  const tb = el("tbody");
  for (const it of v.items) {
    const tr = el("tr", it.all_correct ? "" : "bad");
    tr.append(el("td", "", String(it.n)), el("td", "", it.names.join(", ")),
      el("td", "", it.kind === "correct" ? "정상" : "결함"), el("td", "", it.all_correct ? "맞음" : "틀림"));
    const td = el("td");
    const b = el("button", "", "다시 보기");
    b.onclick = async () => showReviewResult(await api(`/api/review/decks/${v.review_id}/cards/${it.n}/result`));
    td.append(b);
    tr.append(td);
    tb.append(tr);
  }
  t.append(tb);
  body.append(t);
  show("rev-summary");
}

// ---------- 변경 대응 드릴 (docs/flow_drills_design.md 4절 D) ----------

const cg = { resp: null, q: null, tick: null, sending: false };

$("#btn-change").onclick = openChange;
$("#btn-chg-setup").onclick = openChange;
$("#btn-chg-bundle").onclick = () => bundleButton($("#btn-chg-bundle"), $("#chg-bundle-status"), openChange);

async function openChange() {
  location.hash = "change";
  clearInterval(cg.tick);
  show("chg-setup");
  const d = await api("/api/change");
  const tb = $("#chg-bundles");
  tb.innerHTML = "";
  for (const b of d.bundles) {
    const tr = el("tr");
    const name = el("td", "", b.title);
    name.append(el("br"), el("small", "muted", `${b.domain} · 정책 ${b.policies}개 · 함수 ${b.functions}개`));
    tr.append(name, el("td", "", b.changes ? `${b.changes}개` : "아직 (시작 시 출제)"), el("td", "", `${b.responses}회`),
      el("td", "", drillsText(b.drills)));
    const td = el("td");
    const btn = el("button", "primary", "시작");
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const p = post("/api/change/responses", { bundle_id: b.bundle_id });
        renderChange(b.changes ? await p : await withProgress($("#chg-start-status"), "정책 변경 공지 출제 → 실행으로 영향 범위 계산 중…", p));
      } catch (err) {
        $("#chg-start-status").textContent = err.message;
        btn.disabled = false;
      }
    };
    td.append(btn);
    tr.append(td);
    tb.append(tr);
  }
  if (!d.bundles.length) tb.append(emptyRow(5, "아직 정책 묶음이 없습니다. 위에서 새로 만드세요."));
  const sb = $("#chg-resps");
  sb.innerHTML = "";
  for (const r of d.responses) {
    const tr = el("tr");
    const rp = (rec, pre) => (rec == null ? "-" : `재현 ${rec}% · 정밀 ${pre ?? "-"}%`);
    tr.append(el("td", "", r.response_id), el("td", "", r.title), el("td", "", `${r.answered} / ${r.cards}장`),
      el("td", "", `${r.all_correct}장`), el("td", "", rp(r.policy_recall, r.policy_precision)),
      el("td", "", rp(r.example_recall, r.example_precision)), el("td", "", rp(r.code_recall, r.code_precision)));
    const td = el("td");
    const btn = el("button", "", r.done ? "보기" : "이어하기");
    btn.onclick = () => openResponse(r.response_id);
    td.append(btn);
    tr.append(td);
    sb.append(tr);
  }
  if (!d.responses.length) sb.append(emptyRow(8, "아직 없습니다."));
}

async function openResponse(id) {
  renderChange(await api(`/api/change/responses/${id}`));
}

function checkRow(name, value, labelNode) {
  const lab = el("label", "pickrow");
  const inp = el("input");
  inp.type = "checkbox"; inp.name = name; inp.value = value;
  lab.append(inp, labelNode);
  return lab;
}

function renderChange(v) {
  cg.resp = v;
  location.hash = `change=${v.response_id}`;
  if (!v.question) return showChangeSummary(v);
  const q = v.question;
  cg.q = q;
  $("#cg-progress").textContent = `변경 ${q.n} / ${q.total}`;
  $("#cg-notice").textContent = `[${q.title}]\n${q.notice}`;
  $("#cg-ask").classList.remove("hidden");
  $("#cg-result").innerHTML = "";
  $("#cg-doc").textContent = v.spec_doc;
  const pols = $("#cg-policies");
  pols.innerHTML = "";
  for (const p of q.policies) {
    const t = el("span", "", `${p.id} ${p.title} — `);
    t.append(el("span", "muted", p.statement));
    pols.append(checkRow("cg-pol", p.id, t));
  }
  const tb = $("#cg-examples");
  tb.innerHTML = "";
  for (const e of q.examples) {
    const tr = el("tr");
    const c = el("td");
    const inp = el("input");
    inp.type = "checkbox"; inp.name = "cg-ex"; inp.value = e.eid;
    c.append(inp);
    const val = el("input");
    val.autocomplete = "off";
    val.dataset.eid = e.eid;
    val.disabled = true;
    const r = e.returns;
    val.placeholder = r.type === "choice" ? r.options.join(" / ") : `${r.label} (${TYPE_HINT[r.type] || r.type})`;
    inp.onchange = () => { val.disabled = !inp.checked; if (inp.checked) val.focus(); };
    const vtd = el("td");
    vtd.append(val);
    tr.append(c, el("td", "", e.eid), el("td", "", e.call), el("td", "", e.expected), vtd);
    tb.append(tr);
  }
  const ch = $("#cg-chunks");
  ch.innerHTML = "";
  for (const x of q.chunks) {
    ch.append(checkRow("cg-chunk", x.name, el("span", "", `${x.name} (${x.kind === "func" ? "함수" : "상수·표"}, ${x.start}~${x.end}행)`)));
  }
  const code = $("#cg-code");
  code.innerHTML = "";
  code.append(codeBlock("", q.code, "지금 코드"));
  $("#cg-status").textContent = "";
  countUp(cg, "#cg-timer", q.elapsed_sec, q.soft_sec);
  show("chg-card");
  window.scrollTo(0, 0);
}

$("#btn-cg-submit").onclick = async () => {
  if (cg.sending) return;
  const checked = (n) => [...$$(`input[name=${n}]:checked`)].map((x) => x.value);
  const examples = {};
  for (const eid of checked("cg-ex")) {
    const v = $(`#cg-examples input[data-eid="${eid}"]`).value.trim();
    if (!v) { $("#cg-status").textContent = `${eid}의 새 기대값을 적으세요.`; return; }
    examples[eid] = v;
  }
  cg.sending = true;
  try {
    const r = await post(`/api/change/responses/${cg.resp.response_id}/cards/${cg.q.n}/answer`,
      { policies: checked("cg-pol"), examples, chunks: checked("cg-chunk") });
    showChangeResult(r);
  } catch (err) {
    $("#cg-status").textContent = `제출 실패: ${err.message}`;
  } finally {
    cg.sending = false;
  }
};

function showChangeResult(r) {
  clearInterval(cg.tick);
  $("#cg-timer").textContent = `${r.elapsed_sec}초`;
  $("#cg-timer").classList.remove("low");
  $("#cg-progress").textContent = `변경 ${r.n} / ${r.total} — ${r.result.all_correct ? "모두 맞음" : "채점 결과"}`;
  $("#cg-notice").textContent = `[${r.title}]\n${r.notice}`;
  $("#cg-ask").classList.add("hidden");
  const box = $("#cg-result");
  box.innerHTML = "";
  const s = r.result;
  const ax = (x) => `${x.hit} / ${x.truth}`;
  const sub = (x) => `놓침 ${x.missed} · 불필요 ${x.extra}`;
  const row = el("div", "score-row");
  row.append(scoreTile("요구사항", ax(s.policies), sub(s.policies)), scoreTile("성공 기준 예시", ax(s.examples), sub(s.examples)),
    scoreTile("새 기대값", `${s.values_ok} / ${s.examples.truth}`, "맞게 적은 값"), scoreTile("코드 위치", ax(s.code), sub(s.code)));
  box.append(row);
  const mark = (t, m) => (t && m ? "✓" : t ? "✗ 놓침" : "✗ 불필요");

  box.append(el("h3", "", "요구사항"));
  for (const p of r.policies) {
    const b = polBox(p, p.truth && p.mine ? "caught" : "missed");
    b.firstChild.textContent = `${mark(p.truth, p.mine)} ${b.firstChild.textContent}`;
    if (p.new_statement) b.append(el("p", "mine", `바뀐 뒤: ${p.new_statement}`));
    else if (p.ripple) b.append(el("p", "ev", "문구는 그대로지만 이 변경 때문에 예시 값이 바뀝니다 (파급)."));
    else if (!p.truth) b.append(el("p", "ev", "이 변경으로 바뀌지 않습니다. 실행해도 이 요구사항의 예시 값은 그대로입니다."));
    box.append(b);
  }
  if (!r.policies.length) box.append(el("p", "muted", "없음"));

  box.append(el("h3", "", `성공 기준 예시 (바뀌지 않는 예시 ${r.unchanged_examples}개는 생략)`));
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>판정</th><th>예시</th><th>입력</th><th>지금</th><th>바뀐 뒤</th><th>내 답</th></tr></thead>";
  const tb = el("tbody");
  for (const e of r.examples) {
    const ok = e.truth && e.chosen && e.value_ok;
    const tr = el("tr", ok ? "ok" : "bad");
    const verdict = !e.truth ? "✗ 바뀌지 않음" : !e.chosen ? "✗ 놓침" : e.value_ok ? "✓" : "✗ 값 틀림";
    tr.append(el("td", "", verdict), el("td", "", e.eid), el("td", "", e.call), el("td", "", e.old),
      el("td", "", e.new), el("td", "", e.mine ?? "-"));
    tb.append(tr);
  }
  t.append(tb);
  box.append(t);

  box.append(el("h3", "", "코드 위치"));
  for (const c of r.code_rows) {
    const txt = c.truth ? `${mark(true, c.mine)} ${c.name}` : c.reader ? `${c.mine ? "○" : "·"} ${c.name} — 바뀐 기준표를 읽는 곳 (고칠 곳은 아님, 골라도 감점 없음)` : `✗ 불필요 ${c.name}`;
    box.append(el("p", "", txt));
  }
  box.append(el("h4", "", "바뀐 뒤 코드 (바뀐 줄 표시)"));
  r.new_code.forEach((sn) => box.append(codeBlock(sn.name, sn.lines, `${sn.name} (${sn.start}~${sn.end}행)`)));
  const det = el("details");
  det.append(el("summary", "", "바뀌기 전 코드"));
  r.old_code.forEach((sn) => det.append(codeBlock(sn.name, sn.lines, `${sn.name}`)));
  box.append(det);

  const nrow = el("div", "row");
  const next = el("button", "primary", r.done ? "요약 보기" : "다음 변경");
  next.onclick = () => openResponse(cg.resp.response_id);
  nrow.append(next);
  box.append(nrow);
  show("chg-card");
  window.scrollTo(0, 0);
}

function showChangeSummary(v) {
  clearInterval(cg.tick);
  const s = v.summary;
  $("#cgs-title").textContent = `변경 대응 결과 — ${v.title}`;
  const body = $("#cgs-body");
  body.innerHTML = "";
  const rp = (rec, pre) => `재현 ${pctText(rec)} · 정밀 ${pctText(pre)}`;
  const row = el("div", "score-row");
  row.append(scoreTile("모두 맞음", `${s.all_correct} / ${s.cards}장`, ""),
    scoreTile("요구사항", rp(s.policy_recall, s.policy_precision), "평균"),
    scoreTile("성공 기준 예시", rp(s.example_recall, s.example_precision), `새 기대값 ${s.values_ok} / ${s.values_total}`),
    scoreTile("코드 위치", rp(s.code_recall, s.code_precision), "평균"),
    scoreTile("평균 시간", s.avg_sec == null ? "-" : `${s.avg_sec}초`, "목표 장당 3~5분"));
  body.append(row);
  body.append(el("p", "muted", "재현 = 고쳐야 하는 것 중 고른 비율 (놓치면 낮음). 정밀 = 고른 것 중 정말 고쳐야 하는 비율 (안 바뀌는 것까지 고치면 낮음)."));
  const t = el("table", "list");
  t.innerHTML = "<thead><tr><th>변경</th><th>공지</th><th>결과</th><th></th></tr></thead>";
  const tb = el("tbody");
  for (const it of v.items) {
    const tr = el("tr", it.all_correct ? "" : "bad");
    tr.append(el("td", "", String(it.n)), el("td", "", it.title), el("td", "", it.all_correct ? "모두 맞음" : "틀림"));
    const td = el("td");
    const b = el("button", "", "다시 보기");
    b.onclick = async () => showChangeResult(await api(`/api/change/responses/${v.response_id}/cards/${it.n}/result`));
    td.append(b);
    tr.append(td);
    tb.append(tr);
  }
  t.append(tb);
  body.append(t);
  show("chg-summary");
}

// ---------- 상단 mockdrill 표시 → 홈 ----------

// 시간이 흐르는 화면: 떠나도 서버 쪽 시간은 계속 흐르므로 확인을 받는다
const TIMED_VIEWS = ["exam", "submit", "rehearsal", "drill-card", "crit-sheet", "read-sheet"];

$("#brand").onclick = async () => {
  const cur = [...$$(".view")].find((v) => !v.classList.contains("hidden"))?.id;
  if (cur === "home") return;
  if (TIMED_VIEWS.includes(cur) && !confirm("진행 중인 시간은 계속 흐릅니다. 홈으로 갈까요? (작성한 내용은 저장됩니다)")) return;
  [rh, dc, cs, rs, rv, cg].forEach((o) => clearInterval(o.tick));
  // 아직 저장 대기 중인 드릴 답안은 바로 저장
  if (cur === "crit-sheet" && cs.sheet) {
    clearTimeout(cs.saveTimer);
    await api(`/api/criteria/sheets/${cs.sheet.sheet_id}/rows`, { method: "PUT", body: JSON.stringify({ rows: cs.rows }) }).catch(() => {});
  }
  if (cur === "read-sheet" && rs.sheet) {
    clearTimeout(rs.saveTimer);
    await saveReadText().catch(() => {});
  }
  show("home");
};

// ---------- 시작 ----------

loadOptions();
const m = location.hash.match(/s=([\w]+)/);
if (m) openSession(m[1]).catch(() => show("home"));
else if (location.hash === "#dashboard") openDashboard();
else if (location.hash.startsWith("#run=")) openRun(location.hash.slice(5)).catch(() => show("home"));
else if (location.hash === "#drill") openDrillSetup();
else if (location.hash === "#criteria") openCriteria();
else if (location.hash.startsWith("#sheet=")) openSheet(location.hash.slice(7)).catch(() => show("home"));
else if (location.hash === "#reading") openReading();
else if (location.hash.startsWith("#read=")) openRead(location.hash.slice(6)).catch(() => show("home"));
else if (location.hash === "#review") openReview();
else if (location.hash.startsWith("#review=")) openReviewDeck(location.hash.slice(8)).catch(() => show("home"));
else if (location.hash === "#change") openChange();
else if (location.hash.startsWith("#change=")) openResponse(location.hash.slice(8)).catch(() => show("home"));
else loadSessions();
