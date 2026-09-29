"use strict";

let TEMPLATES = [];
let ADDONS = [];
let CAT_LABELS = {};
let TEMPLATE_FILES = {};
let DATA_VERSION = "";

let selected = new Set();
let autoDeps = new Set();
let currentTpl = "blank";
let currentFilter = "all";
let projectName = "my-project";
let pkgName = "my_project";

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function normalisePkg(name) {
  const clean = name.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^_+|_+$/g, "");
  return clean || "my_project";
}

function pkg(path) {
  return path.replace(/\(\(pkg_name\)\)/g, pkgName);
}

function findAddon(id) {
  return ADDONS.find((a) => a.id === id);
}

function allAddons() {
  return [...selected, ...autoDeps].map(findAddon).filter(Boolean);
}

function hasDocker() {
  return allAddons().some((a) => a.id === "docker");
}

/* state derivation */

function computeAuto() {
  autoDeps.clear();
  const resolve = (id, depth) => {
    if (depth > 10) return;
    const a = findAddon(id);
    if (!a) return;
    (a.requires || []).forEach((r) => {
      if (!selected.has(r)) {
        autoDeps.add(r);
        resolve(r, depth + 1);
      }
    });
  };
  selected.forEach((id) => resolve(id, 0));
}

function addonState(a) {
  if (!a.templates.includes(currentTpl)) return { cls: "off", reason: "fastapi only" };
  if (selected.has(a.id)) return { cls: "sel", reason: "" };
  if (autoDeps.has(a.id)) return { cls: "auto", reason: "auto" };
  const clash = (a.conflicts || []).filter((c) => selected.has(c) || autoDeps.has(c));
  if (clash.length) return { cls: "off", reason: "conflicts with " + clash.join(", ") };
  return { cls: "", reason: "" };
}

function toggle(id) {
  const a = findAddon(id);
  if (!a) return;
  if (autoDeps.has(id)) return;
  if (!a.templates.includes(currentTpl)) return;
  const clash = (a.conflicts || []).filter((c) => selected.has(c) || autoDeps.has(c));
  if (clash.length) return;
  if (selected.has(id)) selected.delete(id);
  else selected.add(id);
  computeAuto();
  renderAll();
}

function setTemplate(id) {
  currentTpl = id;
  selected.clear();
  autoDeps.clear();
  renderAll();
}

function clearAll() {
  selected.clear();
  autoDeps.clear();
  renderAll();
}

/* url */

function parseURL() {
  const p = new URLSearchParams(window.location.search);
  if (p.has("template")) {
    const t = TEMPLATES.find((x) => x.id === p.get("template"));
    if (t) currentTpl = t.id;
  }
  if (p.has("addons")) {
    p.get("addons").split(",").forEach((id) => {
      if (findAddon(id)) selected.add(id);
    });
    computeAuto();
  }
  if (p.has("p") && p.get("p").trim()) {
    projectName = p.get("p").trim().slice(0, 60);
    pkgName = normalisePkg(projectName);
    $("projInput").value = projectName;
  }
}

function shareState() {
  const addons = [...selected].join(",");
  let url = location.pathname + "?template=" + currentTpl;
  if (addons) url += "&addons=" + addons;
  if (projectName !== "my-project") url += "&p=" + encodeURIComponent(projectName);
  return url;
}

function flashBtn(btn, text) {
  const orig = btn.textContent;
  btn.textContent = text;
  btn.classList.add("done");
  setTimeout(() => {
    btn.textContent = orig;
    btn.classList.remove("done");
  }, 1600);
}

/* command */

function buildCommand() {
  const all = [...selected, ...autoDeps];
  let cmd = "zenit create " + projectName + " --template " + currentTpl;
  if (all.length) cmd += " \\\n  --addons " + all.join(",");
  return cmd;
}

function renderCommand() {
  const cmd = buildCommand();
  const firstNL = cmd.indexOf("\n");
  let html;
  if (firstNL === -1) {
    html = '<span class="p">$ </span>' + esc(cmd);
  } else {
    html = '<span class="p">$ </span>' + esc(cmd.slice(0, firstNL)) + "\n" + esc(cmd.slice(firstNL + 1));
  }
  $("cmdText").innerHTML = html;
  $("clearBtn").style.display = selected.size ? "" : "none";
  $("miniText").textContent = "$ " + cmd.replace(/\s+/g, " ");

  if (!autoDeps.size) {
    $("autoNote").innerHTML = "";
    return;
  }
  const parts = [...autoDeps].map((id) => {
    const by = [...selected].map(findAddon).filter((a) => a && (a.requires || []).includes(id)).map((a) => a.id);
    return "<code>" + esc(id) + "</code> (required by " + esc(by.join(", ") || "dependency") + ")";
  });
  $("autoNote").innerHTML = "Auto-included: " + parts.join(", ");
}

/* sidebar */

function renderTemplates() {
  $("tplList").innerHTML = TEMPLATES.map((t) => {
    const on = t.id === currentTpl ? " on" : "";
    return '<button class="tpl' + on + '" data-tpl="' + esc(t.id) + '">' +
      esc(t.id) + "<small>" + esc(t.desc) + "</small></button>";
  }).join("");
  document.querySelectorAll("#tplList .tpl").forEach((el) => {
    el.addEventListener("click", () => setTemplate(el.dataset.tpl));
  });
}

function renderFilters() {
  const cats = ["all", ...Object.keys(CAT_LABELS)];
  $("filterChips").innerHTML = cats.map((c) => {
    const count = c === "all"
      ? ADDONS.filter((a) => a.templates.includes(currentTpl)).length
      : ADDONS.filter((a) => a.tags.includes(c) && a.templates.includes(currentTpl)).length;
    const label = c === "all" ? "all" : CAT_LABELS[c];
    const on = currentFilter === c ? " on" : "";
    return '<button class="chip' + on + '" data-f="' + esc(c) + '">' + esc(label) + " (" + count + ")</button>";
  }).join("");
  document.querySelectorAll("#filterChips .chip").forEach((el) => {
    el.addEventListener("click", () => {
      currentFilter = el.dataset.f;
      renderFilters();
      renderAddons();
    });
  });
}

function detailBlock(a) {
  const rows = [];
  if (a.files && a.files.length) {
    rows.push("<dt>files</dt><dd>" + a.files.map((f) => esc(pkg(f))).join("<br>") + "</dd>");
  }
  if (a.injections && a.injections.length) {
    rows.push("<dt>injections</dt><dd>" + a.injections.map(esc).join("<br>") + "</dd>");
  }
  const deps = [...(a.deps || []), ...(a.devDeps || []).map((d) => d + " (dev)")];
  if (deps.length) {
    rows.push("<dt>pip deps</dt><dd>" + deps.map(esc).join("<br>") + "</dd>");
  }
  if (a.envVars && a.envVars.length) {
    rows.push("<dt>env vars</dt><dd>" + a.envVars.map(esc).join("<br>") + "</dd>");
  }
  if (a.recipes && a.recipes.length) {
    rows.push("<dt>just recipes</dt><dd>" + a.recipes.map((r) => "just " + esc(r)).join("<br>") + "</dd>");
  }
  if (a.composeServices && a.composeServices.length) {
    rows.push("<dt>compose services</dt><dd>" + a.composeServices.map(esc).join("<br>") + "</dd>");
  }
  if (!rows.length) return "";
  return '<details class="addon-detail"><summary>contents</summary><dl class="dl">' +
    rows.join("") + "</dl></details>";
}

function renderAddons() {
  const visible = ADDONS.filter((a) => currentFilter === "all" || a.tags.includes(currentFilter));

  const n = selected.size + autoDeps.size;
  $("selCount").textContent = n ? "(" + n + ")" : "";

  if (!visible.length) {
    $("addonList").innerHTML = '<p class="empty">No addons match.</p>';
    return;
  }

  const groups = {};
  visible.forEach((a) => {
    const tag = a.tags[0];
    (groups[tag] = groups[tag] || []).push(a);
  });

  $("addonList").innerHTML = Object.entries(groups).map(([tag, list]) => {
    const rows = list.map((a) => {
      const st = addonState(a);
      const checked = st.cls === "sel" || st.cls === "auto" ? " checked" : "";
      const dis = st.cls === "off" ? " disabled" : "";
      let meta = "";
      if (st.cls === "auto") meta += '<span class="meta auto">auto</span>';
      (a.requires || []).forEach((r) => {
        if (st.cls !== "auto") meta += '<span class="meta">needs ' + esc(r) + "</span>";
      });
      if (st.reason && st.cls === "off") meta += '<span class="meta warn">' + esc(st.reason) + "</span>";
      return '<div class="addon ' + st.cls + '" tabindex="0" role="checkbox" aria-checked="' +
        (checked ? "true" : "false") + '" data-id="' + esc(a.id) + '">' +
        '<input type="checkbox"' + checked + dis + ' tabindex="-1" aria-hidden="true">' +
        '<div class="addon-body"><div class="addon-name">' + esc(a.name) + "</div>" +
        '<div class="addon-desc">' + esc(a.desc) + "</div>" +
        (meta ? '<div class="addon-meta">' + meta + "</div>" : "") +
        detailBlock(a) + "</div></div>";
    }).join("");
    return '<div class="cat"><div class="cat-name">' + esc(CAT_LABELS[tag] || tag) +
      "</div>" + rows + "</div>";
  }).join("");

  document.querySelectorAll("#addonList .addon").forEach((el) => {
    const id = el.dataset.id;
    el.addEventListener("click", (e) => {
      if (e.target.closest("details")) return;
      toggle(id);
    });
    el.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target === el) {
        e.preventDefault();
        toggle(id);
      }
    });
  });
}

/* detail: what gets generated */

function section(num, name, count, body) {
  return '<div class="dsec"><div class="dsec-head"><span class="dsec-num">' + num +
    '</span><span class="dsec-name">' + name + '</span><span class="dsec-count">' + count +
    "</span></div><div>" + body + "</div></div>";
}

function renderDetail() {
  const addons = allAddons();
  const tplFiles = TEMPLATE_FILES[currentTpl] || [];
  const addonFiles = [];
  addons.forEach((a) => (a.files || []).forEach((f) => addonFiles.push({ f, addon: a.id })));
  const injections = [];
  addons.forEach((a) => (a.injections || []).forEach((inj) => injections.push({ inj, addon: a.id })));
  const deps = [];
  addons.forEach((a) => {
    (a.deps || []).forEach((d) => deps.push(d));
    (a.devDeps || []).forEach((d) => deps.push(d + " (dev)"));
  });
  const env = [];
  addons.forEach((a) => (a.envVars || []).forEach((v) => env.push(v)));
  const recipes = [];
  addons.forEach((a) => (a.recipes || []).forEach((r) => recipes.push(r)));
  const services = [];
  addons.forEach((a) => {
    if (a.id === "docker" || !hasDocker()) return;
    (a.composeServices || []).forEach((s) => services.push({ s, addon: a.id }));
  });
  const waitingForDocker = !hasDocker() && addons.some((a) => (a.composeServices || []).length);

  const names = addons.length
    ? currentTpl + " + " + addons.map((a) => a.id).join(", ")
    : currentTpl + " (no addons)";
  $("detailSub").textContent = names + ": " +
    (tplFiles.length + addonFiles.length) + " files, " +
    injections.length + " injections, " + deps.length + " deps.";

  const out = [];

  out.push(section("01", "Template files", tplFiles.length,
    '<div class="file-list">' + tplFiles.map((f) => '<span class="file">' + esc(pkg(f)) + "</span>").join("") + "</div>"));

  if (addonFiles.length) {
    const byAddon = {};
    addonFiles.forEach((x) => ((byAddon[x.addon] = byAddon[x.addon] || []).push(x.f)));
    out.push(section("02", "Addon files", addonFiles.length,
      Object.entries(byAddon).map(([id, files]) =>
        '<div class="file-group"><div class="file-group-name">' + esc(id) + '</div><div class="file-list">' +
        files.map((f) => '<span class="file">' + esc(pkg(f)) + "</span>").join("") + "</div></div>"
      ).join("")));
  } else {
    out.push(section("02", "Addon files", 0, '<p class="muted">Select addons to see their files here.</p>'));
  }

  out.push(section("03", "Code injections", injections.length,
    injections.length
      ? injections.map((x) => '<div class="inj"><span class="arr">-&gt;</span>' + esc(x.inj) +
        '<span class="who">' + esc(x.addon) + "</span></div>").join("")
      : '<p class="muted">No structural edits for this selection.</p>'));

  out.push(section("04", "Dependencies", deps.length,
    deps.length
      ? '<div class="file-list">' + deps.map((d) => '<span class="file">' + esc(d) + "</span>").join("") + "</div>"
      : '<p class="muted">No new pip packages.</p>'));

  out.push(section("05", "Env vars", env.length,
    env.length
      ? '<div class="file-list">' + env.map((v) => '<span class="file">' + esc(v) + "</span>").join("") + "</div>"
      : '<p class="muted">No new environment variables.</p>'));

  out.push(section("06", "Just recipes", recipes.length,
    recipes.length
      ? '<div class="file-list">' + recipes.map((r) => '<span class="file">just ' + esc(r) + "</span>").join("") + "</div>"
      : '<p class="muted">No new just recipes.</p>'));

  let composeBody;
  if (services.length) {
    composeBody = '<div class="file-list">' + services.map((x) => '<span class="file">' + esc(x.s) +
      "</span>").join("") + "</div>";
  } else if (waitingForDocker) {
    composeBody = '<p class="muted">Selected addons define compose services, add docker to merge them into compose.yml.</p>';
  } else {
    composeBody = '<p class="muted">No compose services for this selection.</p>';
  }
  out.push(section("07", "Compose merge", services.length, composeBody));

  const totalFiles = tplFiles.length + addonFiles.length;
  out.push(section("08", "Manifest and git", "auto",
    '<div class="static-line"><span class="a">.zenit.toml</span> tracks ' + totalFiles +
    " files and " + addons.length + " addons, fingerprints written</div>" +
    '<div class="static-line"><span class="g">git init, git add ., git commit</span> on create</div>'));

  $("detailBody").innerHTML = out.join("");
}

function renderAll() {
  renderTemplates();
  renderFilters();
  renderAddons();
  renderCommand();
  renderDetail();
}

/* events */

function initEvents() {
  $("projInput").addEventListener("input", (e) => {
    projectName = e.target.value.trim() || "my-project";
    pkgName = normalisePkg(projectName);
    renderCommand();
    renderDetail();
  });

  $("copyBtn").addEventListener("click", () => {
    navigator.clipboard.writeText(buildCommand()).catch(() => {});
    flashBtn($("copyBtn"), "copied");
  });

  $("miniCopy").addEventListener("click", () => {
    navigator.clipboard.writeText(buildCommand()).catch(() => {});
    flashBtn($("miniCopy"), "copied");
  });

  $("clearBtn").addEventListener("click", clearAll);

  const panel = $("cmdPanel");
  const mini = $("miniCmd");
  if ("IntersectionObserver" in window && panel && mini) {
    new IntersectionObserver(
      (entries) => {
        const out = !entries[0].isIntersecting && entries[0].boundingClientRect.top < 80;
        mini.classList.toggle("show", out);
        mini.setAttribute("aria-hidden", out ? "false" : "true");
      },
      { rootMargin: "-80px 0px 0px 0px" }
    ).observe(panel);
  }

  $("shareBtn").addEventListener("click", () => {
    navigator.clipboard.writeText(location.origin + shareState()).catch(() => {});
    flashBtn($("shareBtn"), "link copied");
  });
}

/* boot */

async function init() {
  let data;
  try {
    const resp = await fetch("studio-data.json");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    data = await resp.json();
  } catch (e) {
    $("detailBody").innerHTML = '<p class="muted">Could not load studio-data.json.</p>';
    return;
  }
  TEMPLATES = data.templates;
  ADDONS = data.addons;
  CAT_LABELS = data.catLabels;
  TEMPLATE_FILES = data.templateFiles;
  DATA_VERSION = data.version;
  $("footMeta").textContent = "zenit studio v" + DATA_VERSION;
  parseURL();
  initEvents();
  renderAll();
}

init();
