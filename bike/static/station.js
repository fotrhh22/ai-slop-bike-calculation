"use strict";
const el = (id) => document.getElementById(id);
let state = null;
let pending = false;
let historyKey = "";
const fmt = (value, digits = 2) => Number(value || 0).toFixed(digits);
const labels = { running: "측정 중", completed: "3분 측정 완료", stopped: "측정 중단", interrupted: "프로그램 중단" };

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
  const demo = data.mode === "demo";
  el("demo-banner").hidden = !demo;
  el("mode-label").textContent = demo ? "SIMULATION" : data.mode === "usb" ? "USB 실측" : "BLE 실측";
  el("connection").className = "badge " + (data.fresh ? "live" : data.connected ? "warn" : "");
  el("connection").replaceChildren(Object.assign(document.createElement("i"), {}),
    document.createTextNode(data.fresh ? (demo ? "테스트 수신 중" : "실측 수신 중") : data.connected ? "데이터 대기 / 지연" : "연결 대기"));
  el("session-status").textContent = session ? labels[session.status] || session.status : "참가자 준비";
  const energy = session?.energy_mwh || 0;
  el("energy").textContent = fmt(energy, 1);
  el("energy-wh").textContent = fmt(energy / 1000, 6) + " Wh";
  el("participant-label").textContent = session?.participant || "—";
  const remaining = Math.max(0, Math.ceil(180 - (session?.elapsed_s || 0)));
  el("timer").textContent = String(Math.floor(remaining / 60)).padStart(2, "0") + ":" + String(remaining % 60).padStart(2, "0");
  el("time-progress").style.width = ((session?.elapsed_s || 0) / 180 * 100) + "%";
  el("timer-note").textContent = running ? (data.fresh ? "실제 수신 구간만 전력량에 더합니다." : "수신이 지연되었습니다. 타이머는 진행되며 미수신 구간은 제외됩니다.") : session ? "결과가 Mac에 저장되었습니다." : "측정값이 수신되면 시작할 수 있습니다.";
  const reading = data.fresh ? data.reading : null;
  el("power").textContent = reading ? fmt(reading.power_w, 3) + " W" : "— W";
  el("electrical").textContent = reading ? fmt(reading.voltage_v, 3) + " V / " + fmt(reading.current_a, 3) + " A" : "— V / — A";
  el("coverage").textContent = session ? fmt(session.coverage_pct, 1) + " %" : "— %";
  el("target-result").hidden = !session?.target_wh;
  if (session?.target_wh) el("target-result").textContent = "AI 소비량 " + fmt(session.target_wh, 4) + " Wh 대비 " + fmt(session.energy_wh / session.target_wh * 100, 1) + "%";
  el("session-note").textContent = session ? "유효 계측 " + fmt(session.measured_s, 1) + "초 / 경과 " + fmt(session.elapsed_s, 1) + "초 · " + (demo ? "시뮬레이션 기록" : "USB 전달 전력량") + " · 측정 중 새로고침해도 Mac의 기록은 유지됩니다." : "시작 전에 받은 값은 참가자 전력량에 포함하지 않습니다. 기록은 Mac에 저장됩니다.";
  el("start").disabled = pending || running || !!session || !data.fresh;
  el("stop").disabled = pending || !running;
  el("reset").disabled = pending || running || !session;
  el("participant").disabled = running || !!session;
  el("target").disabled = running || !!session;
  for (const [id, mode] of [["real-mode", "ble"], ["usb-mode", "usb"], ["demo-mode", "demo"]]) {
    el(id).disabled = pending || running;
    el(id).classList.toggle("selected", data.mode === mode);
  }
  el("transport-controls").hidden = demo;
  for (const mode of ["usb", "ble"]) {
    const t = data.transports?.[mode] || {};
    const prefix = mode === "usb" ? "usb-" : "";
    el(prefix + "scan").disabled = pending || t.busy || t.connected || t.connecting;
    el(prefix + "connect").disabled = pending || t.busy || t.connected || t.connecting || !el(prefix + "devices").value;
    el(prefix + "disconnect").disabled = pending || running || t.busy || !(t.connected || t.connecting);
    el(mode + "-selected").textContent = data.mode === mode ? "· 누적에 사용" : "· 비교 전용";
    const r = t.fresh ? t.reading : null;
    el(mode + "-diagnostic").textContent = (t.message || "연결 대기") + " · " +
      (r ? fmt(r.voltage_v, 3) + " V / " + fmt(r.current_a, 3) + " A / " + fmt(r.power_w, 3) + " W" : "수신값 없음") +
      " · 유효 샘플 " + (t.samples || 0) + " · 패킷 " + (t.raw_packets || 0) +
      " · 형식 오류 " + (t.bad_frames || 0) + (t.sample_age_s == null ? "" : " · 마지막 수신 " + fmt(t.sample_age_s, 1) + "초 전");
  }
  el("device-name").textContent = data.device_name || "선택된 기기 없음";
  el("device-message").textContent = data.message;
  drawChart(data.trace || []);
}

function drawChart(trace) {
  const max = Math.max(1, ...trace.map((point) => point.w));
  let path = "";
  let before = null;
  for (const point of trace) {
    const x = point.t / 180 * 900;
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
    tr.insertCell().textContent = fmt(row.energy_wh * 1000, 1) + " mWh";
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
  if (target !== null && (!Number.isFinite(target) || target <= 0)) return error("목표 Wh는 0보다 큰 숫자로 입력하세요.");
  action("/api/session/start", { participant: el("participant").value, target_wh: target });
};
el("stop").onclick = () => action("/api/session/stop");
el("reset").onclick = async () => {
  if (await action("/api/session/reset")) { el("participant").value = ""; el("target").value = ""; }
};
el("real-mode").onclick = () => action("/api/mode", { mode: "ble" });
el("demo-mode").onclick = () => action("/api/mode", { mode: "demo" });
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
