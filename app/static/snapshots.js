"use strict";

// Value snapshots: save the current values (all tags or the filtered ones), apply,
// download, import and delete.

async function openSnapshots() {
  const total = state.rows.length;
  const shown = state.matches ? state.matches.length : total;
  $("snapAllLabel").textContent = `All ${total.toLocaleString()} tags`;
  $("snapFilterLabel").textContent = `The ${shown.toLocaleString()} tag${shown === 1 ? "" : "s"} shown by the current filter`;
  $("snapFilterRadio").disabled = !filterActive() || shown === 0;
  if ($("snapFilterRadio").disabled) document.querySelector('input[name="snapScope"][value="all"]').checked = true;
  $("snapDialog").showModal();
  await loadSnapshots();
}

async function loadSnapshots() {
  let list = [];
  try { list = await api("/api/snapshots"); } catch (e) { toast(e.message, true); }
  const rows = list.map((s) => {
    const tr = document.createElement("tr");
    const name = el("td");
    name.append(el("div", null, s.name));
    if (s.note) name.append(el("div", "muted small", s.note));
    const saved = el("td", "muted", new Date(s.created * 1000).toLocaleString());
    const count = el("td", "num-col", (s.count || 0).toLocaleString());
    const from = el("td", "muted", [s.project, s.mode === "plc" ? "PLC" : "file"].filter(Boolean).join(" · "));
    const actions = el("td");
    const box = el("div", "snap-actions");
    const apply = el("button", "btn small primary", "Apply");
    apply.addEventListener("click", () => applySnapshot(s));
    const dl = el("a", "btn small", "Download");
    dl.href = `/api/snapshots/${encodeURIComponent(s.id)}`;
    dl.download = `${s.id}.json`;
    const del = el("button", "btn small danger", "Delete");
    del.addEventListener("click", () => deleteSnapshot(s));
    box.append(apply, dl, del);
    actions.append(box);
    tr.append(name, saved, count, from, actions);
    return tr;
  });
  $("snapRows").replaceChildren(...rows);
  $("snapEmpty").hidden = rows.length > 0;
}

async function saveSnapshot(e) {
  e.preventDefault();
  const scope = document.querySelector('input[name="snapScope"]:checked').value;
  const body = { name: $("snapName").value.trim() };
  if (scope === "filter") body.paths = state.matches.map((r) => r[P]);
  try {
    const s = await api("/api/snapshots", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    toast(`Saved "${s.name}" (${s.count.toLocaleString()} tags)`);
    $("snapName").value = "";
    await loadSnapshots();
  } catch (err) {
    toast(err.message, true);
  }
}

async function applySnapshot(s) {
  const plc = state.status && state.status.mode === "plc";
  if (!confirm(`Apply "${s.name}"?\n\nWritable tags whose value differs from the snapshot are written` +
      (plc ? " to the PLC." : ".") + " Read-only tags are skipped.")) return;
  try {
    const r = await api(`/api/snapshots/${encodeURIComponent(s.id)}/apply`, { method: "POST" });
    const skipped = [
      r.unchanged && `${r.unchanged.toLocaleString()} already equal`,
      r.read_only && `${r.read_only.toLocaleString()} read-only`,
      r.missing && `${r.missing.toLocaleString()} not in this program`,
      r.invalid && `${r.invalid.toLocaleString()} invalid`,
    ].filter(Boolean).join(", ");
    toast(`"${r.name}": ${r.written.toLocaleString()} written` + (skipped ? `; skipped ${skipped}` : ""));
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteSnapshot(s) {
  if (!confirm(`Delete snapshot "${s.name}"? This can't be undone.`)) return;
  try {
    await api(`/api/snapshots/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    await loadSnapshots();
  } catch (err) {
    toast(err.message, true);
  }
}

async function importSnapshot(file) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const s = await api("/api/snapshots/import", { method: "POST", body: fd });
    toast(`Imported "${s.name}" (${s.count.toLocaleString()} tags)`);
    await loadSnapshots();
  } catch (err) {
    toast(err.message, true);
  }
}

$("snapBtn").addEventListener("click", openSnapshots);
$("snapClose").addEventListener("click", () => $("snapDialog").close());
$("snapForm").addEventListener("submit", saveSnapshot);
$("snapImport").addEventListener("change", (e) => {
  if (e.target.files[0]) importSnapshot(e.target.files[0]);
  e.target.value = "";
});
