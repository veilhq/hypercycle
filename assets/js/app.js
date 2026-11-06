/* Hypercycle frontend - routing screen, full-surface drop target, queue drawer.
   Talks to the Python bridge exposed on window.pywebview.api. */

(function hypercycle() {
  "use strict";

  var ASCII_FRAMES = ["|", "/", "-", "\\"];
  var ASCII_INTERVAL_MS = 110;

  var el = {};
  var state = {
    outputDir: null,
    accepted: [],
    modes: [],
    categories: {},
    order: [],
    defaults: {},
    groupTargets: {},
    jobs: [],
    running: false,
    drawerOpen: false,
    brandSvg: "",
    formatIndex: [],
    finderMatches: [],
    finderActive: -1,
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
    el.app = byId("hc-app");
    el.brand = byId("hc-brand");
    el.optbarActions = byId("hc-optbar-actions");
    el.finder = byId("hc-finder");
    el.finderInput = byId("hc-finder-input");
    el.finderList = byId("hc-finder-list");
    el.caps = byId("hc-caps");
    el.drawerToggle = byId("hc-drawer-toggle");
    el.drawerClose = byId("hc-drawer-close");
    el.drawer = byId("hc-drawer");
    el.queueCount = byId("hc-queue-count");
    el.dropzone = byId("hc-dropzone");
    el.dropMark = byId("hc-drop-mark");
    el.groups = byId("hc-groups");
    el.empty = byId("hc-empty");
    el.summary = byId("hc-summary");
    el.outdir = byId("hc-outdir");
    el.pickOutdir = byId("hc-pick-outdir");
    el.start = byId("hc-start");
    el.clear = byId("hc-clear");
    el.toastHost = byId("hc-toast-host");
    el.depOverlay = byId("hc-dep-overlay");
    el.depList = byId("hc-dep-list");
    el.depCmdText = byId("hc-dep-cmd-text");
    el.depCopy = byId("hc-dep-copy");
    el.depDismiss = byId("hc-dep-dismiss");

    wireEvents();
    waitForBridge(loadInitialState);
  }

  function waitForBridge(done, attempt) {
    attempt = attempt || 0;
    if (api()) { done(); return; }
    if (attempt > 100) {
      toast("Python bridge unavailable - conversions disabled.", true);
      return;
    }
    setTimeout(function () { waitForBridge(done, attempt + 1); }, 50);
  }

  function loadInitialState() {
    api().get_startup_state().then(function (data) {
      state.accepted = data.accepted || [];
      state.modes = data.modes || [];
      state.categories = data.categories || {};
      state.order = data.category_order || [];
      state.defaults = data.default_targets || {};
      state.brandSvg = data.brand_svg || "";
      applyTheme(data.theme);
      renderBrand();
      renderCaps(data.capabilities || []);
      renderOptbarActions();
      buildFormatIndex();
      refreshQueue();
    }).catch(function (err) {
      toast("Startup failed: " + err, true);
    });
  }

  // --- Theme ---------------------------------------------------------------
  // The palette and light-mode flag are owned by Hypervisor's preferences;
  // Hyperkit's token file carries fallback values only. Python re-pushes the
  // theme on a poll (see push_theme), so this repaints on a change or a forced push.

  var lastThemeKey = null;

  function applyTheme(theme, force) {
    if (!theme) return;

    // Skip redundant repaints unless forced, since the poll fires every few seconds.
    var key = JSON.stringify(theme);
    if (!force && key === lastThemeKey) return;
    lastThemeKey = key;

    var root = document.documentElement;

    // Light mode is the ecosystem accessibility toggle. It repaints the neutrals
    // and the accent through the a11y-bw-theme class. Inline styles would beat
    // that class, so while light mode is on we must NOT set the accent variables
    // inline - the class owns them. Clear any we set on a previous pass.
    var light = !!theme.light;
    root.classList.toggle("a11y-bw-theme", light);

    var accentProps = ["--accent", "--accent-border", "--accent-glow",
                       "--warm", "--cool", "--comp"];
    if (light) {
      accentProps.forEach(function (p) { root.style.removeProperty(p); });
      return;
    }

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

  // Python drives theme refreshes directly through this, since pywebview has no
  // window-focus event to hook.
  window.hcApplyTheme = applyTheme;

  // --- Route screen --------------------------------------------------------

  function renderBrand() {
    if (!state.brandSvg) return;
    if (el.brand) el.brand.innerHTML = state.brandSvg;
    if (el.dropMark) el.dropMark.innerHTML = state.brandSvg;
  }

  function renderCaps(caps) {
    var missing = caps.filter(function (c) { return !c.available; });
    if (!missing.length) {
      el.caps.textContent = caps.length + " engines ready";
      el.caps.classList.remove("hc-caps-degraded");
      el.caps.setAttribute("data-tooltip", caps.map(function (c) {
        return c.name;
      }).join("\n"));
      return;
    }
    el.caps.textContent = missing.length + " engine(s) unavailable";
    el.caps.classList.add("hc-caps-degraded");
    el.caps.setAttribute("data-tooltip", missing.map(function (c) {
      return c.name + (c.detail ? " - " + c.detail : "");
    }).join("\n"));

    // A missing OPTIONAL engine (the PDF engine) is expected and never warns -
    // it's surfaced in the caps line above and the disabled nav. Only a missing
    // CORE engine means the install is broken, and that gets the launch modal.
    var missingCore = missing.filter(function (c) { return !c.optional; });
    if (missingCore.length) showDepModal(missingCore);
  }

  // --- Dependency modal ----------------------------------------------------
  // Launch-only, dismissible. Appears when a bundled engine failed to load -
  // an incomplete install - and points the operator at the reinstall command.

  function isDepModalOpen() {
    return el.depOverlay && el.depOverlay.classList.contains("visible");
  }

  function showDepModal(missingCore) {
    if (!el.depOverlay) return;

    el.depList.innerHTML = "";
    missingCore.forEach(function (cap) {
      var li = document.createElement("li");
      li.className = "hc-dep-item";

      var name = document.createElement("span");
      name.className = "hc-dep-name";
      name.textContent = cap.name;
      li.appendChild(name);

      // Name the conversion area this engine unlocks, so the impact is concrete.
      var impact = document.createElement("span");
      impact.className = "hc-dep-impact";
      impact.textContent = cap.category
        ? cap.category + " conversion disabled"
        : "some conversions disabled";
      li.appendChild(impact);

      if (cap.detail) {
        var detail = document.createElement("span");
        detail.className = "hc-dep-detail";
        detail.textContent = cap.detail;
        li.appendChild(detail);
      }

      el.depList.appendChild(li);
    });

    el.depOverlay.hidden = false;
    // Next frame so the opacity transition runs rather than snapping.
    requestAnimationFrame(function () {
      el.depOverlay.classList.add("visible");
    });
    // Move focus to the dismiss control for keyboard and screen-reader users.
    if (el.depDismiss) el.depDismiss.focus();
  }

  function closeDepModal() {
    if (!el.depOverlay) return;
    el.depOverlay.classList.remove("visible");
    // Hide after the fade so it leaves the tab order.
    setTimeout(function () { el.depOverlay.hidden = true; }, 200);
  }

  function copyDepCommand() {
    var text = (el.depCmdText && el.depCmdText.textContent) || "";
    var done = function () { toast("Command copied."); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallbackCopy);
    } else {
      fallbackCopy();
    }
    function fallbackCopy() {
      // WebView2 can withhold the async clipboard API without a secure origin;
      // the legacy selection path still works inside the host.
      try {
        var r = document.createRange();
        r.selectNodeContents(el.depCmdText);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
        document.execCommand("copy");
        sel.removeAllRanges();
        done();
      } catch (err) {
        toast("Copy failed - select the command manually.", true);
      }
    }
  }

  // Bottom options bar - non-drop actions only. File conversion needs no button
  // here: you just drop or browse, and files are auto-categorised. This renders
  // the "non-drop" modes (currently only "From URL"), disabled until their
  // engine ships. New non-drop actions appear by adding a mode with no file
  // extensions on the backend.
  function renderOptbarActions() {
    el.optbarActions.innerHTML = "";
    // A non-drop action is a mode that accepts no file extensions - it is not
    // something you drop, so it belongs in the bar rather than the drop flow.
    var actions = state.modes.filter(function (m) {
      return (m.extensions || []).length === 0;
    });
    actions.forEach(function (mode) {
      var btn = document.createElement("button");
      btn.className = "action-button hc-btn-sm hc-optbar-btn";
      btn.setAttribute("data-mode", mode.id);
      btn.disabled = !mode.available;
      btn.textContent = mode.label;
      if (mode.available) {
        btn.addEventListener("click", function () { runAction(mode); });
      } else {
        btn.setAttribute("data-tooltip", mode.blurb);
      }
      el.optbarActions.appendChild(btn);
    });
  }

  // Placeholder for non-drop actions. "From URL" lands here once its engine
  // ships; until then every such mode is disabled and this is unreachable.
  function runAction(mode) {
    toast(mode.label + " isn't available yet.");
  }

  // --- Format finder -------------------------------------------------------
  // A flat, searchable index of every supported format, tagged with its
  // category - a quick reference for "is X supported and what is it". In the
  // drop-first model there is no mode to route into, so picking a result just
  // fills the field; the answer is the list itself.

  function buildFormatIndex() {
    var seen = {};
    state.formatIndex = [];
    state.modes.forEach(function (mode) {
      if (!mode.available) return;
      (mode.extensions || []).forEach(function (ext) {
        // A format can appear under one mode only; first wins, deterministic
        // because modes come in a fixed order from the backend.
        if (seen[ext]) return;
        seen[ext] = true;
        state.formatIndex.push({ ext: ext, modeId: mode.id, modeLabel: mode.label });
      });
    });
    state.formatIndex.sort(function (a, b) { return a.ext < b.ext ? -1 : 1; });
    state.finderActive = -1;
  }

  function openFinder() {
    renderFinderList(el.finderInput.value.trim().toLowerCase());
  }

  function renderFinderList(query) {
    var matches = state.formatIndex.filter(function (f) {
      return !query || f.ext.indexOf(query) !== -1;
    });

    el.finderList.innerHTML = "";
    if (!matches.length) {
      el.finderList.hidden = true;
      el.finderInput.setAttribute("aria-expanded", "false");
      return;
    }

    matches.forEach(function (f, i) {
      var li = document.createElement("li");
      li.className = "hc-finder-opt";
      li.setAttribute("role", "option");
      li.setAttribute("data-ext", f.ext);
      li.setAttribute("aria-selected", i === state.finderActive ? "true" : "false");
      if (i === state.finderActive) li.classList.add("hc-finder-opt-active");

      var ext = document.createElement("span");
      ext.className = "hc-finder-ext";
      ext.textContent = "." + f.ext;
      li.appendChild(ext);

      var cat = document.createElement("span");
      cat.className = "hc-finder-cat";
      cat.textContent = f.modeLabel;
      li.appendChild(cat);

      // mousedown, not click - click fires after the input blur that would
      // otherwise close the list first.
      li.addEventListener("mousedown", function (e) {
        e.preventDefault();
        chooseFormat(f);
      });
      el.finderList.appendChild(li);
    });

    state.finderMatches = matches;
    el.finderList.hidden = false;
    el.finderInput.setAttribute("aria-expanded", "true");
  }

  function moveFinderActive(delta) {
    var n = (state.finderMatches || []).length;
    if (!n) return;
    state.finderActive = (state.finderActive + delta + n) % n;
    renderFinderList(el.finderInput.value.trim().toLowerCase());
  }

  function chooseFormat(f) {
    // Reference-only in the drop-first model: there's no mode to enter, so a
    // pick just reflects the choice in the field and closes the list. The
    // useful answer was the filtered list itself.
    el.finderInput.value = "." + f.ext;
    closeFinder();
    el.finderInput.blur();
  }

  function closeFinder() {
    el.finderList.hidden = true;
    el.finderInput.setAttribute("aria-expanded", "false");
    state.finderActive = -1;
  }

  // A self-contained searchable target-format picker for a queue group. Options
  // are already clamped by the backend to formats compatible with the group's
  // source type, so this only adds search + keyboard selection over that set.
  // Type to filter, arrows to move, Enter/click to choose; the choice calls
  // onChange(value).
  function makeTargetPicker(options, current, onChange, ariaLabel) {
    var wrap = document.createElement("span");
    wrap.className = "hc-target-picker";

    var input = document.createElement("input");
    input.type = "text";
    input.className = "hc-target-input";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("aria-label", ariaLabel || "target format");
    input.value = current ? "." + current : "";

    var list = document.createElement("ul");
    list.className = "hc-target-list hc-finder-list-up";
    list.setAttribute("role", "listbox");
    list.hidden = true;

    var active = -1;
    var matches = options.slice();

    function render(query) {
      matches = options.filter(function (o) {
        return !query || o.indexOf(query) !== -1;
      });
      list.innerHTML = "";
      if (!matches.length) { list.hidden = true; input.setAttribute("aria-expanded", "false"); return; }
      matches.forEach(function (ext, i) {
        var li = document.createElement("li");
        li.className = "hc-target-opt" + (i === active ? " hc-finder-opt-active" : "");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", i === active ? "true" : "false");
        li.textContent = "." + ext;
        // mousedown, not click - fires before the input blur that closes the list.
        li.addEventListener("mousedown", function (e) { e.preventDefault(); choose(ext); });
        list.appendChild(li);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function choose(ext) {
      input.value = "." + ext;
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.blur();
      if (ext !== current) onChange(ext);
    }

    function move(delta) {
      if (!matches.length) return;
      active = (active + delta + matches.length) % matches.length;
      render(input.value.trim().toLowerCase().replace(/^\./, ""));
    }

    input.addEventListener("focus", function () {
      // Show the whole compatible set on focus; select the text so a keystroke
      // starts a fresh filter rather than appending to ".pdf".
      active = -1;
      input.select();
      render("");
    });
    input.addEventListener("input", function () {
      active = -1;
      render(input.value.trim().toLowerCase().replace(/^\./, ""));
    });
    input.addEventListener("keydown", function (e) {
      if (list.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        var pick = active >= 0 ? matches[active] : matches[0];
        if (pick) choose(pick);
      } else if (e.key === "Escape") {
        list.hidden = true;
        input.setAttribute("aria-expanded", "false");
      }
    });
    input.addEventListener("blur", function () {
      // Restore the current value if the field was left mid-edit, then close.
      setTimeout(function () {
        input.value = current ? "." + current : "";
        list.hidden = true;
        input.setAttribute("aria-expanded", "false");
      }, 120);
    });

    wrap.appendChild(input);
    wrap.appendChild(list);
    return wrap;
  }

  function setDrawer(open) {
    state.drawerOpen = !!open;
    el.drawer.classList.toggle("hc-drawer-open", state.drawerOpen);
    el.drawerToggle.setAttribute("aria-expanded", state.drawerOpen ? "true" : "false");
  }

  // --- Queue ---------------------------------------------------------------

  // Status maps onto the Hyperkit chip vocabulary: outlined accent for notable,
  // outlined muted for quiet. Converting gets the motion beat instead of a chip.
  var CHIP_VARIANT = {
    pending: "status-chip-outlined-muted",
    complete: "status-chip-outlined-accent",
    failed: "status-chip-outlined-accent hc-chip-failed",
    cancelled: "status-chip-outlined-muted",
  };

  function stateCell(job) {
    if (job.status === "converting") {
      var span = document.createElement("span");
      span.className = "hc-cyc";
      span.setAttribute("data-ascii", "1");
      span.textContent = ASCII_FRAMES[asciiTick % ASCII_FRAMES.length];
      return span;
    }
    var chip = document.createElement("span");
    chip.className = "status-chip " + (CHIP_VARIANT[job.status] || "status-chip-outlined-muted");
    chip.textContent = job.status;
    return chip;
  }

  function pairCell(job, target) {
    var wrap = document.createElement("span");
    wrap.className = "hc-job-pair";

    var from = document.createElement("span");
    from.className = "status-chip status-chip-outlined-muted";
    from.textContent = job.source_ext;

    var arrow = document.createElement("span");
    arrow.className = "hc-pair-arrow";
    arrow.textContent = "\u2192";

    var to = document.createElement("span");
    to.className = "status-chip status-chip-outlined-accent";
    to.textContent = target;

    wrap.appendChild(from);
    wrap.appendChild(arrow);
    wrap.appendChild(to);
    return wrap;
  }

  function targetFor(category) {
    var meta = state.categories[category] || {};
    return state.groupTargets[category]
      || state.defaults[category]
      || (meta.targets || [])[0];
  }

  function renderQueue() {
    el.groups.innerHTML = "";
    el.queueCount.textContent = String(state.jobs.length);
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

      var head = document.createElement("div");
      head.className = "hc-group-head";

      var name = document.createElement("span");
      name.className = "hc-group-name";
      name.textContent = meta.label;

      var count = document.createElement("span");
      count.className = "status-chip status-chip-outlined-muted";
      count.textContent = String(rows.length);

      var spacer = document.createElement("span");
      spacer.className = "hc-group-spacer";

      var field = document.createElement("span");
      field.className = "hc-group-field";
      var key = document.createElement("span");
      key.className = "hc-key";
      key.textContent = "to";
      // Searchable target picker, clamped to formats compatible with this
      // category (meta.targets is already the compatible set from the backend).
      var picker = makeTargetPicker(meta.targets || [], target, function (val) {
        state.groupTargets[cat] = val;
        api().set_group_target(cat, val).then(refreshQueue);
      }, meta.label + " target format");
      field.appendChild(key);
      field.appendChild(picker);

      head.appendChild(name);
      head.appendChild(count);
      head.appendChild(spacer);
      head.appendChild(field);
      group.appendChild(head);

      rows.forEach(function (job) {
        var row = document.createElement("div");
        row.className = "hc-job content-card";

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

        // Cancel is available while a job is still pending or mid-conversion.
        // A pending job is dropped before it starts; an in-flight one finishes
        // its current file (in-process conversions are quick) then stops.
        if (job.status === "pending" || job.status === "converting") {
          var cancel = document.createElement("button");
          cancel.className = "action-button action-button-danger hc-job-cancel";
          cancel.textContent = "\u00d7";
          cancel.setAttribute("aria-label", "Cancel " + job.name);
          cancel.setAttribute("data-tooltip", "Cancel");
          cancel.addEventListener("click", function () {
            cancel.disabled = true;
            api().cancel_job(job.job_id).then(refreshQueue);
          });
          row.appendChild(cancel);
        } else {
          // Keep the grid column aligned across rows regardless of state.
          var spacerCell = document.createElement("span");
          spacerCell.className = "hc-job-cancel-slot";
          row.appendChild(spacerCell);
        }

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
      + " / " + groupCount + " group" + (groupCount === 1 ? "" : "s");
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
    el.drawerToggle.addEventListener("click", function () { setDrawer(!state.drawerOpen); });
    el.drawerClose.addEventListener("click", function () { setDrawer(false); });

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

    el.depCopy.addEventListener("click", copyDepCommand);
    el.depDismiss.addEventListener("click", closeDepModal);

    // Format finder - open on focus, filter on type, keyboard-navigable.
    el.finderInput.addEventListener("focus", openFinder);
    el.finderInput.addEventListener("input", function () {
      state.finderActive = -1;
      renderFinderList(el.finderInput.value.trim().toLowerCase());
    });
    el.finderInput.addEventListener("keydown", function (e) {
      if (el.finderList.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); moveFinderActive(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveFinderActive(-1); }
      else if (e.key === "Enter") {
        var m = state.finderMatches || [];
        var pick = state.finderActive >= 0 ? m[state.finderActive] : m[0];
        if (pick) { e.preventDefault(); chooseFormat(pick); }
      }
    });
    // Blur closes the list; the option's mousedown fires first so a pick still lands.
    el.finderInput.addEventListener("blur", function () {
      setTimeout(closeFinder, 100);
    });

    // Escape closes the dependency modal first, then the finder, then the drawer.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (isDepModalOpen()) closeDepModal();
      else if (!el.finderList.hidden) { closeFinder(); el.finderInput.blur(); }
      else if (state.drawerOpen) setDrawer(false);
    });

    // Palette changes are made in Hypervisor, so re-read on focus.
    window.addEventListener("focus", function () {
      if (!api()) return;
      api().get_theme().then(applyTheme).catch(function () {});
    });
  }

  function handleDrop(e) {
    // Path extraction and enqueue happen in Python: the dropped file's real OS
    // path is exposed only to the Python drop handler as `pywebviewFullPath`,
    // never to JS. This handler just clears the drag-over visual; Python calls
    // back through window.hcDropResult with the outcome.
    e.preventDefault();
    el.dropzone.classList.remove("hc-dragover");
  }

  // Python pushes the drop outcome here after it has resolved the OS paths and
  // enqueued them (see Api.on_drop). Mirrors what enqueue() does for browse.
  window.hcDropResult = function (res) {
    res = res || {};
    if (res.no_path) {
      toast("Could not read dropped file paths - use browse instead.", true);
    }
    var rej = res.rejected || [];
    if (rej.length) {
      var reasons = {};
      rej.forEach(function (r) { reasons[r.reason || "skipped"] = true; });
      toast(rej.length + " file(s) skipped: " + Object.keys(reasons).join(", ") + ".", true);
    }
    refreshQueue();
    if ((res.added || []).length) setDrawer(true);
  };

  function browseFiles() {
    // No mode gate - the dialog offers every supported input format.
    api().pick_files(null).then(function (paths) {
      if (paths && paths.length) enqueue(paths);
    });
  }

  function enqueue(paths) {
    api().add_jobs(paths).then(function (res) {
      var rej = res.rejected || [];
      if (rej.length) {
        // Report the actual reason rather than assuming "unsupported input" -
        // a path we couldn't read as a file is a different problem than a format
        // we don't handle, and the operator needs to know which.
        var reasons = {};
        rej.forEach(function (r) { reasons[r.reason || "skipped"] = true; });
        toast(rej.length + " file(s) skipped: " + Object.keys(reasons).join(", ") + ".", true);
      }
      // Dropping work is the cue to reveal the queue.
      refreshQueue();
      if ((res.added || []).length) setDrawer(true);
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
