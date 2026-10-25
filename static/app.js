"use strict";

// Photos are downscaled in the browser before upload: faster on slow links, and
// re-encoding drops EXIF metadata (location etc.) before anything leaves the device.
const MAX_SIDE = 1600;
const MAX_UPLOAD = 10 * 1024 * 1024;
const LOW_CONFIDENCE = 0.8;

const $ = (id) => document.getElementById(id);
const els = {
  cameraBtn: $("camera-btn"), uploadBtn: $("upload-btn"),
  fileInput: $("file-input"), captureInput: $("capture-input"),
  camera: $("camera"), video: $("video"), snap: $("snap-btn"), cameraCancel: $("camera-cancel"),
  preview: $("preview"), previewImg: $("preview-img"), clearPhoto: $("clear-photo"),
  ep: $("ep"), depth: $("depth"), go: $("go-btn"),
  fenForm: $("fen-form"), fenInput: $("fen-input"),
  status: $("status"), result: $("result"),
  bestMove: $("best-move"), bestUci: $("best-uci"), evalEl: $("eval"), confidence: $("confidence"),
  board: $("board"), flip: $("flip-btn"), copyFen: $("copy-fen"), fenOut: $("fen-out"),
};

let photo = null;        // Blob ready to upload (kept only in memory)
let previewUrl = null;
let stream = null;
let flipped = false;
let shown = null;        // {fen, best} currently drawn

// ---------- status ----------

function setStatus(msg, isError = false) {
  els.status.textContent = msg;
  els.status.classList.toggle("error", isError);
}

function setBusy(busy) {
  for (const b of [els.go, els.cameraBtn, els.uploadBtn]) b.disabled = busy;
  els.fenForm.querySelector("button").disabled = busy;
  if (!busy) els.go.disabled = !photo;
}

// ---------- photo input ----------

async function toUploadBlob(source) {
  try {
    const bmp = await createImageBitmap(source, { imageOrientation: "from-image" });
    const scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close();
    return await new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("encode failed"))), "image/jpeg", 0.9));
  } catch {
    // Browser can't decode it (e.g. HEIC outside Safari): send as-is and let the server decide.
    return source instanceof Blob ? source : null;
  }
}

async function setPhoto(source) {
  const blob = await toUploadBlob(source);
  if (!blob) return setStatus("Couldn't read that image.", true);
  if (blob.size > MAX_UPLOAD) return setStatus("Image is larger than 10 MB.", true);
  photo = blob;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  els.previewImg.src = previewUrl;
  els.preview.hidden = false;
  els.go.disabled = false;
  setStatus("Photo ready. Check the side to move, then find the best move.");
}

function clearPhoto() {
  photo = null;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  els.previewImg.removeAttribute("src");
  els.preview.hidden = true;
  els.go.disabled = true;
}

function onFileChosen(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";   // allow choosing the same file again
  if (file) setPhoto(file);
}

async function openCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.isSecureContext) {
    // No live camera API (e.g. plain http): fall back to the phone's camera app.
    els.captureInput.click();
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch {
    els.captureInput.click();
    return;
  }
  els.video.srcObject = stream;
  els.camera.hidden = false;
  await els.video.play().catch(() => {});
}

function closeCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  els.video.srcObject = null;
  els.camera.hidden = true;
}

async function snap() {
  const v = els.video;
  if (!v.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = v.videoWidth;
  canvas.height = v.videoHeight;
  canvas.getContext("2d").drawImage(v, 0, 0);
  closeCamera();
  await setPhoto(canvas);
}

// ---------- requests ----------

function castlingValue() {
  const checked = new Set([...document.querySelectorAll('input[name="castle"]:checked')].map((c) => c.value));
  return ["K", "Q", "k", "q"].filter((c) => checked.has(c)).join("") || "-";
}

function turnValue() {
  return document.querySelector('input[name="turn"]:checked').value;
}

async function request(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    throw { detail: "Couldn't reach the server." };
  }
  const body = await res.json().catch(() => ({ detail: `Server error (${res.status}).` }));
  if (!res.ok) throw body;
  return body;
}

async function analysePhoto() {
  if (!photo) return;
  const form = new FormData();
  form.append("image", photo, "board.jpg");
  form.append("turn", turnValue());
  form.append("castling", castlingValue());
  form.append("ep", els.ep.value);
  form.append("depth", els.depth.value);
  await run(() => request("move", { method: "POST", body: form }), "Reading the board…");
}

async function analyseFen(e) {
  e.preventDefault();
  const fen = els.fenInput.value.trim();
  if (!fen) return;
  const q = new URLSearchParams({ fen, depth: els.depth.value });
  await run(() => request(`move?${q}`), "Thinking…");
}

async function run(fn, busyMsg) {
  setBusy(true);
  setStatus(busyMsg);
  els.result.hidden = true;   // never leave an old answer next to a new request
  try {
    showResult(await fn());
    setStatus("");
  } catch (err) {
    setStatus(err.detail || "Something went wrong.", true);
    // A recognised but illegal position still comes back with its FEN: show it.
    if (err.fen) showResult({ ...err, best: null });
  } finally {
    setBusy(false);
  }
}

// ---------- result ----------

function formatEval(r) {
  if (r.mate != null) {
    const side = r.mate > 0 ? "White" : "Black";
    return `Mate in ${Math.abs(r.mate)} for ${side}`;
  }
  if (r.eval == null) return "";
  const v = r.eval;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const who = Math.abs(v) < 0.3 ? "about equal" : `${v > 0 ? "White" : "Black"} is better`;
  return `Evaluation ${sign}${Math.abs(v).toFixed(2)} (${who})`;
}

function showResult(r) {
  els.result.hidden = false;
  els.bestMove.textContent = r.best ? r.san || r.best : "—";
  els.bestUci.textContent = r.best && r.san ? r.best : "";
  els.evalEl.textContent = r.best ? formatEval(r) + (r.depth ? ` · depth ${r.depth}` : "") : "";
  if (r.confidence != null) {
    const pct = Math.round(r.confidence * 100);
    const low = r.confidence < LOW_CONFIDENCE;
    els.confidence.textContent = `Recognition confidence: ${pct}%` +
      (low ? ". Check the board below against your photo." : ". Check the board below matches your photo.");
    els.confidence.classList.toggle("low", low);
    els.confidence.hidden = false;
  } else {
    els.confidence.hidden = true;
  }
  els.fenOut.textContent = r.fen;
  flipped = r.fen.split(" ")[1] === "b";
  shown = { fen: r.fen, best: r.best };
  drawBoard();
  els.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- board drawing ----------

const SVG_NS = "http://www.w3.org/2000/svg";
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const SQ = 100;

function svg(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

function parsePlacement(fen) {
  const rows = fen.split(" ")[0].split("/");
  const grid = [];   // grid[rank 0..7 from 8 down][file 0..7]
  for (const row of rows) {
    const r = [];
    for (const ch of row) {
      if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) r.push(null);
      else r.push(ch);
    }
    grid.push(r);
  }
  return grid;
}

// Screen position (top-left, in SVG units) of a square given file 0-7 and row 0-7 (row 0 = rank 8).
function pos(file, row) {
  return flipped ? [(7 - file) * SQ, (7 - row) * SQ] : [file * SQ, row * SQ];
}

function squareOf(name) {
  return [name.charCodeAt(0) - 97, 8 - Number(name[1])];
}

function drawBoard() {
  const g = els.board;
  g.replaceChildren();
  if (!shown) return;
  const grid = parsePlacement(shown.fen);
  const move = shown.best ? [squareOf(shown.best.slice(0, 2)), squareOf(shown.best.slice(2, 4))] : [];

  for (let row = 0; row < 8; row++) {
    for (let file = 0; file < 8; file++) {
      const [x, y] = pos(file, row);
      const light = (file + row) % 2 === 0;
      g.append(svg("rect", { x, y, width: SQ, height: SQ, class: light ? "light" : "dark" }));
      if (move.some(([f, r]) => f === file && r === row)) {
        g.append(svg("rect", { x, y, width: SQ, height: SQ, class: "hl" }));
      }
    }
  }
  // Coordinates along the bottom and left edges, in the opposite colour of their square.
  // (A square's colour depends only on its screen position, flipped or not.)
  const ink = (light) => `var(${light ? "--sq-dark" : "--sq-light"})`;
  for (let i = 0; i < 8; i++) {
    const file = flipped ? 7 - i : i;
    const row = flipped ? 7 - i : i;
    g.append(svg("text", { x: i * SQ + SQ - 6, y: 8 * SQ - 8, "text-anchor": "end", class: "coord",
      fill: ink((i + 7) % 2 === 0) }, "abcdefgh"[file]));
    g.append(svg("text", { x: 6, y: i * SQ + 22, class: "coord", fill: ink(i % 2 === 0) }, String(8 - row)));
  }
  for (let row = 0; row < 8; row++) {
    for (let file = 0; file < 8; file++) {
      const p = grid[row] && grid[row][file];
      if (!p) continue;
      const [x, y] = pos(file, row);
      // U+FE0E asks for the text (not emoji) form of the glyph, mainly for iOS.
      g.append(svg("text", { x: x + SQ / 2, y: y + SQ / 2 + 4, "text-anchor": "middle",
        "dominant-baseline": "central", class: `piece ${p === p.toUpperCase() ? "w" : "b"}` },
        GLYPH[p.toLowerCase()] + "︎"));
    }
  }
  if (move.length) drawArrow(g, move[0], move[1]);
}

function drawArrow(g, from, to) {
  const [x1, y1] = pos(...from).map((v) => v + SQ / 2);
  const [x2, y2] = pos(...to).map((v) => v + SQ / 2);
  const len = Math.hypot(x2 - x1, y2 - y1);
  const ux = (x2 - x1) / len, uy = (y2 - y1) / len;
  const head = 38, half = 24;
  const bx = x2 - ux * head, by = y2 - uy * head;
  g.append(svg("line", { x1, y1, x2: bx, y2: by, class: "arrow" }));
  g.append(svg("polygon", { class: "arrowhead",
    points: `${x2},${y2} ${bx - uy * half},${by + ux * half} ${bx + uy * half},${by - ux * half}` }));
}

// ---------- wiring ----------

els.uploadBtn.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", onFileChosen);
els.captureInput.addEventListener("change", onFileChosen);
els.cameraBtn.addEventListener("click", openCamera);
els.snap.addEventListener("click", snap);
els.cameraCancel.addEventListener("click", closeCamera);
els.clearPhoto.addEventListener("click", clearPhoto);
els.go.addEventListener("click", analysePhoto);
els.fenForm.addEventListener("submit", analyseFen);
els.flip.addEventListener("click", () => { flipped = !flipped; drawBoard(); });
els.copyFen.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(els.fenOut.textContent);
    setStatus("FEN copied.");
  } catch {
    setStatus("Couldn't copy. Select the FEN text instead.", true);
  }
});
