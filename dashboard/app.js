/**
 * Review Intelligence MVP Dashboard Controller
 *
 * Handles:
 *  - Health check & LLM configuration discovery
 *  - Store metadata validation & App Store candidate picking
 *  - Analysis run creation & lifecycle polling
 *  - Live stage stepper & log tail streaming
 *  - Chart.js analytical charts rendering
 *  - Expandable issue cards & proof reviews
 *  - Unaddressed feedback export & Run history
 */

document.addEventListener("DOMContentLoaded", () => {
  // State variables
  let currentRunId = null;
  let pollingInterval = null;
  let activeCharts = {};

  // DOM Elements
  const setupBanner = document.getElementById("setup-banner");
  const runWarningBanner = document.getElementById("run-warning-banner");
  const runWarningText = document.getElementById("run-warning-text");

  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  const tabBtnProgress = document.getElementById("tab-btn-progress");
  const tabBtnReport = document.getElementById("tab-btn-report");

  // Form Elements
  const runForm = document.getElementById("run-form");
  const appNameInput = document.getElementById("app-name");
  const storePlayCheck = document.getElementById("store-play");
  const storeAppleCheck = document.getElementById("store-apple");
  const playPkgInput = document.getElementById("play-pkg");
  const appleIdInput = document.getElementById("apple-id");
  const grpPlayPkg = document.getElementById("grp-play-pkg");
  const grpAppleId = document.getElementById("grp-apple-id");
  const grpCandidates = document.getElementById("grp-candidates");
  const selectCandidates = document.getElementById("select-candidates");
  const btnValidate = document.getElementById("btn-validate");
  const validateSpinner = document.getElementById("validate-spinner");
  const valPreview = document.getElementById("validation-preview");
  const valIcon = document.getElementById("val-icon");
  const valTitle = document.getElementById("val-title");
  const valMeta = document.getElementById("val-meta");

  // Progress Elements
  const progressTitle = document.getElementById("progress-title");
  const progressSubtitle = document.getElementById("progress-subtitle");
  const progressStatusBox = document.getElementById("progress-status-box");
  const logTerminal = document.getElementById("log-terminal");
  const btnCancelRun = document.getElementById("btn-cancel-run");

  // History & Nav Buttons
  const navBtnNew = document.getElementById("nav-btn-new");
  const navBtnHistory = document.getElementById("nav-btn-history");
  const btnRefreshHistory = document.getElementById("btn-refresh-history");
  const tableHistoryBody = document.querySelector("#table-history tbody");

  // Report Elements
  const repAppName = document.getElementById("rep-app-name");
  const repMeta = document.getElementById("rep-meta");
  const repExecSummary = document.getElementById("rep-exec-summary");
  const repHighlights = document.getElementById("rep-highlights");
  const fnlFetched = document.getElementById("fnl-fetched");
  const fnlCleaned = document.getElementById("fnl-cleaned");
  const fnlClassified = document.getElementById("fnl-classified");
  const fnlCategories = document.getElementById("fnl-categories");
  const fnlCost = document.getElementById("fnl-cost");
  const tableActionPlanBody = document.querySelector("#table-action-plan tbody");
  const tableVersionBody = document.querySelector("#table-version-metrics tbody");
  const heatmapHead = document.getElementById("heatmap-head");
  const heatmapBody = document.getElementById("heatmap-body");
  const issuesContainer = document.getElementById("issues-container");
  const tableUnaddressedBody = document.querySelector("#table-unaddressed tbody");

  // Export links
  const expClean = document.getElementById("exp-clean");
  const expClass = document.getElementById("exp-class");
  const expUnaddressed = document.getElementById("exp-unaddressed");
  const expSummary = document.getElementById("exp-summary");
  const expAggregates = document.getElementById("exp-aggregates");
  const expTaxonomy = document.getElementById("exp-taxonomy");
  const expRaw = document.getElementById("exp-raw");
  const btnExportUnaddressed = document.getElementById("btn-export-unaddressed");
  const btnRerunFresh = document.getElementById("btn-rerun-fresh");
  const btnRerunReplay = document.getElementById("btn-rerun-replay");

  // -------------------------------------------------------------------
  // Tab Switching
  // -------------------------------------------------------------------
  function switchTab(tabId) {
    tabPanes.forEach(p => p.style.display = "none");
    tabBtns.forEach(b => b.classList.remove("active"));

    const targetPane = document.getElementById(tabId);
    if (targetPane) targetPane.style.display = "block";

    const targetBtn = document.querySelector(`[data-tab="${tabId}"]`);
    if (targetBtn) targetBtn.classList.add("active");
  }

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  navBtnNew.addEventListener("click", () => switchTab("tab-form"));
  navBtnHistory.addEventListener("click", () => {
    switchTab("tab-history");
    loadRunHistory();
  });
  btnRefreshHistory.addEventListener("click", loadRunHistory);

  // Store Toggles visibility
  storePlayCheck.addEventListener("change", () => {
    grpPlayPkg.style.display = storePlayCheck.checked ? "flex" : "none";
  });
  storeAppleCheck.addEventListener("change", () => {
    grpAppleId.style.display = storeAppleCheck.checked ? "flex" : "none";
  });

  // -------------------------------------------------------------------
  // Health Check
  // -------------------------------------------------------------------
  async function checkHealth() {
    try {
      const resp = await fetch("/health");
      const data = await resp.json();
      if (!data.llm_configured) {
        setupBanner.style.display = "flex";
      } else {
        setupBanner.style.display = "none";
      }
    } catch (err) {
      console.warn("Health check error:", err);
    }
  }
  checkHealth();

  // -------------------------------------------------------------------
  // App Identifier Validation
  // -------------------------------------------------------------------
  btnValidate.addEventListener("click", async () => {
    const stores = [];
    if (storePlayCheck.checked) stores.push("playstore");
    if (storeAppleCheck.checked) stores.push("appstore");

    if (stores.length === 0) {
      alert("Please select at least one store platform.");
      return;
    }

    validateSpinner.style.display = "inline";
    valPreview.style.display = "none";
    grpCandidates.style.display = "none";

    const payload = {
      stores: stores,
      play_package: playPkgInput.value.trim() || null,
      appstore_id: appleIdInput.value.trim() || null,
      app_name: appNameInput.value.trim() || null,
      country: "in",
    };

    try {
      const resp = await fetch("/apps/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const resData = await resp.json();
      validateSpinner.style.display = "none";

      if (!resp.ok) {
        alert(resData.error?.message || "Validation failed.");
        return;
      }

      // Check candidate matches for App Store
      if (resData.candidates && resData.candidates.length > 0) {
        selectCandidates.innerHTML = '<option value="">Select a matched App Store candidate...</option>';
        resData.candidates.forEach(c => {
          const opt = document.createElement("option");
          opt.value = c.id;
          opt.textContent = `${c.name} (${c.developer}) - ID: ${c.id}`;
          opt.dataset.icon = c.icon || "";
          opt.dataset.title = c.name;
          selectCandidates.appendChild(opt);
        });
        grpCandidates.style.display = "flex";
      }

      // Display preview for valid store
      let metaFound = null;
      if (resData.stores.playstore && resData.stores.playstore.found) {
        metaFound = resData.stores.playstore;
      } else if (resData.stores.appstore && resData.stores.appstore.found) {
        metaFound = resData.stores.appstore;
      }

      if (metaFound) {
        valIcon.src = metaFound.icon || "";
        valTitle.textContent = metaFound.name || appNameInput.value;
        valMeta.textContent = `Version: ${metaFound.current_version || "N/A"} | Rating: ${metaFound.rating ? metaFound.rating.toFixed(1) : "N/A"} (${metaFound.ratings_count || 0} reviews)`;
        valPreview.style.display = "flex";

        if (!appNameInput.value.trim() && metaFound.name) {
          appNameInput.value = metaFound.name;
        }
      } else {
        const errors = [];
        if (resData.stores.playstore?.error) errors.push(`Play: ${resData.stores.playstore.error}`);
        if (resData.stores.appstore?.error) errors.push(`AppStore: ${resData.stores.appstore.error}`);
        alert(`App validation issues:\n${errors.join("\n")}`);
      }
    } catch (err) {
      validateSpinner.style.display = "none";
      alert("Error contacting validation endpoint: " + err.message);
    }
  });

  // Handle Candidate Selection
  selectCandidates.addEventListener("change", () => {
    const selectedId = selectCandidates.value;
    if (selectedId) {
      appleIdInput.value = selectedId;
      const opt = selectCandidates.selectedOptions[0];
      if (opt && opt.dataset.icon) {
        valIcon.src = opt.dataset.icon;
        valTitle.textContent = opt.dataset.title;
        valPreview.style.display = "flex";
      }
    }
  });

  // -------------------------------------------------------------------
  // Run Submission
  // -------------------------------------------------------------------
  runForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    const stores = [];
    if (storePlayCheck.checked) stores.push("playstore");
    if (storeAppleCheck.checked) stores.push("appstore");

    if (stores.length === 0) {
      alert("Select at least one store.");
      return;
    }

    // Collect stars
    const starBoxes = document.querySelectorAll('input[name="stars"]:checked');
    const stars = Array.from(starBoxes).map(b => parseInt(b.value));

    const config = {
      app_name: appNameInput.value.trim(),
      stores: stores,
      play_package: playPkgInput.value.trim() || null,
      appstore_id: appleIdInput.value.trim() || null,
      country: "in",
      language_filter: document.getElementById("lang-filter").value,
      filters: {
        stars: stars.length > 0 ? stars : [1, 2, 3, 4, 5],
        date_from: document.getElementById("date-from").value || null,
        date_to: document.getElementById("date-to").value || null,
      },
      sample_size: parseInt(document.getElementById("sample-size").value) || 1000,
      taxonomy_sample_pct: (parseFloat(document.getElementById("tax-pct").value) || 10) / 100.0,
      taxonomy_sample_min: 150,
      taxonomy_sample_max: 300,
      top_k_proof_reviews: parseInt(document.getElementById("top-k").value) || 50,
      version_context: {
        baseline_versions: null,
        treat_dev_replied_as_addressed: true,
      },
    };

    try {
      const resp = await fetch("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });

      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error?.message || "Failed to initiate run.");
        return;
      }

      currentRunId = data.run_id;
      tabBtnProgress.style.display = "block";
      switchTab("tab-progress");
      startProgressPolling(currentRunId);

    } catch (err) {
      alert("Error submitting run: " + err.message);
    }
  });

  // -------------------------------------------------------------------
  // Progress Polling & Stepper
  // -------------------------------------------------------------------
  const STAGE_META = {
    s01_collect: { num: 1, label: "Collect", title: "Stage 1 of 7: Collecting reviews from app stores" },
    s02_clean: { num: 2, label: "Clean", title: "Stage 2 of 7: Cleaning, deduplicating & language filtering" },
    s03_sample_taxonomy: { num: 3, label: "Taxonomy", title: "Stage 3 of 7: Discovering complaint taxonomy with Gemini" },
    s04_classify: { num: 4, label: "Classify", title: "Stage 4 of 7: Classifying reviews against taxonomy" },
    s05_reconcile: { num: 5, label: "Reconcile", title: "Stage 5 of 7: Reconciling proposed categories & expanding taxonomy" },
    s06_summarize: { num: 6, label: "Summarize", title: "Stage 6 of 7: Summarizing issues & ranking proof reviews" },
    s07_aggregate: { num: 7, label: "Aggregate", title: "Stage 7 of 7: Computing aggregates & prioritized action plan" },
  };

  function startProgressPolling(runId) {
    if (pollingInterval) clearInterval(pollingInterval);
    currentRunId = runId;
    if (progressTitle) progressTitle.textContent = "Connecting to pipeline worker...";
    if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-neutral">Connecting...</span>`;
    if (progressStatusBox) {
      progressStatusBox.style.display = "none";
      progressStatusBox.innerHTML = "";
    }
    btnCancelRun.style.display = "inline-block";
    btnCancelRun.disabled = false;
    btnCancelRun.textContent = "Cancel run";
    resetStepper();

    const pollFn = async () => {
      try {
        const resp = await fetch(`/runs/${runId}`);
        if (!resp.ok) return;

        const runData = await resp.json();
        updateStepper(runData.stages);

        // Fetch live log tail
        const logResp = await fetch(`/runs/${runId}/logs?lines=60`);
        if (logResp.ok) {
          const logData = await logResp.json();
          if (logData.lines && logData.lines.length > 0) {
            logTerminal.textContent = logData.lines.join("\n");
            logTerminal.scrollTop = logTerminal.scrollHeight;
          }
        }

        // Determine active or failed stage
        const activeStage = (runData.stages || []).find(s => s.status === "running");
        const failedStage = (runData.stages || []).find(s => s.status === "failed");

        // Dynamic Header and Subtitle Mapping
        if (runData.status === "queued") {
          if (progressTitle) progressTitle.textContent = "Waiting in queue for pipeline worker...";
          if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-neutral">Queued</span>`;
        } else if (runData.status === "running") {
          if (activeStage && STAGE_META[activeStage.stage_name]) {
            const meta = STAGE_META[activeStage.stage_name];
            if (progressTitle) progressTitle.textContent = `${meta.title}...`;
            if (progressSubtitle) {
              const durText = activeStage.started_at ? ` (running for ${Math.max(1, Math.round((Date.now() - new Date(activeStage.started_at).getTime()) / 1000))}s)` : "";
              progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-info">Running</span> &bull; <strong>${meta.label}</strong>${durText}`;
            }
          } else {
            if (progressTitle) progressTitle.textContent = "Pipeline executing stages...";
            if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-info">Running</span>`;
          }
        }

        // Terminal state checks
        if (runData.status === "completed") {
          clearInterval(pollingInterval);
          pollingInterval = null;
          btnCancelRun.style.display = "none";
          if (progressTitle) progressTitle.textContent = "Analysis completed successfully!";
          if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-success">Completed</span>`;
          (runData.stages || []).forEach(stg => {
            const el = document.getElementById(`step-${stg.stage_name}`);
            if (el) el.className = "step-node completed";
          });
          tabBtnReport.style.display = "block";
          loadReport(runId);
          setTimeout(() => switchTab("tab-report"), 900);
        } else if (runData.status === "failed") {
          clearInterval(pollingInterval);
          pollingInterval = null;
          btnCancelRun.style.display = "none";
          const failMsg = runData.error?.message || "An error occurred during pipeline execution.";
          const failCode = runData.error?.code || "FAILED";
          if (progressTitle) progressTitle.textContent = "Analysis failed";
          if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-danger">Failed (${failCode})</span>`;
          if (progressStatusBox) {
            progressStatusBox.className = "status-alert status-alert-danger";
            progressStatusBox.style.display = "flex";
            progressStatusBox.innerHTML = `
              <div>
                <strong>Pipeline Error [${failCode}]:</strong> ${failMsg}
                ${failedStage ? `<div style="font-size:0.82rem; margin-top:0.3rem;">Failed at stage: <code>${failedStage.stage_name}</code></div>` : ""}
              </div>
              <div style="display:flex; gap:0.5rem;">
                <button class="btn btn-secondary btn-sm" onclick="window.resumeRun('${runId}')">Resume from checkpoint</button>
                <button class="btn btn-primary btn-sm" onclick="window.rerunRun('${runId}')">Retry run</button>
              </div>
            `;
          }
        } else if (runData.status === "cancelled") {
          clearInterval(pollingInterval);
          pollingInterval = null;
          btnCancelRun.style.display = "none";
          if (progressTitle) progressTitle.textContent = "Analysis cancelled";
          if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-warning">Cancelled</span>`;
          if (progressStatusBox) {
            progressStatusBox.className = "status-alert status-alert-warning";
            progressStatusBox.style.display = "flex";
            progressStatusBox.innerHTML = `
              <div>Run <code>${runId}</code> was cancelled by user request.</div>
              <button class="btn btn-secondary btn-sm" onclick="window.rerunRun('${runId}')">Restart Run</button>
            `;
          }
        } else if (runData.status === "interrupted") {
          clearInterval(pollingInterval);
          pollingInterval = null;
          btnCancelRun.style.display = "none";
          if (progressTitle) progressTitle.textContent = "Analysis interrupted";
          if (progressSubtitle) progressSubtitle.innerHTML = `Run ID: <code>${runId}</code> &bull; <span class="badge badge-neutral">Interrupted</span>`;
          if (progressStatusBox) {
            progressStatusBox.className = "status-alert status-alert-info";
            progressStatusBox.style.display = "flex";
            progressStatusBox.innerHTML = `
              <div>The application process or container restarted while this analysis was running. You can resume from the last saved stage checkpoint.</div>
              <button class="btn btn-primary btn-sm" onclick="window.resumeRun('${runId}')">Resume Run</button>
            `;
          }
        }
      } catch (err) {
        console.error("Polling error:", err);
      }
    };

    // Immediate initial poll followed by 2000ms periodic interval
    pollFn();
    pollingInterval = setInterval(pollFn, 2000);
  }

  function resetStepper() {
    const stages = [
      "s01_collect", "s02_clean", "s03_sample_taxonomy",
      "s04_classify", "s05_reconcile", "s06_summarize", "s07_aggregate"
    ];
    stages.forEach(stg => {
      const el = document.getElementById(`step-${stg}`);
      if (el) {
        el.className = "step-node";
      }
    });
  }

  function updateStepper(stages) {
    if (!stages || !Array.isArray(stages)) return;

    stages.forEach(stg => {
      const el = document.getElementById(`step-${stg.stage_name}`);
      if (el) {
        el.className = `step-node ${stg.status}`;
      }
    });
  }

  btnCancelRun.addEventListener("click", async () => {
    if (!currentRunId) return;
    if (!confirm("Are you sure you want to stop this run?")) return;
    try {
      btnCancelRun.disabled = true;
      btnCancelRun.textContent = "Cancelling...";
      if (progressSubtitle) {
        progressSubtitle.innerHTML += ` &bull; <span class="badge badge-warning">Cancelling...</span>`;
      }
      await fetch(`/runs/${currentRunId}/cancel`, { method: "POST" });
    } catch (err) {
      alert("Error requesting cancellation: " + err.message);
      btnCancelRun.disabled = false;
      btnCancelRun.textContent = "Cancel run";
    }
  });

  // -------------------------------------------------------------------
  // Load & Render Report
  // -------------------------------------------------------------------
  async function loadReport(runId) {
    currentRunId = runId;
    try {
      const resp = await fetch(`/runs/${runId}/report`);
      if (!resp.ok) {
        alert("Failed to load report data.");
        return;
      }
      const report = await resp.json();
      renderReportView(report);
      tabBtnReport.style.display = "block";
      switchTab("tab-report");
    } catch (err) {
      alert("Error loading report: " + err.message);
    }
  }

  function renderReportView(data) {
    const run = data.run;
    repAppName.textContent = run.app_name;
    repMeta.textContent = `Run: ${run.id} | Date: ${run.created_at ? new Date(run.created_at).toLocaleString() : "-"} | Status: ${run.status}`;

    // Warnings Banner
    if (run.warnings && run.warnings.length > 0) {
      const msgs = run.warnings.map(w => `[${w.code}] ${w.message}`).join(" | ");
      runWarningText.textContent = `Notices: ${msgs}`;
      runWarningBanner.style.display = "flex";
    } else {
      runWarningBanner.style.display = "none";
    }

    // Funnel counts
    const cnts = run.counts || {};
    fnlFetched.textContent = cnts.fetched_total || cnts.raw_total || 0;
    fnlCleaned.textContent = cnts.clean_total || 0;
    fnlClassified.textContent = cnts.classified_total || 0;
    fnlCategories.textContent = data.taxonomy?.categories?.length || 0;
    fnlCost.textContent = `$${(run.estimated_cost || 0).toFixed(4)}`;

    // Executive Summary
    repExecSummary.textContent = data.executive_summary || "No executive summary available.";
    repHighlights.innerHTML = "";
    if (data.key_highlights && data.key_highlights.length > 0) {
      const ul = document.createElement("ul");
      ul.style.paddingLeft = "1.25rem";
      ul.style.color = "var(--text-secondary)";
      data.key_highlights.forEach(h => {
        const li = document.createElement("li");
        li.textContent = h;
        li.style.marginBottom = "0.25rem";
        ul.appendChild(li);
      });
      repHighlights.appendChild(ul);
    }

    // Action Plan Table
    tableActionPlanBody.innerHTML = "";
    (data.prioritized_action_plan || []).forEach(item => {
      const tr = document.createElement("tr");

      let actionsHtml = "";
      if (item.actions && item.actions.length > 0) {
        actionsHtml = item.actions.map(a => `
          <div style="margin-bottom: 0.4rem;">
            <strong>${a.action}</strong>
            <span class="badge ${a.effort === 'S' ? 'badge-success' : a.effort === 'M' ? 'badge-info' : 'badge-warning'}">${a.effort}</span>
            <span class="badge badge-neutral">Impact: ${a.impact}/5</span>
            <div style="font-size: 0.8rem; color: var(--text-muted);">${a.rationale || ''}</div>
          </div>
        `).join("");
      } else {
        actionsHtml = `<span style="color: var(--text-muted); font-size: 0.85rem;">${item.narrative || 'Scheduled for investigation'}</span>`;
      }

      tr.innerHTML = `
        <td style="font-weight: 800; font-size: 1.1rem; color: var(--primary);">#${item.priority_rank}</td>
        <td>
          <strong>${item.category_name}</strong>
        </td>
        <td><span class="badge badge-info" style="font-size: 0.85rem;">${item.priority_score.toFixed(4)}</span></td>
        <td><strong>${item.review_count}</strong></td>
        <td>${actionsHtml}</td>
      `;
      tableActionPlanBody.appendChild(tr);
    });

    // Render Charts
    renderCharts(data.charts);

    // Version progression table
    tableVersionBody.innerHTML = "";
    (data.charts?.per_version_table || []).forEach(v => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${v.version}</strong></td>
        <td>${v.review_count}</td>
        <td>${v.avg_rating.toFixed(2)}</td>
        <td><span class="badge ${v.pct_complaints > 50 ? 'badge-danger' : 'badge-neutral'}">${v.pct_complaints}%</span></td>
        <td>${(v.top_categories || []).join(", ") || "None"}</td>
        <td>${v.dev_reply_rate_play !== null ? `${v.dev_reply_rate_play}%` : '<span style="color: var(--text-muted)">N/A (iOS)</span>'}</td>
      `;
      tableVersionBody.appendChild(tr);
    });

    // Heatmap Matrix
    renderHeatmap(data.charts?.category_version_heatmap, data.taxonomy?.categories);

    // Issue cards
    renderIssueCards(data);

    // Unaddressed feedback table
    tableUnaddressedBody.innerHTML = "";
    (data.unaddressed_feedback || []).slice(0, 50).forEach(u => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="badge ${u.rating <= 2 ? 'badge-danger' : 'badge-neutral'}">${u.rating}/5</span></td>
        <td><span class="badge badge-neutral">${u.store}</span></td>
        <td>${u.version}</td>
        <td><span class="badge badge-warning" style="font-size: 0.75rem;">${u.reason}</span></td>
        <td>${u.thumbs_up}</td>
        <td style="max-width: 450px; font-size: 0.85rem;">${u.content}</td>
      `;
      tableUnaddressedBody.appendChild(tr);
    });

    // Configure export links
    const exportBase = `/runs/${run.id}/export`;
    expClean.href = `${exportBase}/reviews_clean`;
    expClass.href = `${exportBase}/classifications`;
    expUnaddressed.href = `${exportBase}/unaddressed_feedback`;
    expSummary.href = `${exportBase}/summary`;
    expAggregates.href = `${exportBase}/aggregates`;
    expTaxonomy.href = `${exportBase}/taxonomy`;
    expRaw.href = `${exportBase}/raw_reviews`;
    btnExportUnaddressed.onclick = () => window.open(`${exportBase}/unaddressed_feedback`, "_blank");

    // Re-run handlers
    btnRerunFresh.onclick = () => rerunAnalysis(run.id, false);
    btnRerunReplay.onclick = () => rerunAnalysis(run.id, true);
  }

  // -------------------------------------------------------------------
  // Rerun Action
  // -------------------------------------------------------------------
  async function rerunAnalysis(runId, reuseSnapshot) {
    if (!confirm(reuseSnapshot ? "Replay pipeline on raw snapshot with LLM cache?" : "Trigger fresh scrape re-run?")) return;
    try {
      const resp = await fetch(`/runs/${runId}/rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reuse_snapshot: reuseSnapshot }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error?.message || "Failed to rerun.");
        return;
      }
      currentRunId = data.run_id;
      tabBtnProgress.style.display = "block";
      switchTab("tab-progress");
      startProgressPolling(currentRunId);
    } catch (err) {
      alert("Error: " + err.message);
    }
  }

  // -------------------------------------------------------------------
  // Chart.js Visualization Renderers
  // -------------------------------------------------------------------
  function renderCharts(chartsData) {
    if (!chartsData) return;

    // Destroy existing chart instances
    Object.keys(activeCharts).forEach(k => {
      if (activeCharts[k]) activeCharts[k].destroy();
    });

    // 1. Rating Distribution Chart
    const ratingsCtx = document.getElementById("chart-ratings")?.getContext("2d");
    if (ratingsCtx && chartsData.rating_distribution) {
      const rDist = chartsData.rating_distribution;
      const labels = ["1", "2", "3", "4", "5"];
      const playData = labels.map((_, i) => rDist.playstore?.[String(i + 1)] || 0);
      const appleData = labels.map((_, i) => rDist.appstore?.[String(i + 1)] || 0);

      activeCharts.ratings = new Chart(ratingsCtx, {
        type: "bar",
        data: {
          labels: labels,
          datasets: [
            { label: "Google Play", data: playData, backgroundColor: "#3b82f6" },
            { label: "App Store", data: appleData, backgroundColor: "#06b6d4" },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: { y: { beginAtZero: true } },
        },
      });
    }

    // 2. Volume Over Time Chart
    const volCtx = document.getElementById("chart-volume")?.getContext("2d");
    if (volCtx && chartsData.volume_over_time) {
      const vData = chartsData.volume_over_time;
      activeCharts.volume = new Chart(volCtx, {
        type: "line",
        data: {
          labels: vData.map(d => d.week),
          datasets: [
            {
              label: "Total Reviews",
              data: vData.map(d => d.total),
              borderColor: "#2563eb",
              backgroundColor: "rgba(37, 99, 235, 0.1)",
              fill: true,
              tension: 0.3,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: { y: { beginAtZero: true } },
        },
      });
    }

    // 3. Category Shares Chart
    const catCtx = document.getElementById("chart-categories")?.getContext("2d");
    if (catCtx && chartsData.category_shares) {
      const cData = chartsData.category_shares.slice(0, 10);
      activeCharts.categories = new Chart(catCtx, {
        type: "bar",
        data: {
          labels: cData.map(c => c.category_name),
          datasets: [
            {
              label: "Complaint Share (%)",
              data: cData.map(c => c.percentage),
              backgroundColor: "#8b5cf6",
            },
          ],
        },
        options: {
          indexAxis: "y",
          responsive: true,
          maintainAspectRatio: false,
          scales: { x: { beginAtZero: true, max: 100 } },
        },
      });
    }

    // 4. Per-Version Rating & Complaints Dual-Axis Chart
    const verCtx = document.getElementById("chart-versions")?.getContext("2d");
    if (verCtx && chartsData.per_version_table) {
      const vt = chartsData.per_version_table;
      activeCharts.versions = new Chart(verCtx, {
        type: "line",
        data: {
          labels: vt.map(v => v.version),
          datasets: [
            {
              label: "Avg Rating",
              data: vt.map(v => v.avg_rating),
              borderColor: "#10b981",
              yAxisID: "yRating",
              tension: 0.2,
            },
            {
              label: "Complaint %",
              data: vt.map(v => v.pct_complaints),
              borderColor: "#ef4444",
              borderDash: [5, 5],
              yAxisID: "yComplaint",
              tension: 0.2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            yRating: { type: "linear", position: "left", min: 1, max: 5 },
            yComplaint: { type: "linear", position: "right", min: 0, max: 100 },
          },
        },
      });
    }
  }

  // -------------------------------------------------------------------
  // Heatmap Renderer
  // -------------------------------------------------------------------
  function renderHeatmap(heatmapData, categories) {
    if (!heatmapData || !categories) return;

    heatmapHead.innerHTML = "<th>Category</th>";
    heatmapBody.innerHTML = "";

    // Collect versions
    const firstCat = Object.keys(heatmapData)[0];
    const versions = firstCat ? Object.keys(heatmapData[firstCat]) : [];

    versions.forEach(v => {
      const th = document.createElement("th");
      th.textContent = v;
      heatmapHead.appendChild(th);
    });

    categories.forEach(cat => {
      const tr = document.createElement("tr");
      const tdName = document.createElement("td");
      tdName.innerHTML = `<strong>${cat.name}</strong>`;
      tr.appendChild(tdName);

      versions.forEach(v => {
        const td = document.createElement("td");
        const val = heatmapData[cat.id]?.[v] || 0.0;
        td.textContent = `${val.toFixed(1)}%`;

        // Color cell according to intensity
        const intensity = Math.min(1.0, val / 40.0);
        td.style.backgroundColor = `rgba(239, 68, 68, ${intensity * 0.4})`;
        td.style.fontWeight = intensity > 0.5 ? "700" : "400";
        tr.appendChild(td);
      });

      heatmapBody.appendChild(tr);
    });
  }

  // -------------------------------------------------------------------
  // Issues Cards & Lazy Proof Reviews
  // -------------------------------------------------------------------
  function renderIssueCards(data) {
    issuesContainer.innerHTML = "";
    const catSummaries = data.category_summaries || {};
    const signals = data.charts?.version_resolution_signals || {};
    const plan = data.prioritized_action_plan || [];

    plan.forEach(item => {
      const cid = item.category_id;
      const cSummary = catSummaries[cid];
      if (!cSummary) return;

      const sig = signals[cid] || { signal: "insufficient_data", explanation: "" };
      let sigBadgeClass = "badge-neutral";
      if (sig.signal === "likely_resolved") sigBadgeClass = "badge-success";
      if (sig.signal === "regression") sigBadgeClass = "badge-danger";
      if (sig.signal === "persistent") sigBadgeClass = "badge-warning";

      const card = document.createElement("div");
      card.className = "issue-card";

      card.innerHTML = `
        <div class="issue-header" onclick="this.parentElement.querySelector('.issue-body').classList.toggle('hidden');">
          <div class="issue-title-group">
            <span class="badge badge-primary">#${item.priority_rank}</span>
            <div class="issue-title">${cSummary.category_name}</div>
            <span class="badge ${sigBadgeClass}">${sig.signal.replace('_', ' ')}</span>
            ${sig.dev_claims_fixed && sig.dev_claims_fixed.length > 0 ? '<span class="badge badge-info" title="Dev replies mention fixed">Dev Claims Fixed</span>' : ''}
          </div>
          <div>
            <span style="font-size: 0.85rem; color: var(--text-muted); margin-right: 0.5rem;">${cSummary.review_count} reviews</span>
            ▾
          </div>
        </div>
        <div class="issue-body">
          <p style="font-weight: 700; color: var(--text-primary); margin-bottom: 0.5rem;">${cSummary.headline}</p>
          <p style="font-size: 0.925rem; color: var(--text-secondary); line-height: 1.5; margin-bottom: 0.75rem;">${cSummary.summary}</p>

          <div style="margin: 0.75rem 0;">
            <strong style="font-size: 0.85rem; text-transform: uppercase; color: var(--text-muted);">Root Causes:</strong>
            <ul style="padding-left: 1.25rem; font-size: 0.875rem; margin-top: 0.25rem;">
              ${(cSummary.root_causes || []).map(rc => `<li>${rc}</li>`).join('')}
            </ul>
          </div>

          <div style="margin: 0.75rem 0;">
            <strong style="font-size: 0.85rem; text-transform: uppercase; color: var(--text-muted);">Affected Versions:</strong>
            <div style="display: flex; gap: 0.35rem; margin-top: 0.25rem;">
              ${(cSummary.affected_versions || []).map(v => `<span class="badge badge-neutral">${v}</span>`).join('')}
            </div>
          </div>

          <div style="margin-top: 1rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <strong style="font-size: 0.85rem; text-transform: uppercase; color: var(--text-muted);">Proof Reviews (Verbatim Citations):</strong>
              <button class="btn btn-secondary btn-sm" onclick="window.fetchProofReviews('${data.run.id}', '${cid}', this)">Load Proof Reviews</button>
            </div>
            <div class="proof-reviews-target" id="proof-target-${cid}">
              ${(cSummary.user_quotes || []).map(q => `<span class="badge badge-neutral" style="margin-right: 0.25rem;">ID: ${q}</span>`).join('')}
            </div>
          </div>
        </div>
      `;
      issuesContainer.appendChild(card);
    });
  }

  // Global helper for lazy loading proof reviews
  window.fetchProofReviews = async function(runId, catId, btnElement) {
    btnElement.disabled = true;
    btnElement.textContent = "Loading...";
    try {
      const resp = await fetch(`/runs/${runId}/categories/${catId}/reviews?limit=10&sort=score`);
      const res = await resp.json();
      const target = document.getElementById(`proof-target-${catId}`);
      if (!target) return;

      if (!res.reviews || res.reviews.length === 0) {
        target.innerHTML = "<p style='font-size: 0.85rem; color: var(--text-muted);'>No proof reviews returned.</p>";
        return;
      }

      target.innerHTML = res.reviews.map(r => `
        <div class="proof-review-box">
          <div style="display: flex; justify-content: space-between; margin-bottom: 0.25rem; font-size: 0.8rem; color: var(--text-muted);">
            <span><strong>${r.rating}/5</strong> | ${r.store} (${r.version})</span>
            <span>Relevance: ${r.relevance}/5 | Severity: ${r.severity}/5 | Upvotes: ${r.thumbs_up}</span>
          </div>
          <p style="color: var(--text-primary); font-size: 0.875rem;">${r.content}</p>
          ${r.note ? `<div style="font-size: 0.8rem; color: var(--primary); margin-top: 0.25rem;">Note: ${r.note}</div>` : ''}
          ${r.dev_reply ? `<div style="font-size: 0.8rem; background: var(--bg-surface); padding: 0.4rem; border-radius: 4px; margin-top: 0.35rem;"><strong>Developer Reply:</strong> ${r.dev_reply}</div>` : ''}
        </div>
      `).join("");
      btnElement.textContent = "Refreshed";
    } catch (err) {
      alert("Error loading reviews: " + err.message);
      btnElement.disabled = false;
      btnElement.textContent = "Retry";
    }
  };

  // -------------------------------------------------------------------
  // Load Run History
  // -------------------------------------------------------------------
  async function loadRunHistory() {
    tableHistoryBody.innerHTML = "<tr><td colspan='7'>Loading past runs...</td></tr>";
    try {
      const resp = await fetch("/runs");
      const runs = await resp.json();
      tableHistoryBody.innerHTML = "";

      if (!runs || runs.length === 0) {
        tableHistoryBody.innerHTML = "<tr><td colspan='7'>No analysis runs found. Create your first run!</td></tr>";
        return;
      }

      runs.forEach(r => {
        const tr = document.createElement("tr");
        const statusBadge = `<span class="badge ${r.status === 'completed' ? 'badge-success' : r.status === 'failed' ? 'badge-danger' : r.status === 'running' ? 'badge-info' : 'badge-neutral'}">${r.status}</span>`;
        tr.innerHTML = `
          <td><code>${r.id}</code></td>
          <td><strong>${r.app_name}</strong></td>
          <td>${r.created_at ? new Date(r.created_at).toLocaleDateString() : '-'}</td>
          <td>${statusBadge}</td>
          <td>${r.counts?.clean_total || r.counts?.fetched_total || 0}</td>
          <td>$${(r.estimated_cost || 0).toFixed(4)}</td>
          <td>
            <button class="btn btn-secondary btn-sm" onclick="window.viewRun('${r.id}', '${r.status}')">
              ${r.status === 'completed' ? 'View Report' : 'Check Progress'}
            </button>
          </td>
        `;
        tableHistoryBody.appendChild(tr);
      });
    } catch (err) {
      tableHistoryBody.innerHTML = `<tr><td colspan='7'>Error loading history: ${err.message}</td></tr>`;
    }
  }

  window.viewRun = function(runId, status) {
    if (status === "completed") {
      loadReport(runId);
    } else {
      currentRunId = runId;
      tabBtnProgress.style.display = "block";
      switchTab("tab-progress");
      startProgressPolling(runId);
    }
  };

  window.resumeRun = async function(runId) {
    try {
      const resp = await fetch(`/runs/${runId}/resume`, { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error?.message || "Failed to resume run.");
        return;
      }
      currentRunId = runId;
      tabBtnProgress.style.display = "block";
      switchTab("tab-progress");
      startProgressPolling(runId);
    } catch (err) {
      alert("Error resuming run: " + err.message);
    }
  };

  window.rerunRun = async function(runId) {
    try {
      const resp = await fetch(`/runs/${runId}/rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reuse_snapshot: false }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error?.message || "Failed to restart run.");
        return;
      }
      currentRunId = data.run_id;
      tabBtnProgress.style.display = "block";
      switchTab("tab-progress");
      startProgressPolling(currentRunId);
    } catch (err) {
      alert("Error restarting run: " + err.message);
    }
  };
});
