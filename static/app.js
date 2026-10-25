"use strict";

// Photos are downscaled in the browser before upload: faster on slow links, and
// re-encoding drops EXIF metadata (location etc.) before anything leaves the device.
const MAX_SIDE = 1600;
const MAX_UPLOAD = 10 * 1024 * 1024;
const LOW_CONFIDENCE = 0.8;
const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR";

const $ = (id) => document.getElementById(id);
const els = {
  cameraBtn: $("camera-btn"), uploadBtn: $("upload-btn"),
  fileInput: $("file-input"), captureInput: $("capture-input"),
  camera: $("camera"), video: $("video"), snap: $("snap-btn"), cameraCancel: $("camera-cancel"),
  preview: $("preview"), previewImg: $("preview-img"), clearPhoto: $("clear-photo"), overlay: $("overlay"),
  ep: $("ep"), depth: $("depth"), go: $("go-btn"),
  fenForm: $("fen-form"), fenInput: $("fen-input"), manual: $("manual-btn"),
  status: $("status"), result: $("result"),
  reviewHead: $("review-head"), reviewTitle: $("review-title"), answer: $("answer"),
  bestMove: $("best-move"), bestUci: $("best-uci"), evalEl: $("eval"),
  confidence: $("confidence"), repairs: $("repairs"),
  photoRef: $("photo-ref"), photoRefWrap: $("photo-ref-wrap"),
  board: $("board"), rotate: $("rotate-btn"), flip: $("flip-btn"), reset: $("reset-btn"),
  moveBtn: $("move-btn"), fenOut: $("fen-out"), copyFen: $("copy-fen"),
  showDiagram: $("show-diagram"), diagram: $("diagram"), diagramImg: $("diagram-img"), errorImg: $("error-img"),
  picker: $("picker"), pickerTitle: $("picker-title"), pickerGrid: $("picker-grid"), pickerCancel: $("picker-cancel"),
};

let photo = null;        // Blob ready to upload (kept only in memory)
let previewUrl = null;
let stream = null;

// The position being shown/edited.
const state = {
  grid: null,            // grid[row 0..7, rank 8 first][file 0..7] = piece letter or null
  detected: null,        // grid as recognised, for Reset
  uncertain: new Set(),  // squares the model was unsure about and the user hasn't touched
  alternatives: {},      // square -> [[piece, prob], ...] from the model
  answers: new Map(),    // FEN -> engine result, so undoing an edit needs no new search
  revealedFen: null,     // the answer is shown only while the position still equals this
  fromPhoto: false,
  flipped: false,
  picking: null,         // square whose piece is being chosen
};

// ---------- status ----------

function setStatus(msg, isError = false) {
  els.status.textContent = msg;
  els.status.classList.toggle("error", isError);
}

// Error diagram (from the server's image_url, or built for GET errors) under the status line.
function showErrorImage(url) {
  els.errorImg.hidden = !url || !els.showDiagram.checked;
  if (url && els.showDiagram.checked) els.errorImg.src = url;
  else els.errorImg.removeAttribute("src");
}

function setBusy(busy) {
  for (const b of [els.go, els.cameraBtn, els.uploadBtn, els.moveBtn]) b.disabled = busy;
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
  els.overlay.replaceChildren();
  els.previewImg.src = previewUrl;
  els.preview.hidden = false;
  els.go.disabled = false;
  setStatus("Photo ready. Check the side to move, then read the board.");
}

function clearPhoto() {
  photo = null;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  els.previewImg.removeAttribute("src");
  els.overlay.replaceChildren();
  els.preview.hidden = true;
  els.photoRef.hidden = true;
  els.photoRefWrap.replaceChildren();
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

// ---------- position helpers ----------

function parsePlacement(placement) {
  return placement.split("/").map((row) => {
    const r = [];
    for (const ch of row) {
      if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) r.push(null);
      else r.push(ch);
    }
    return r;
  });
}

function toPlacement(grid) {
  return grid.map((row) => {
    let out = "", run = 0;
    for (const p of row) {
      if (!p) { run++; continue; }
      if (run) { out += run; run = 0; }
      out += p;
    }
    return out + (run || "");
  }).join("/");
}

const squareName = (file, row) => "abcdefgh"[file] + (8 - row);
const squareOf = (name) => [name.charCodeAt(0) - 97, 8 - Number(name[1])];

// Rotating the position 90° clockwise: new[row][file] = old[7 - file][row].
function rotateGrid(grid) {
  return grid.map((_, row) => grid.map((_, file) => grid[7 - file][row]));
}
function rotateSquare(name) {
  const [file, row] = squareOf(name);
  return squareName(7 - row, file);   // old (file, row) -> new (7 - row, file)
}

function castlingValue() {
  const checked = new Set([...document.querySelectorAll('input[name="castle"]:checked')].map((c) => c.value));
  return ["K", "Q", "k", "q"].filter((c) => checked.has(c)).join("") || "-";
}

// Castling needs the king and rook on their home squares: disable boxes that can't apply.
function updateCastlingBoxes() {
  const g = state.grid;
  const ok = {
    K: g && g[7][4] === "K" && g[7][7] === "R", Q: g && g[7][4] === "K" && g[7][0] === "R",
    k: g && g[0][4] === "k" && g[0][7] === "r", q: g && g[0][4] === "k" && g[0][0] === "r",
  };
  for (const box of document.querySelectorAll('input[name="castle"]')) {
    const allowed = !g || ok[box.value];
    box.disabled = !allowed;
    if (!allowed) box.checked = false;
  }
}

function turnValue() {
  return document.querySelector('input[name="turn"]:checked').value;
}

function currentFen() {
  const turn = turnValue();
  const ep = els.ep.value ? els.ep.value + (turn === "w" ? "6" : "3") : "-";
  return `${toPlacement(state.grid)} ${turn} ${castlingValue()} ${ep} 0 1`;
}

// ---------- requests ----------

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

async function readPhoto() {
  if (!photo) return;
  const form = new FormData();
  form.append("image", photo, "board.jpg");
  form.append("turn", turnValue());
  form.append("castling", castlingValue());
  form.append("ep", els.ep.value);
  form.append("depth", els.depth.value);
  if (els.showDiagram.checked) form.append("img_out", "true");
  setBusy(true);
  setStatus("Reading the board…");
  showErrorImage(null);
  els.result.hidden = true;   // never leave an old answer next to a new request
  try {
    loadDetection(await request("move", { method: "POST", body: form }));
    setStatus("");
  } catch (err) {
    if (err.detected) {
      // Recognised, but not a legal position as-is: let the user fix it.
      loadDetection(err);
      setStatus(`${err.detail}. Fix the board below.`, true);
    } else {
      setStatus(err.detail || "Something went wrong.", true);
    }
    showErrorImage(err.image_url);
  } finally {
    setBusy(false);
  }
}

async function getMove() {
  const fen = currentFen();
  if (state.answers.has(fen)) return showAnswer();
  setBusy(true);
  setStatus("Thinking…");
  showErrorImage(null);
  try {
    const r = await request(`move?${new URLSearchParams({ fen, depth: els.depth.value })}`);
    state.answers.set(fen, r);
    showAnswer();
    setStatus("");
  } catch (err) {
    setStatus(err.detail || "Something went wrong.", true);
    if (err.error) showErrorImage(`img/error?${new URLSearchParams({ code: err.error, fen })}`);
  } finally {
    setBusy(false);
  }
}

async function analyseFen(e) {
  e.preventDefault();
  const fen = els.fenInput.value.trim();
  if (!fen) return;
  setBusy(true);
  setStatus("Thinking…");
  try {
    const r = await request(`move?${new URLSearchParams({ fen, depth: els.depth.value })}`);
    // Mirror the FEN's side to move, castling and en passant into the controls so edits keep them.
    const [placement, turn, castling, ep] = r.fen.split(" ");
    document.querySelector(`input[name="turn"][value="${turn}"]`).checked = true;
    for (const box of document.querySelectorAll('input[name="castle"]')) box.checked = castling.includes(box.value);
    els.ep.value = ep === "-" ? "" : ep[0];
    loadPosition(placement);
    state.answers.set(currentFen(), r);
    showAnswer();
    setStatus("");
  } catch (err) {
    setStatus(err.detail || "Something went wrong.", true);
  } finally {
    setBusy(false);
  }
}

// ---------- result card ----------

function loadPosition(placement, extras = {}) {
  state.grid = parsePlacement(placement);
  state.detected = parsePlacement(placement);
  state.uncertain = new Set(extras.uncertain || []);
  state.alternatives = extras.alternatives || {};
  state.answers = new Map();
  state.revealedFen = null;
  state.fromPhoto = Boolean(extras.detected);
  state.flipped = turnValue() === "b";
  els.confidence.hidden = true;
  els.repairs.hidden = true;
  els.photoRef.hidden = true;
  els.result.hidden = false;
}

function loadDetection(r) {
  loadPosition(r.detected, r);
  // A 200 response already carries the best move for exactly this position.
  if (r.best) state.answers.set(r.fen, r);
  showConfidence(r);
  showRepairs(r.repairs || []);
  drawOverlay(r.corners, r.image_size);
  showPhotoRef();
  render();
  els.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

function showConfidence(r) {
  const pct = Math.round(r.confidence * 100);
  const low = r.confidence < LOW_CONFIDENCE;
  const unsure = (r.uncertain || []).length;
  els.confidence.textContent = `Recognition confidence: ${pct}%. ` +
    (low ? "Likely mistakes: compare the board with your photo carefully." : "Check it matches your photo.") +
    (unsure ? ` Dashed squares (${unsure}) are the least certain.` : "");
  els.confidence.classList.toggle("low", low);
  els.confidence.hidden = false;
}

function showRepairs(repairs) {
  const names = { pawn_on_back_rank: "no pawns on the back rank", extra_king: "only one king per side",
    missing_king: "each side needs a king", too_many_pawns: "at most 8 pawns per side" };
  const label = (p) => (p === "." ? "empty" : p);
  els.repairs.textContent = repairs.length
    ? "Changed to make the position legal: " +
      repairs.map((x) => `${x.square} ${label(x.from)}→${label(x.to)} (${names[x.reason] || x.reason})`).join(", ") + "."
    : "";
  els.repairs.hidden = !repairs.length;
}

// A small copy of the photo (with the detected outline) next to the board for comparison.
function showPhotoRef() {
  els.photoRefWrap.replaceChildren();
  if (!previewUrl) return;
  const img = document.createElement("img");
  img.src = previewUrl;
  img.alt = "Your photo, with the detected board outlined";
  els.photoRefWrap.append(img, els.overlay.cloneNode(true));
  els.photoRef.hidden = false;
}

function showAnswer() {
  state.revealedFen = currentFen();
  render();
  els.answer.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function answerMatches() {
  return state.answers.has(currentFen());
}

function render() {
  if (!state.grid) return;
  updateCastlingBoxes();
  const showing = answerMatches() && state.revealedFen === currentFen();
  const r = state.answers.get(currentFen());
  els.answer.hidden = !showing;
  els.reviewHead.hidden = showing;
  els.reviewTitle.textContent = state.fromPhoto ? "Check the detected position" : "Edit the position";
  els.moveBtn.hidden = showing;
  if (showing) {
    els.bestMove.textContent = r.san || r.best;
    els.bestUci.textContent = r.san ? r.best : "";
    els.evalEl.textContent = formatEval(r) + (r.depth ? ` · depth ${r.depth}` : "");
  }
  els.reset.hidden = toPlacement(state.grid) === toPlacement(state.detected);
  els.fenOut.textContent = currentFen();
  drawBoard(showing ? r.best : null);
  showDiagram(showing ? r : null);
}

// The server-drawn diagram: the image_url from the photo request, or the same /img link
// built here for positions analysed after edits.
function showDiagram(r) {
  const on = Boolean(r) && els.showDiagram.checked;
  els.diagram.hidden = !on;
  if (!on) return;
  const params = { fen: r.fen, best: r.best };
  if (r.mate != null) params.mate = r.mate;
  else if (r.eval != null) params.eval = r.eval;
  const url = r.image_url || `img?${new URLSearchParams(params)}`;
  if (els.diagramImg.getAttribute("src") !== url) els.diagramImg.src = url;
}

function formatEval(r) {
  if (r.mate != null) return `Mate in ${Math.abs(r.mate)} for ${r.mate > 0 ? "White" : "Black"}`;
  if (r.eval == null) return "";
  const v = r.eval;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const who = Math.abs(v) < 0.3 ? "about equal" : `${v > 0 ? "White" : "Black"} is better`;
  return `Evaluation ${sign}${Math.abs(v).toFixed(2)} (${who})`;
}

// ---------- board drawing ----------

const SVG_NS = "http://www.w3.org/2000/svg";
const SQ = 100;

function svg(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

function pieceEl(p, x, y, size = SQ) {
  return svg("path", { d: PIECE_PATHS[p.toLowerCase()], class: `pc ${p === p.toUpperCase() ? "pc-w" : "pc-b"}`,
    transform: `translate(${x} ${y}) scale(${size / 100})` });
}

// Screen position (top-left, in SVG units) of a square given file 0-7 and row 0-7 (row 0 = rank 8).
function pos(file, row) {
  return state.flipped ? [(7 - file) * SQ, (7 - row) * SQ] : [file * SQ, row * SQ];
}

function drawBoard(best) {
  const g = els.board;
  g.replaceChildren();
  const move = best ? [squareOf(best.slice(0, 2)), squareOf(best.slice(2, 4))] : [];
  const picking = state.picking && squareOf(state.picking);

  for (let row = 0; row < 8; row++) {
    for (let file = 0; file < 8; file++) {
      const [x, y] = pos(file, row);
      g.append(svg("rect", { x, y, width: SQ, height: SQ, class: (file + row) % 2 === 0 ? "light" : "dark" }));
      if (move.some(([f, r]) => f === file && r === row)) g.append(svg("rect", { x, y, width: SQ, height: SQ, class: "hl" }));
    }
  }
  // Coordinates along the bottom and left edges, in the opposite colour of their square.
  // (A square's colour depends only on its screen position, flipped or not.)
  const ink = (light) => `var(${light ? "--sq-dark" : "--sq-light"})`;
  for (let i = 0; i < 8; i++) {
    const file = state.flipped ? 7 - i : i;
    const row = state.flipped ? 7 - i : i;
    g.append(svg("text", { x: i * SQ + SQ - 6, y: 8 * SQ - 8, "text-anchor": "end", class: "coord",
      fill: ink((i + 7) % 2 === 0) }, "abcdefgh"[file]));
    g.append(svg("text", { x: 6, y: i * SQ + 22, class: "coord", fill: ink(i % 2 === 0) }, String(8 - row)));
  }
  for (let row = 0; row < 8; row++) {
    for (let file = 0; file < 8; file++) {
      const p = state.grid[row][file];
      if (p) g.append(pieceEl(p, ...pos(file, row)));
    }
  }
  for (const name of state.uncertain) {
    const [x, y] = pos(...squareOf(name));
    g.append(svg("rect", { x: x + 5, y: y + 5, width: SQ - 10, height: SQ - 10, rx: 8, class: "unsure" }));
  }
  if (picking) {
    const [x, y] = pos(...picking);
    g.append(svg("rect", { x: x + 3, y: y + 3, width: SQ - 6, height: SQ - 6, class: "picking" }));
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

// Outline where the board was found in the photo, with its a8 and h1 corners labelled.
let overlayData = null;
function drawOverlay(corners, size) {
  overlayData = corners && size ? { corners, size } : null;
  const o = els.overlay;
  o.replaceChildren();
  if (!overlayData || els.preview.hidden) return;
  o.setAttribute("viewBox", `0 0 ${size[0]} ${size[1]}`);
  o.setAttribute("preserveAspectRatio", "none");
  o.append(svg("polygon", { points: corners.map((p) => p.join(",")).join(" ") }));
  const r = Math.max(size[0], size[1]) / 120;
  for (const [i, label] of [[0, "a8"], [2, "h1"]]) {
    const [x, y] = corners[i];
    o.append(svg("circle", { cx: x, cy: y, r }));
    o.append(svg("text", { x: x + r * 1.5, y: y - r * 1.5, "font-size": r * 4 }, label));
  }
}

// ---------- editing ----------

function squareFromEvent(e) {
  const box = els.board.getBoundingClientRect();
  const sx = Math.floor(((e.clientX - box.left) / box.width) * 8);
  const sy = Math.floor(((e.clientY - box.top) / box.height) * 8);
  if (sx < 0 || sx > 7 || sy < 0 || sy > 7) return null;
  return state.flipped ? squareName(7 - sx, 7 - sy) : squareName(sx, sy);
}

const PICKER_ORDER = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p", "."];
const PIECE_NAMES = { k: "king", q: "queen", r: "rook", b: "bishop", n: "knight", p: "pawn" };
const pieceLabel = (p) => (p === "." ? "Empty" : `${p === p.toUpperCase() ? "White" : "Black"} ${PIECE_NAMES[p.toLowerCase()]}`);

function openPicker(name) {
  state.picking = name;
  const [file, row] = squareOf(name);
  const current = state.grid[row][file] || ".";
  const guesses = Object.fromEntries((state.alternatives[name] || []).map(([p, prob]) => [p, prob]));
  els.pickerTitle.textContent = `${name}: ${pieceLabel(current).toLowerCase()}`;
  els.pickerGrid.replaceChildren();
  for (const p of PICKER_ORDER) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pick" + (p === current ? " current" : "") + (p in guesses && p !== current ? " guess" : "");
    b.setAttribute("aria-label", pieceLabel(p) + (p in guesses ? `, model ${Math.round(guesses[p] * 100)}%` : ""));
    const icon = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
    if (p === ".") icon.append(svg("path", { d: "M30 30L70 70M70 30L30 70", class: "empty-x" }));
    else icon.append(pieceEl(p, 0, 0));
    b.append(icon);
    if (p in guesses && p !== current) b.append(Object.assign(document.createElement("span"), { textContent: `${Math.round(guesses[p] * 100)}%` }));
    b.addEventListener("click", () => setPiece(name, p === "." ? null : p));
    els.pickerGrid.append(b);
  }
  els.picker.hidden = false;
  drawBoard(null);
  els.pickerGrid.querySelector(".current").focus();
}

function closePicker() {
  els.picker.hidden = true;
  state.picking = null;
  render();
}

function setPiece(name, piece) {
  const [file, row] = squareOf(name);
  state.grid[row][file] = piece;
  state.uncertain.delete(name);
  closePicker();
}

function rotatePosition() {
  state.grid = rotateGrid(state.grid);
  state.detected = rotateGrid(state.detected);
  state.uncertain = new Set([...state.uncertain].map(rotateSquare));
  state.alternatives = Object.fromEntries(Object.entries(state.alternatives).map(([k, v]) => [rotateSquare(k), v]));
  if (overlayData) {
    // New a8, h8, h1, a1 are the old a1, a8, h8, h1.
    const c = overlayData.corners;
    drawOverlay([c[3], c[0], c[1], c[2]], overlayData.size);
    showPhotoRef();
  }
  render();
}

function resetPosition() {
  state.grid = state.detected.map((row) => row.slice());
  render();
}

function setUpByHand() {
  loadPosition(START);
  for (const box of document.querySelectorAll('input[name="castle"]')) box.checked = true;
  document.querySelector('input[name="turn"][value="w"]').checked = true;
  state.flipped = false;
  updateCastlingBoxes();
  render();
  setStatus("Tap squares to set up the position, then get the best move.");
  els.result.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- wiring ----------

els.uploadBtn.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", onFileChosen);
els.captureInput.addEventListener("change", onFileChosen);
els.cameraBtn.addEventListener("click", openCamera);
els.snap.addEventListener("click", snap);
els.cameraCancel.addEventListener("click", closeCamera);
els.clearPhoto.addEventListener("click", clearPhoto);
els.go.addEventListener("click", readPhoto);
els.fenForm.addEventListener("submit", analyseFen);
els.manual.addEventListener("click", setUpByHand);
els.board.addEventListener("click", (e) => {
  const name = state.grid && squareFromEvent(e);
  if (name) openPicker(name);
});
els.pickerCancel.addEventListener("click", closePicker);
els.picker.addEventListener("click", (e) => { if (e.target === els.picker) closePicker(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !els.picker.hidden) closePicker(); });
els.rotate.addEventListener("click", rotatePosition);
els.flip.addEventListener("click", () => { state.flipped = !state.flipped; render(); });
els.reset.addEventListener("click", resetPosition);
els.moveBtn.addEventListener("click", getMove);
// Side to move, castling and en passant are part of the position: re-render when they change.
for (const el of document.querySelectorAll('input[name="turn"], input[name="castle"], #ep')) {
  el.addEventListener("change", () => { if (state.grid) render(); });
}
els.showDiagram.addEventListener("change", () => {
  if (!els.showDiagram.checked) showErrorImage(null);
  render();
});
els.copyFen.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(els.fenOut.textContent);
    setStatus("FEN copied.");
  } catch {
    setStatus("Couldn't copy. Select the FEN text instead.", true);
  }
});
