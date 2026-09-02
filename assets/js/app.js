/* Hypercycle frontend — intake, category groups, and the conversion queue.
   Talks to the Python bridge exposed on window.pywebview.api. */

(function hypercycle() {
  "use strict";

  var ASCII_FRAMES = ["|", "/", "-", "\\"];
  var ASCII_INTERVAL_MS = 110;

  var el = {};
  var state = {
    outputDir: null,
    targets: [],
    accepted: [],
    categories: {},
    order: [],
    defaults: {},
    groupTargets: {},
    jobs: [],
    running: false,
  };

  var asciiTick = 0;
  var asciiTimer = null;

  function byId(id) { return document.getElementById(id); }
  function api() { return (window.pywebview && window.pywebview.api) || null; }

  function toast(message, isError) {
    if (!el.toastHost) return;
    var node = document.createElement("div");
    node.className = "hc-toast" + (isError ? " hc-toast-error" : "");
    node.textContent = message;
    el.toastHost.appendChild(node);
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 5000);
  }

  // --- Boot ----------------------------------------------------------------

  function boot() {
    el.brandMark = byId("hc-brand-mark");
    el.intakeMark = byId("hc-intake-mark");
    el.caps = byId("hc-caps");
    el.dropzone = byId("hc-dropzone");
    el.accepted = byId("hc-accepted");
    el.groups = byId("hc-groups");
    el.empty = byId("hc-empty");
    el.outdir = byId("hc-outdir");
    el.pickOutdir = byId("hc-pick-outdir");
    el.summary = byId("hc-summary");
    el.start = byId("hc-start");
    el.clear = byId("hc-clear");
    el.toastHost = byId("hc-toast-host");

    wireEvents();
    waitForBridge(loadInitialState);
  }

  function waitForBridge(done, attempt) {
    attempt = attempt || 0;
    if (api()) { done(); return; }
    if (attempt > 100) {
      toast("Python bridge unavailable — conversions disabled.", true);
      return;
    }
    setTimeout(function () { waitForBridge(done, attempt + 1); }, 50);
  }

  function loadInitialState() {
    api().get_startup_state().then(function (data) {
      state.targets = data.targets || [];
      state.accepted = data.accepted || [];
      state.categories = data.categories || {};
      state.order = data.category_order || [];
      state.defaults = data.default_targets || {};
      applyTheme(data.theme);
      renderMarks(data.brand_svg);
      renderCaps(data.capabilities || []);
      renderAccepted();
      refreshQueue();
    }).catch(function (err) {
      toast("Startup failed: " + err, true);
    });
  }

  // --- Theme ---------------------------------------------------------------
  // The palette is owned by Hypervisor's preferences; Hyperkit's token file
  // carries fallback values only.

  function applyTheme(theme) {
    if (!theme) return;
    var root = document.documentElement;
    var accent = theme.accent;

    if (accent) {
      root.style.setProperty("--accent", accent);
      // Alpha variants are hex-suffixed to match how the sibling apps derive
      // them: 0x26 is the ~15% border tint, 0x0f the ~6% glow.
      root.style.setProperty("--accent-border", accent + "26");
      root.style.setProperty("--accent-glow", accent + "0f");
    }
    if (theme.warm) root.style.setProperty("--warm", theme.warm);
    if (theme.cool) root.style.setProperty("--cool", theme.cool);
    if (theme.comp) root.style.setProperty("--comp", theme.comp);

    var semantics = theme.semantics || {};
    ["success", "warning", "error", "info"].forEach(function (key) {
      if (semantics[key]) root.style.setProperty("--" + key, semantics[key]);
    });
  }

  // --- Static rendering ----------------------------------------------------

  function renderMarks(svg) {
    if (!svg) return;
    if (el.brandMark) el.brandMark.innerHTML = svg;
    if (el.intakeMark) el.intakeMark.innerHTML = svg;
  }

  function renderCaps(caps) {
    var missing = caps.filter(function (c) { return !c.available; });
    if (!missing.length) {
      el.caps.textContent = caps.length + " ENGINES READY";
      el.caps.classList.remove("hc-caps-degraded");
      el.caps.setAttribute("data-tooltip", caps.map(function (c) {
        return c.name;
      }).join("\n"));
      return;
    }
    el.caps.textContent = missing.length + " ENGINE(S) UNAVAILABLE";
    el.caps.classList.add("hc-caps-degraded");
    el.caps.setAttribute("data-tooltip", missing.map(function (c) {
      return c.name + (c.detail ? " — " + c.detail : "");
    }).join("\n"));
  }

  function renderAccepted() {
    el.accepted.textContent = state.accepted.length
      ? state.accepted.join(" ").toUpperCase()
      : "No input formats available.";
  }

  // --- Queue ---------------------------------------------------------------

  // Status maps onto the Hyperkit chip vocabulary: filled accent for the loud
  // current state, outlined accent for notable, outlined muted for quiet.
  var CHIP_VARIANT = {
    pending: "hv-chip-outlined-muted",
    complete: "hv-chip-outlined-accent",
    failed: "hv-chip-outlined-accent hc-chip-failed",
    cancelled: "hv-chip-outlined-muted",
  };

  function stateCell(job) {
    if (job.status === "converting") {
      // The one motion beat, and only on the row actually doing work.
      var span = document.createElement("span");
      span.className = "hc-cyc";
      span.setAttribute("data-ascii", "1");
      span.textContent = ASCII_FRAMES[asciiTick % ASCII_FRAMES.length];
      return span;
    }
    var chip = document.createElement("span");
    chip.className = "hv-chip " + (CHIP_VARIANT[job.status] || "hv-chip-outlined-muted");
    chip.textContent = job.status;
    return chip;
  }

  function pairCell(job, target) {
    var wrap = document.createElement("span");
    wrap.className = "hc-pair";

    var from = document.createElement("span");
    from.className = "hv-chip hv-chip-outlined-muted";
    from.textContent = job.source_ext;

    var arrow = document.createElement("span");
    arrow.className = "hc-pair-arrow";
    arrow.textContent = "\u2192";

    var to = document.createElement("span");
    to.className = "hv-chip hv-chip-outlined-accent";
    to.textContent = target;

    wrap.appendChild(from);
    wrap.appendChild(arrow);
    wrap.appendChild(to);
    return wrap;
  }

  function targetFor(category) {
    return state.groupTargets[category]
      || state.defaults[category]
      || (state.categories[category] || {}).targets[0];
  }

  function renderQueue() {
    el.groups.innerHTML = "";
    el.empty.classList.toggle("hc-hidden", state.jobs.length > 0);
    el.groups.classList.toggle("hc-hidden", state.jobs.length === 0);

    var present = state.order.filter(function (cat) {
      return state.jobs.some(function (j) { return j.category === cat; });
    });

    present.forEach(function (cat) {
      var meta = state.categories[cat] || { label: cat, targets: [] };
      var rows = state.jobs.filter(function (j) { return j.category === cat; });
      var live = rows.some(function (j) {
        return j.status === "pending" || j.status === "converting";
      });
      var target = targetFor(cat);

      var group = document.createElement("div");
      group.className = "hc-group" + (live ? " hc-group-active" : "");

      // -- header: label, count, and this category's own target format
      var head = document.createElement("div");
      head.className = "hc-group-head";

      var name = document.createElement("span");
      name.className = "hc-group-name";
      name.textContent = meta.label;

      var count = document.createElement("span");
      count.className = "hv-chip hv-chip-outlined-muted";
      count.textContent = String(rows.length);

      var spacer = document.createElement("span");
      spacer.className = "hc-group-spacer";

      var field = document.createElement("span");
      field.className = "hc-group-field";
      var key = document.createElement("span");
      key.className = "hc-key";
      key.textContent = "to";
      var sel = document.createElement("select");
      sel.className = "hc-target";
      sel.setAttribute("aria-label", meta.label + " target format");
      (meta.targets || []).forEach(function (ext) {
        var opt = document.createElement("option");
        opt.value = ext;
        opt.textContent = "." + ext;
        if (ext === target) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function () {
        state.groupTargets[cat] = sel.value;
        api().set_group_target(cat, sel.value).then(refreshQueue);
      });
      field.appendChild(key);
      field.appendChild(sel);

      head.appendChild(name);
      head.appendChild(count);
      head.appendChild(spacer);
      head.appendChild(field);
      group.appendChild(head);

      // -- rows
      rows.forEach(function (job) {
        var row = document.createElement("div");
        row.className = "hc-job";

        row.appendChild(stateCell(job));

        var jobName = document.createElement("span");
        jobName.className = "hc-job-name";
        jobName.textContent = job.name;
        jobName.setAttribute("data-tooltip", job.source);
        row.appendChild(jobName);

        var size = document.createElement("span");
        size.className = "hc-job-size";
        size.textContent = job.size || "";
        row.appendChild(size);

        row.appendChild(pairCell(job, job.target_ext || target));

        if (job.error) {
          var err = document.createElement("span");
          err.className = "hc-job-error";
          err.textContent = job.error;
          row.appendChild(err);
        }

        group.appendChild(row);
      });

      el.groups.appendChild(group);
    });

    renderSummary(present.length);
    updateButtons();
    syncAsciiTimer();
  }

  function renderSummary(groupCount) {
    if (!el.summary) return;
    if (!state.jobs.length) { el.summary.textContent = ""; return; }
    var n = state.jobs.length;
    el.summary.textContent = n + " file" + (n === 1 ? "" : "s")
      + " in " + groupCount + " group" + (groupCount === 1 ? "" : "s");
  }

  function updateButtons() {
    var pending = state.jobs.some(function (j) { return j.status === "pending"; });
    var finished = state.jobs.some(function (j) {
      return j.status === "complete" || j.status === "failed" || j.status === "cancelled";
    });
    el.start.disabled = !pending || !state.outputDir || state.running;
    el.clear.disabled = !finished;
  }

  function refreshQueue() {
    api().list_jobs().then(function (jobs) {
      state.jobs = jobs || [];
      renderQueue();
    });
  }

  // --- ASCII frame ticker --------------------------------------------------
  // Runs only while something is converting, so an idle window costs nothing.

  function syncAsciiTimer() {
    var active = state.jobs.some(function (j) { return j.status === "converting"; });
    if (active && !asciiTimer) {
      asciiTimer = setInterval(function () {
        asciiTick++;
        var frame = ASCII_FRAMES[asciiTick % ASCII_FRAMES.length];
        var cells = document.querySelectorAll("[data-ascii]");
        for (var i = 0; i < cells.length; i++) cells[i].textContent = frame;
      }, ASCII_INTERVAL_MS);
    } else if (!active && asciiTimer) {
      clearInterval(asciiTimer);
      asciiTimer = null;
    }
  }

  // --- Events --------------------------------------------------------------

  function wireEvents() {
    el.dropzone.addEventListener("click", browseFiles);
    el.dropzone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        browseFiles();
      }
    });

    ["dragenter", "dragover"].forEach(function (evt) {
      el.dropzone.addEventListener(evt, function (e) {
        e.preventDefault();
        el.dropzone.classList.add("hc-dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      el.dropzone.addEventListener(evt, function (e) {
        e.preventDefault();
        el.dropzone.classList.remove("hc-dragover");
      });
    });
    el.dropzone.addEventListener("drop", handleDrop);

    el.pickOutdir.addEventListener("click", pickOutputDir);
    el.start.addEventListener("click", startQueue);
    el.clear.addEventListener("click", function () {
      api().clear_finished().then(refreshQueue);
    });

    // Palette changes are made in Hypervisor, so re-read on focus.
    window.addEventListener("focus", function () {
      if (!api()) return;
      api().get_theme().then(applyTheme).catch(function () {});
    });
  }

  function handleDrop(e) {
    var files = (e.dataTransfer && e.dataTransfer.files) || [];
    var paths = [];
    for (var i = 0; i < files.length; i++) {
      // pywebview exposes the OS path here; browsers without it yield "".
      var p = files[i].path || files[i].name;
      if (p) paths.push(p);
    }
    if (!paths.length) {
      toast("Could not read dropped paths — use browse instead.", true);
      return;
    }
    enqueue(paths);
  }

  function browseFiles() {
    api().pick_files().then(function (paths) {
      if (paths && paths.length) enqueue(paths);
    });
  }

  function enqueue(paths) {
    api().add_jobs(paths).then(function (res) {
      if (res.rejected && res.rejected.length) {
        toast(res.rejected.length + " file(s) skipped: unsupported input.", true);
      }
      refreshQueue();
    });
  }

  function pickOutputDir() {
    api().pick_output_dir().then(function (path) {
      if (!path) return;
      state.outputDir = path;
      el.outdir.textContent = path;
      updateButtons();
    });
  }

  function startQueue() {
    state.running = true;
    updateButtons();
    api().start_queue().then(function (res) {
      if (!res.ok) {
        state.running = false;
        toast(res.error || "Could not start.", true);
        updateButtons();
      }
    });
  }

  // --- Bridge push ---------------------------------------------------------

  window.hcJobUpdate = function (job) {
    var found = false;
    for (var i = 0; i < state.jobs.length; i++) {
      if (state.jobs[i].job_id === job.job_id) {
        state.jobs[i] = job;
        found = true;
        break;
      }
    }
    if (!found) state.jobs.push(job);

    var active = state.jobs.some(function (j) {
      return j.status === "pending" || j.status === "converting";
    });
    if (!active && state.running) {
      state.running = false;
      var done = state.jobs.filter(function (j) { return j.status === "complete"; }).length;
      var bad = state.jobs.filter(function (j) { return j.status === "failed"; }).length;
      toast(done + " converted" + (bad ? ", " + bad + " failed" : "") + ".", bad > 0);
    }
    renderQueue();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
