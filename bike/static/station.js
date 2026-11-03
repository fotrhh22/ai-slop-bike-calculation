"use strict";
const el = (id) => document.getElementById(id);
let state = null;
let pending = false;
let historyKey = "";
let queueKey = "";
const fmt = (value, digits = 2) => Number(value || 0).toFixed(digits);
const labels = { running: "측정 중", completed: "측정 완료", stopped: "측정 중단", interrupted: "프로그램 중단" };

async function request(path, body) {
  const options = { cache: "no-store", signal: AbortSignal.timeout ? AbortSignal.timeout(12000) : undefined };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : "요청을 처리하지 못했습니다.");
  }
  return response.json();
}

function error(message) {
  el("error").hidden = !message;
  el("error").textContent = message;
}

async function action(path, body = {}) {
  if (pending) return null;
  pending = true;
  error("");
  if (state) render(state);
  try {
    return await request(path, body);
  } catch (exc) {
    error(exc.message || "연결을 확인하세요.");
    return null;
  } finally {
    pending = false;
    historyKey = "";
    await refresh();
  }
}

function render(data) {
  state = data;
  const session = data.session;
  const running = session?.status === "running";
  el("mode-label").textContent = data.mode === "usb" ? "USB 실측" : "BLE 실측";
  el("connection").className = "badge " + (data.fresh ? "live" : data.connected ? "warn" : "");
  el("connection").replaceChildren(Object.assign(document.createElement("i"), {}),
    document.createTextNode(data.fresh ? "실측 수신 중" : data.connected ? "데이터 대기 / 지연" : "연결 대기"));
  el("session-status").textContent = session ? labels[session.status] || session.status : "참가자 준비";
  const energy = (session?.campaign_energy_wh || 0) * 1000;
  el("energy").textContent = fmt(energy, 1);
  el("energy-wh").textContent = fmt(energy / 1000, 6) + " Wh";
  el("participant-label").textContent = session?.participant || "—";
  el("timer").textContent = formatTime(session?.elapsed_s || 0);
  el("timer").classList.toggle("long", (session?.elapsed_s || 0) >= 3600);
  el("timer-note").textContent = running ? (data.fresh ? "쉬는 동안에도 시간은 계속 흐릅니다." : "수신 지연 · 미수신 구간은 제외합니다.") : session ? "결과가 Mac에 저장되었습니다." : "측정값을 수신하면 시작할 수 있어요.";
  const reading = data.fresh ? data.reading : null;
  el("power").textContent = reading ? fmt(reading.power_w * 100, 2) + " W" : "— W";
  el("electrical").textContent = reading ? fmt(reading.voltage_v, 3) + " V / " + fmt(reading.current_a, 3) + " A" : "— V / — A";
  el("coverage").textContent = session ? fmt(session.coverage_pct, 1) + " %" : "— %";
  if (session) {
    el("participant").value = session.participant || "";
    el("target").value = session.target_wh == null ? "" : String(session.target_wh * 1000);
  }
  const selected = data.campaign?.selected;
  if (!session && selected) {
    el("participant").value = selected.participant_code;
    const value = selected.target_wh ?? selected.reference_target_wh;
    el("target").value = value == null ? "" : String(value * 1000);
  } else if (!session && data.campaign?.enabled) {
    el("participant").value = "";
    el("target").value = "";
  }
  renderCampaign(data);
  renderComparison(data);
  el("session-note").textContent = session ? "유효 계측 " + fmt(session.measured_s, 1) + "초 / 경과 " + fmt(session.elapsed_s, 1) + "초 · 실측×100 환산 · 측정 중 새로고침해도 Mac의 기록은 유지됩니다." : "시작 전에 받은 값은 참가자 전력량에 포함하지 않습니다. 기록은 Mac에 저장됩니다.";
  el("start").disabled = pending || running || !!session || !data.fresh || !!data.campaign?.starting || (data.campaign?.enabled && (!selected || (selected.target_wh ?? selected.reference_target_wh) == null));
  el("stop").disabled = pending || !running;
  el("reset").disabled = pending || running || !session;
  el("participant").disabled = running || !!session || !!data.campaign?.enabled;
  el("target").readOnly = true;
  el("target").disabled = pending || running || !!session;
  el("target-help").textContent = selected?.energy_basis === "tokens_only"
    ? "RTX 4090 생성+판정 3회 실측 평균 346.3 mWh를 고정 비교 기준으로 사용합니다. Gemini 소비량 측정·추정값이 아닙니다."
    : selected ? "해당 참가자의 GPU 실측 사용량에서 자동 설정된 목표입니다." : "참가자를 선택하면 비교 목표가 자동으로 표시됩니다.";
  for (const [id, mode] of [["real-mode", "ble"], ["usb-mode", "usb"]]) {
    el(id).disabled = pending || running;
    el(id).classList.toggle("selected", data.mode === mode);
    el(id).setAttribute("aria-pressed", String(data.mode === mode));
  }
  el("mode-help").textContent = data.mode === "usb"
      ? "Mac과 FNB58의 PC 포트를 데이터 케이블로 연결한 뒤 기기를 찾아주세요."
      : "FNB58에 전원을 공급한 뒤 기기를 찾아주세요. Bluetooth 연결은 Mac에서 진행합니다.";
  for (const mode of ["usb", "ble"]) el(mode + "-card").hidden = data.mode !== mode;
  for (const mode of ["usb", "ble"]) {
    const t = data.transports?.[mode] || {};
    const prefix = mode === "usb" ? "usb-" : "";
    const active = t.connected || t.connecting;
    const selectable = !!el(prefix + "devices").value;
    el(prefix + "scan").hidden = active;
    el(prefix + "scan").textContent = t.busy ? "찾는 중…" : selectable ? "다시 찾기" : "기기 찾기";
    el(prefix + "scan").disabled = pending || t.busy || active;
    el(mode + "-picker").hidden = active || !selectable;
    el(prefix + "devices").disabled = pending || t.busy;
    el(prefix + "connect").disabled = pending || t.busy || active || !selectable;
    el(prefix + "disconnect").hidden = !active;
    el(prefix + "disconnect").disabled = pending || running || t.busy || !active;
    const issue = !active && /오류|실패|못했습니다|권한|초과/.test(t.message || "");
    const status = t.busy ? "검색 중" : t.connecting ? "연결 중" : t.fresh ? "수신 중" : t.connected ? "데이터 대기" : issue ? "연결 확인 필요" : "연결 대기";
    el(mode + "-status").textContent = status;
    el(mode + "-status").classList.toggle("live", !!t.fresh);
    const r = t.fresh ? t.reading : null;
    el(mode + "-diagnostic").textContent = r
      ? fmt(r.voltage_v, 3) + " V · " + fmt(r.current_a, 3) + " A · " + fmt(r.power_w, 3) + " W"
      : t.connected ? "연결됐습니다. 새 측정값을 기다리고 있어요."
      : t.connecting ? "FNB58에 연결하고 있어요."
      : t.busy ? "주변 기기를 찾고 있어요. 잠시 기다려주세요."
      : issue || /범위/.test(t.message || "") ? t.message
      : selectable ? "찾은 기기를 선택하고 연결하기를 눌러주세요."
      : "기기 찾기를 눌러 FNB58을 연결하세요.";
    el(mode + "-stats").textContent = "유효 샘플 " + (t.samples || 0) + " · 패킷 " + (t.raw_packets || 0) +
      " · 형식 오류 " + (t.bad_frames || 0) + (t.sample_age_s == null ? "" : " · 마지막 수신 " + fmt(t.sample_age_s, 1) + "초 전") +
      (t.message ? " · " + t.message : "");
  }
  el("device-name").textContent = data.device_name || "연결 대기";
  drawChart((data.trace || []).map(p => ({...p, w:p.w*100})), session?.elapsed_s || 0);
}

function renderCampaign(data) {
  const campaign = data.campaign || {};
  el("campaign-panel").hidden = !campaign.enabled;
  if (!campaign.enabled) return;
  el("campaign-sync").textContent = campaign.error ? "연결 대기" : "목록 연결됨";
  el("campaign-selection").textContent = campaign.selected ? "선택: " + campaign.selected.participant_code + (campaign.selected.energy_basis === "tokens_only" ? " · Gemini / GPU 실측 평균 고정 비교 기준" : " · 목표 " + fmt(campaign.selected.target_wh, 4) + " Wh") : "목록에서 참가자 코드를 선택하세요.";
  el("campaign-notice").textContent = campaign.error || (campaign.pending_ends ? "종료 기록 " + campaign.pending_ends + "건 전송 대기 · Mac에 저장됨" : data.session?.campaign_entry && data.session.status !== "running" ? "계측 종료 기록 전송 완료 · 프린터 연결을 확인하세요." : "계측 종료 시 달성 여부와 관계없이 한 번 출력합니다.");
  const key = JSON.stringify([campaign.entries, campaign.selected?.id, !!data.session, pending, campaign.starting, el("campaign-search").value]);
  if (key === queueKey) return;
  queueKey = key;
  const list = el("campaign-queue"); list.replaceChildren();
  const query = el("campaign-search").value.trim().toUpperCase();
  const entries = (campaign.entries || []).filter(e => e.participant_code.includes(query))
    .sort((a,b) => (b.created_at || "").localeCompare(a.created_at || "") || b.id.localeCompare(a.id));
  if (!entries.length) { list.textContent = "대기 참가자가 없습니다."; return; }
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "queue-participant-row";
    const button = document.createElement("button");
    button.className = "queue-participant" + (campaign.selected?.id === entry.id ? " selected" : "");
    button.textContent = entry.participant_code + " · " + (entry.energy_basis === "tokens_only" ? "Gemini / 토큰 전용" : entry.target_wh == null ? "계측 확인 필요" : fmt(entry.target_wh,4)+" Wh") + (entry.state === "measuring" ? " · 측정 중" : "");
    button.disabled = pending || !!data.session || campaign.starting || entry.energy_basis === "unavailable";
    button.onclick = () => action("/api/campaign/select", {entry_id:entry.id});
    const remove = document.createElement("button");
    remove.className = "queue-participant-delete";
    remove.textContent = "×";
    remove.setAttribute("aria-label", entry.participant_code + " 참가자 데이터 삭제");
    remove.title = "참가자와 연결된 데이터 삭제";
    remove.disabled = pending || campaign.starting || entry.state !== "waiting" || !!entry.bike_session_id || data.session?.campaign_entry?.id === entry.id;
    remove.onclick = () => {
      if (window.confirm(entry.participant_code + " 참가자와 AI 영수증·이미지·생성/판정 기록을 삭제할까요?")) {
        void action("/api/campaign/delete", {entry_id:entry.id});
      }
    };
    row.append(button, remove);
    list.append(row);
  }
}

function renderComparison(data) {
  const session = data.session;
  // A saved session owns its comparison target. Draft edits only preview idle sessions.
  const targetWh = session ? session.target_wh : Number(el("target").value) / 1000;
  const valid = Number.isFinite(targetWh) && targetWh > 0;
  el("comparison-content").hidden = !valid;
  el("target-empty").hidden = valid;
  if (!valid) {
    const tokenOnly = (session?.campaign_entry || data.campaign?.selected)?.energy_basis === "tokens_only";
    el("comparison-phase").textContent = tokenOnly ? "Gemini · 목표 없음" : "기준 미입력";
    el("target-empty").textContent = tokenOnly ? "Gemini 고정 비교 목표를 확인할 수 없습니다. SQL·서버 업데이트를 확인하세요." : "참가자를 선택하면 비교 목표가 자동 표시됩니다.";
    el("comparison-percent").textContent = "—";
    el("comparison-fill").style.width = "0%";
    el("comparison-progress").setAttribute("aria-valuenow", "0");
    el("comparison-progress").setAttribute("aria-valuetext", "AI 비교 기준 미입력");
    el("comparison-progress").removeAttribute("title");
    return;
  }
  const generated = Math.max(0, session?.campaign_energy_wh != null ? session.campaign_energy_wh * 1000 : session?.energy_mwh || 0);
  const target = targetWh * 1000;
  const ratio = generated / target;
  const percent = ratio * 100;
  const number = (value, digits = 2) => value.toLocaleString("ko-KR", { maximumFractionDigits: digits });
  const bounded = Math.min(100, Math.max(0, percent));
  el("comparison-phase").textContent = !session ? "측정 준비" : session.status === "running" ? "측정 중" : "측정 결과";
  el("comparison-generated-label").textContent = "×100 환산 발전량";
  el("comparison-generated").textContent = number(generated);
  el("comparison-target").textContent = number(target);
  el("comparison-equivalent").textContent = "실측×100 환산 · 목표 대비 " + number(percent, 1) + "%";
  el("comparison-percent").textContent = number(percent, 1) + "%";
  el("comparison-fill").style.width = bounded + "%";
  el("comparison-progress").setAttribute("aria-valuenow", String(bounded));
  el("comparison-progress").setAttribute("aria-valuetext", "AI 목표 대비 환산 " + number(percent, 1) + "%");
  el("comparison-remaining").textContent = ratio >= 1
    ? "환산 목표 도달 · " + number(generated - target) + " mWh 초과"
    : "환산 목표까지 " + number(target - generated) + " mWh 남았어요.";
  el("comparison-progress").setAttribute("title", el("comparison-remaining").textContent);
}

function formatTime(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const clock = String(minutes).padStart(2, "0") + ":" + String(total % 60).padStart(2, "0");
  return hours ? String(hours).padStart(2, "0") + ":" + clock : clock;
}

function drawChart(trace, elapsed) {
  // Keep the bounded recent trace visible even in sessions longer than 3 min.
  const left = trace.length ? trace[0].t : 0;
  const right = Math.max(left + 1, elapsed, trace.length ? trace[trace.length - 1].t : 0);
  el("chart-range").textContent = formatTime(left) + " — " + formatTime(right) + " · W";
  const max = Math.max(1, ...trace.map((point) => point.w));
  let path = "";
  let before = null;
  for (const point of trace) {
    const x = (point.t - left) / (right - left) * 900;
    const y = 105 - point.w / max * 95;
    path += (before === null || point.t - before > 2 ? "M" : "L") + x.toFixed(2) + " " + y.toFixed(2);
    before = point.t;
  }
  el("chart-line").setAttribute("d", path);
}

async function loadHistory() {
  const rows = await request("/api/history");
  const tbody = el("history");
  tbody.replaceChildren();
  if (!rows.length) {
    const cell = tbody.insertRow().insertCell();
    cell.colSpan = 5;
    cell.textContent = "아직 저장된 측정이 없습니다.";
  }
  for (const row of rows) {
    const tr = tbody.insertRow();
    const name = tr.insertCell();
    name.textContent = row.participant;
    const date = document.createElement("small");
    date.textContent = new Date(row.started_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", hour12: false });
    name.append(date);
    const mode = tr.insertCell();
    mode.textContent = row.mode === "demo" ? "테스트" : row.mode === "usb" ? "USB 실측" : "BLE 실측";
    if (row.mode === "demo") mode.className = "table-demo";
    const status = document.createElement("small");
    status.textContent = labels[row.status] || row.status;
    mode.append(status);
    tr.insertCell().textContent = fmt(row.energy_wh * 100 * 1000, 1) + " mWh";
    tr.insertCell().textContent = fmt(row.elapsed_s ? row.measured_s / row.elapsed_s * 100 : 0, 1) + "%";
    const link = document.createElement("a");
    link.href = "/api/export/" + encodeURIComponent(row.id) + ".csv";
    link.textContent = "CSV ↓";
    tr.insertCell().append(link);
  }
}

async function refresh() {
  try {
    const data = await request("/api/state");
    el("offline").hidden = true;
    render(data);
    const key = data.session ? data.session.id + data.session.status : "idle";
    if (key !== historyKey) {
      await loadHistory();
      historyKey = key;
    }
  } catch (_) {
    el("offline").hidden = false;
    for (const button of document.querySelectorAll("button")) button.disabled = true;
  }
}

el("start").onclick = () => {
  const raw = el("target").value.trim();
  const target = raw ? Number(raw) : null;
  if (target !== null && (!Number.isFinite(target) || target <= 0)) return error("목표 mWh는 0보다 큰 숫자로 입력하세요.");
  action("/api/session/start", { participant: el("participant").value, target_wh: target === null ? null : target / 1000 });
};
el("target").oninput = () => state && renderComparison(state);
el("stop").onclick = () => action("/api/session/stop");
el("reset").onclick = async () => {
  if (await action("/api/session/reset")) { el("participant").value = ""; el("target").value = ""; }
};
el("real-mode").onclick = () => action("/api/mode", { mode: "ble" });
el("usb-mode").onclick = () => action("/api/mode", { mode: "usb" });
for (const mode of ["usb", "ble"]) {
  const prefix = mode === "usb" ? "usb-" : "";
  el(prefix + "scan").onclick = async () => {
    const devices = await action("/api/device/scan", { transport: mode });
    if (!devices) return;
    el(prefix + "devices").replaceChildren();
    for (const device of devices) el(prefix + "devices").append(new Option(device.name, device.id));
    if (!devices.length) el(prefix + "devices").append(new Option("검색된 기기 없음 · 다시 검색하세요", ""));
    if (state) render(state);
  };
  el(prefix + "connect").onclick = () => action("/api/device/connect", { transport: mode, device_id: el(prefix + "devices").value });
  el(prefix + "disconnect").onclick = () => action("/api/device/disconnect", { transport: mode });
  el(prefix + "devices").onchange = () => state && render(state);
}
(async function poll() { await refresh(); setTimeout(poll, 250); })();

el("campaign-search").oninput = () => { queueKey=""; if (state) renderCampaign(state); };
