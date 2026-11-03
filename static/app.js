"use strict";

const $ = (id) => document.getElementById(id);
const state = {csrf:"", partitions:[], destinations:[], stack:[], selection:new Map(), preview:null, busy:false, listing:0, rates:new Map()};
const fmt = (size) => {
  if (!size) return "0 o";
  const units = ["o","Kio","Mio","Gio","Tio"];
  let n = Number(size), i = 0;
  while (n >= 1024 && i < units.length-1) {n/=1024; i++;}
  return n.toLocaleString("fr-FR",{maximumFractionDigits:1}) + " " + units[i];
};

async function api(url, body) {
  const options = {cache:"no-store"};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type":"application/json", "X-Migration-Token":state.csrf};
    options.body = JSON.stringify(body);
  }
  const result = await fetch(url,options);
  let data;
  try {data = await result.json();}
  catch (_) {throw new Error("Réponse du serveur invalide");}
  if (!result.ok) throw new Error(data.detail || "Erreur de connexion");
  return data;
}

function notice(message) {
  $("message").hidden = !message;
  $("message").textContent = message || "";
}

function node(tag, className, txt) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (txt !== undefined) el.textContent = txt;
  return el;
}

function invalidate() {
  state.preview = null;
  $("preview-box").hidden = true;
  $("queue").disabled = true;
  $("preview").disabled = !state.selection.size || state.busy;
}

function selectedUI() {
  const list = $("selected-list");
  list.replaceChildren();
  $("selected-count").textContent = String(state.selection.size);
  if (!state.selection.size) {
    list.append(node("div","empty small","Cochez des fichiers ou dossiers dans l’explorateur."));
  }
  for (const [path, entry] of state.selection) {
    const item = node("div","selected-item");
    item.append(node("span", "", (entry.type === "dir" ? "📁 " : "📄 ") + entry.name));
    const remove = node("button","","×");
    remove.type = "button";remove.title = "Retirer de la sélection";
    remove.addEventListener("click",()=>{state.selection.delete(path);selectedUI();invalidate();renderEntries();});
    item.append(remove);list.append(item);
  }
  invalidate();
}

function renderEntries() {
  const entries = $("entries");
  if (!entries._items) return;
  entries.replaceChildren();
  const items = entries._items;
  $("entrycount").textContent = items.length + " élément" + (items.length > 1 ? "s" : "");
  if (!items.length) entries.append(node("div","empty","Dossier vide"));
  for (const entry of items) {
    const row = node("div","entry");
    const first = node("div","entry-main");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selection.has(entry.path);
    checkbox.setAttribute("aria-label", "Sélectionner " + entry.name);
    checkbox.addEventListener("change",()=>{
      if (checkbox.checked) state.selection.set(entry.path,entry);
      else state.selection.delete(entry.path);
      selectedUI();
    });
    first.append(checkbox);
    const file = node(entry.type === "dir" ? "button" : "div",
      "entry-name" + (entry.type === "dir" ? " folder" : ""));
    if (entry.type === "dir") {
      file.type = "button";file.title = "Ouvrir le dossier";
      file.addEventListener("click",()=>openFolder(entry));
    }
    file.append(node("span","entry-icon",entry.type === "dir" ? "▣" : "□"));
    file.append(node("span","",entry.name));
    first.append(file);
    row.append(first, node("span","entry-size",entry.type === "dir" ? "—" : fmt(entry.size)));
    entries.append(row);
  }
}

async function openFolder(entry, reset=false) {
  if (reset) state.stack = [];
  state.stack.push({name:entry.name,path:entry.path});
  const current = ++state.listing;
  const entries = $("entries");
  entries._items = null;
  entries.replaceChildren(node("div","empty","Lecture du dossier…"));
  breadcrumb();
  try {
    const response = await api("/api/list?path=" + encodeURIComponent(entry.path));
    if (current !== state.listing) return;
    entries._items = response.entries;
    renderEntries();
  } catch (err) {
    if (current !== state.listing) return;
    entries.replaceChildren(node("div","empty","Impossible de lire ce dossier"));
    notice(err.message);
  }
}

function breadcrumb() {
  $("crumbs").textContent = state.stack.map(x=>x.name).join(" / ");
  $("back").disabled = state.stack.length <= 1;
}

async function goBack() {
  if (state.stack.length <= 1) return;
  state.stack.pop();
  const last = state.stack.pop();
  await openFolder(last);
}

function targetOptions() {
  const select = $("destination");
  const byParent = new Map();
  for (const f of state.destinations) {
    const key = f.parent_id || "";
    if (!byParent.has(key)) byParent.set(key,[]);
    byParent.get(key).push(f);
  }
  select.replaceChildren();
  const seen = new Set();
  const walk = (folder, prefix, depth) => {
    if (seen.has(folder.id) || depth > 60) return;
    seen.add(folder.id);
    const option = node("option","",prefix + folder.name);
    option.value = folder.id;
    select.append(option);
    for (const child of byParent.get(folder.id) || []) {
      if (child.id !== folder.id) walk(child,prefix + "　",depth+1);
    }
  };
  const root = state.destinations.find(x=>x.id === "root");
  if (root) walk(root,"",0);
  for (const folder of state.destinations) if (!seen.has(folder.id)) walk(folder,"",0);
}

function setBusy(value) {
  state.busy = value;
  $("preview").disabled = value || !state.selection.size;
  $("queue").disabled = value || !state.preview;
  $("clear").disabled = value;
}

async function preview() {
  if (!state.selection.size) return;
  notice("");
  state.preview = null;
  setBusy(true);
  try {
    const data = await api("/api/preview",{
      paths:[...state.selection.keys()],target:$("destination").value
    });
    state.preview = data.plan_id;
    $("preview-box").hidden = false;
    $("preview-count").textContent = data.files.toLocaleString("fr-FR");
    $("preview-size").textContent = fmt(data.bytes);
    $("preview-folders").textContent = data.folders + " dossier(s) sélectionné(s) ou inclus.";
    $("preview-examples").textContent = data.examples.join(" · ");
  } catch(err) {notice(err.message);}
  finally {setBusy(false);}
}

async function queue() {
  if (!state.preview) return;
  notice("");
  setBusy(true);
  try {
    const data = await api("/api/queue",{plan_id:state.preview});
    state.selection.clear();
    selectedUI();
    renderEntries();
    notice(data.queued + " fichier(s) ajouté(s) à la file de transfert. La copie démarre automatiquement.");
    await refresh();
  } catch(err) {notice(err.message);}
  finally {setBusy(false);}
}

function statusName(s) {
  return ({queued:"En attente",running:"En cours",done:"Terminé",error:"Erreur",paused:"En pause"})[s] || s;
}

async function jobAction(id,action) {
  try {await api("/api/jobs/"+encodeURIComponent(id)+"/action",{action});await refresh();}
  catch(err){notice(err.message);}
}

function renderJobs(data) {
  const all = data.jobs || [];
  const summary = $("transfer-summary");
  const jobs = $("jobs");
  const done = all.filter(j=>j.state === "done");
  const errors = all.filter(j=>j.state === "error");
  const active = all.filter(j=>j.state === "running");
  const queued = all.filter(j=>j.state === "queued");
  const stats = data.stats || {};
  const count = stats.count === undefined ? all.length : stats.count;
  const total = stats.bytes === undefined ? all.reduce((n,j)=>n+j.size,0) : stats.bytes;
  const complete = stats.transferred === undefined ? all.reduce((n,j)=>n+Math.min(j.transferred,j.size),0) : stats.transferred;
  summary.textContent = count ? count + " fichiers · " + fmt(complete) + " / " + fmt(total) +
    " · " + (stats.done === undefined ? done.length : stats.done) + " terminés · " +
    (stats.running === undefined ? active.length : stats.running) + " en cours · " +
    (stats.queued === undefined ? queued.length : stats.queued) + " en attente" +
    ((stats.error || errors.length) ? " · " + (stats.error === undefined ? errors.length : stats.error) + " erreurs" : "") :
    "Aucun transfert pour le moment.";
  jobs.replaceChildren();
  if (!all.length) jobs.append(node("div","empty small","Les transferts apparaîtront ici après confirmation."));
  for (const job of all) {
    const el = node("div","job");
    const head = node("div","");
    head.append(node("div","job-head",job.name));
    let detail = fmt(job.transferred) + " / " + fmt(job.size);
    const tick = Date.now();
    const previous = state.rates.get(job.id);
    if (job.state === "running" && previous && job.transferred > previous.bytes) {
      const seconds = (tick - previous.time) / 1000;
      if (seconds > 0) {
        const speed = (job.transferred - previous.bytes) / seconds;
        if (speed > 0) {
          const eta = Math.ceil((job.size - job.transferred) / speed);
          detail += " · " + fmt(speed) + "/s";
          if (eta > 0 && Number.isFinite(eta)) {
            detail += " · env. " + (eta < 3600 ? Math.ceil(eta/60) + " min" : (eta/3600).toFixed(1) + " h");
          }
        }
      }
    }
    if (!previous || previous.bytes !== job.transferred) {
      state.rates.set(job.id,{bytes:job.transferred,time:tick});
    }
    head.append(node("div","job-meta",detail));
    el.append(head,node("span","status " + job.state,statusName(job.state)));
    const progress = node("div","progress");
    const bar = node("div");
    bar.style.width = (job.size ? Math.min(100,100*job.transferred/job.size) : job.state === "done" ? 100 : 0) + "%";
    progress.append(bar);el.append(progress);
    if (job.error) el.append(node("div","error-message",job.error));
    if (["error","queued","paused","running"].includes(job.state)) {
      const actions = node("div","job-actions");
      const action = job.state === "error" ? "retry" : job.state === "paused" ? "resume" : "pause";
      const btn = node("button","",({retry:"Réessayer",resume:"Reprendre",pause:"Mettre en pause"})[action]);
      btn.type="button";btn.addEventListener("click",()=>jobAction(job.id,action));
      actions.append(btn);el.append(actions);
    }
    jobs.append(el);
  }
}

async function refresh() {
  try{renderJobs(await api("/api/jobs"));}
  catch(err){notice(err.message);}
}

async function boot() {
  try {
    const data = await api("/api/config");
    state.csrf = data.csrf;
    state.partitions = data.partitions;
    state.destinations = data.destinations;
    $("connection").textContent = "Freebox connectée";
    const select = $("partition");
    for (const p of data.partitions) {
      const option = node("option","",p.name + " · " + fmt(p.used));
      option.value = p.path;select.append(option);
    }
    targetOptions();
    if (state.partitions.length) {
      await openFolder({name:state.partitions[0].name,path:state.partitions[0].path},true);
    }
    select.addEventListener("change",()=>{
      const part = state.partitions.find(p=>p.path === select.value);
      if(part) openFolder({name:part.name,path:part.path},true);
    });
    $("back").addEventListener("click",goBack);
    $("clear").addEventListener("click",()=>{state.selection.clear();selectedUI();renderEntries();});
    $("destination").addEventListener("change",invalidate);
    $("preview").addEventListener("click",preview);
    $("queue").addEventListener("click",queue);
    $("refresh").addEventListener("click",refresh);
    await refresh();
    setInterval(refresh,3000);
  } catch(err) {
    $("connection").textContent = "Connexion indisponible";
    notice(err.message + " — vérifiez la configuration sur le VPS.");
  }
}
boot();
