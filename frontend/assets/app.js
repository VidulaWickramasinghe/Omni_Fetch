(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const INSPECTION_TIMEOUT_MS = 30_000;
  const REQUEST_TIMEOUT_MS = 15_000;

  const UI_STATE = Object.freeze({
    IDLE: "idle",
    INSPECTING: "inspecting",
    READY: "ready",
    CREATING: "creating",
    ACTIVE: "active",
    CANCELLING: "cancelling",
    COMPLETED: "completed",
    REJECTED: "rejected",
    FAILED: "failed",
    CANCELLED: "cancelled",
  });

  const ALLOWED_TRANSITIONS = {
    [UI_STATE.IDLE]: new Set([UI_STATE.INSPECTING, UI_STATE.FAILED]),
    [UI_STATE.INSPECTING]: new Set([UI_STATE.IDLE, UI_STATE.READY, UI_STATE.FAILED]),
    [UI_STATE.READY]: new Set([UI_STATE.IDLE, UI_STATE.INSPECTING, UI_STATE.CREATING]),
    [UI_STATE.CREATING]: new Set([UI_STATE.READY, UI_STATE.ACTIVE, UI_STATE.CANCELLING, UI_STATE.COMPLETED, UI_STATE.REJECTED, UI_STATE.FAILED, UI_STATE.CANCELLED]),
    [UI_STATE.ACTIVE]: new Set([UI_STATE.CANCELLING, UI_STATE.COMPLETED, UI_STATE.REJECTED, UI_STATE.FAILED, UI_STATE.CANCELLED]),
    [UI_STATE.CANCELLING]: new Set([UI_STATE.ACTIVE, UI_STATE.COMPLETED, UI_STATE.REJECTED, UI_STATE.FAILED, UI_STATE.CANCELLED]),
    [UI_STATE.COMPLETED]: new Set([UI_STATE.IDLE, UI_STATE.INSPECTING, UI_STATE.CREATING]),
    [UI_STATE.REJECTED]: new Set([UI_STATE.IDLE, UI_STATE.INSPECTING, UI_STATE.READY, UI_STATE.CREATING]),
    [UI_STATE.FAILED]: new Set([UI_STATE.IDLE, UI_STATE.INSPECTING, UI_STATE.READY, UI_STATE.CREATING]),
    [UI_STATE.CANCELLED]: new Set([UI_STATE.IDLE, UI_STATE.INSPECTING, UI_STATE.READY, UI_STATE.CREATING]),
  };

  const TERMINAL_JOB_STATUSES = new Set(["completed", "rejected", "failed", "cancelled"]);
  const VIDEO_MODES = new Set(["original", "mp4"]);

  const elements = {
    inspectForm: document.querySelector("#inspect-form"),
    urlInput: document.querySelector("#url-input"),
    inputTool: document.querySelector("#input-tool"),
    inspectButton: document.querySelector("#inspect-button"),
    accessNote: document.querySelector("#access-note"),
    authOption: document.querySelector("#auth-option"),
    authToggle: document.querySelector("#auth-toggle"),
    urlError: document.querySelector("#url-error"),
    inspectionPanel: document.querySelector("#inspection-panel"),
    cancelInspection: document.querySelector("#cancel-inspection"),
    resultPanel: document.querySelector("#result-panel"),
    mediaTitle: document.querySelector("#media-title"),
    mediaArt: document.querySelector("#media-art"),
    thumbnail: document.querySelector("#thumbnail"),
    liveBadge: document.querySelector("#live-badge"),
    platformBadge: document.querySelector("#platform-badge"),
    durationText: document.querySelector("#duration-text"),
    uploaderText: document.querySelector("#uploader-text"),
    actionError: document.querySelector("#action-error"),
    modeFieldset: document.querySelector("#mode-fieldset"),
    modeInputs: [...document.querySelectorAll('input[name="download-mode"]')],
    videoModeGroup: document.querySelector("#video-mode-group"),
    audioModeGroup: document.querySelector("#audio-mode-group"),
    qualityFieldset: document.querySelector("#quality-fieldset"),
    qualityList: document.querySelector("#quality-list"),
    selectionSummary: document.querySelector("#selection-summary"),
    selectionDetail: document.querySelector("#selection-detail"),
    downloadButton: document.querySelector("#download-button"),
    jobPanel: document.querySelector("#job-panel"),
    jobStatusBadge: document.querySelector("#job-status-badge"),
    jobTitle: document.querySelector("#job-title"),
    jobPhase: document.querySelector("#job-phase"),
    jobPercent: document.querySelector("#job-percent"),
    jobContextSource: document.querySelector("#job-context-source"),
    jobContextSelection: document.querySelector("#job-context-selection"),
    jobProgress: document.querySelector("#job-progress"),
    jobSteps: [...document.querySelectorAll("#job-steps li")],
    networkNotice: document.querySelector("#network-notice"),
    networkNoticeText: document.querySelector("#network-notice-text"),
    outputDetails: document.querySelector("#output-details"),
    outputName: document.querySelector("#output-name"),
    outputSize: document.querySelector("#output-size"),
    outputExpiry: document.querySelector("#output-expiry"),
    jobError: document.querySelector("#job-error"),
    jobErrorLabel: document.querySelector("#job-error-label"),
    jobErrorTitle: document.querySelector("#job-error-title"),
    jobErrorCopy: document.querySelector("#job-error-copy"),
    jobErrorGuidance: document.querySelector("#job-error-guidance"),
    jobErrorDetails: document.querySelector("#job-error-details"),
    jobErrorTechnical: document.querySelector("#job-error-technical"),
    cancelJob: document.querySelector("#cancel-job"),
    retryJob: document.querySelector("#retry-job"),
    changeOptions: document.querySelector("#change-options"),
    saveFile: document.querySelector("#save-file"),
    anotherDownload: document.querySelector("#another-download"),
    emptyState: document.querySelector("#empty-state"),
  };

  const model = {
    ui: UI_STATE.IDLE,
    inspectedUrl: null,
    inspectedUseAuth: null,
    pendingUrl: null,
    metadata: null,
    qualities: [],
    selectedMode: null,
    selectedQuality: null,
    job: null,
    error: null,
    failureScope: null,
    networkIssue: null,
    authAvailable: false,
    useAuth: false,
  };

  let inspectionController = null;
  let creationController = null;
  let thumbnailVersion = 0;

  const poller = {
    active: false,
    generation: 0,
    timer: null,
    controller: null,
    failures: 0,
    jobId: null,
  };

  class ApiError extends Error {
    constructor(status, message, payload = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  function transition(next, patch = {}) {
    if (next !== model.ui && !ALLOWED_TRANSITIONS[model.ui]?.has(next)) {
      console.warn(`Unexpected UI transition: ${model.ui} → ${next}`);
    }
    Object.assign(model, patch);
    model.ui = next;
    document.body.dataset.appState = next;
    render();
  }

  function render() {
    const resultVisible = Boolean(model.metadata) && (
      model.ui === UI_STATE.READY ||
      model.ui === UI_STATE.CREATING ||
      (model.ui === UI_STATE.FAILED && model.failureScope === "create")
    );
    const jobVisible = Boolean(model.job) && (
      model.ui === UI_STATE.ACTIVE ||
      model.ui === UI_STATE.CANCELLING ||
      model.ui === UI_STATE.COMPLETED ||
      model.ui === UI_STATE.REJECTED ||
      model.ui === UI_STATE.CANCELLED ||
      (model.ui === UI_STATE.FAILED && model.failureScope === "job")
    );
    const inspectionVisible = model.ui === UI_STATE.INSPECTING;
    const workflowLocked = model.ui === UI_STATE.CREATING || jobVisible;

    elements.inspectionPanel.hidden = !inspectionVisible;
    elements.resultPanel.hidden = !resultVisible;
    elements.jobPanel.hidden = !jobVisible;
    elements.emptyState.hidden = inspectionVisible || resultVisible || jobVisible;

    elements.urlInput.disabled = workflowLocked;
    elements.inputTool.disabled = workflowLocked;
    elements.inspectButton.disabled = workflowLocked || inspectionVisible;
    elements.authToggle.disabled = workflowLocked || inspectionVisible;
    elements.inspectForm.setAttribute("aria-busy", String(inspectionVisible));
    elements.inspectButton.classList.toggle("is-busy", inspectionVisible);
    elements.inspectButton.querySelector(".button__label").textContent = inspectionVisible ? "Inspecting…" : "Inspect URL";

    const creating = model.ui === UI_STATE.CREATING;
    elements.downloadButton.disabled = creating;
    elements.downloadButton.classList.toggle("is-busy", creating);
    elements.downloadButton.querySelector(".button__label").textContent = creating ? "Starting…" : "Start download";
    elements.modeFieldset.disabled = creating;
    elements.qualityFieldset.disabled = creating;
    elements.accessNote.textContent = model.useAuth ? "Authorized session" : "Public URLs";

    hideInlineError(elements.urlError);
    hideInlineError(elements.actionError);
    if (model.ui === UI_STATE.FAILED && model.failureScope === "inspect" && model.error) {
      showInlineError(elements.urlError, model.error);
    }
    if (model.ui === UI_STATE.FAILED && model.failureScope === "create" && model.error) {
      showInlineError(elements.actionError, model.error);
    }

    if (jobVisible) renderJob(model.job);
    updateInputTool();
  }

  function showInlineError(box, error) {
    box.querySelector("strong").textContent = error.summary;
    box.querySelector("span").textContent = error.copy || "";
    box.hidden = false;
    if (box === elements.urlError) elements.urlInput.setAttribute("aria-invalid", "true");
  }

  function hideInlineError(box) {
    box.hidden = true;
    box.querySelector("strong").textContent = "";
    box.querySelector("span").textContent = "";
    if (box === elements.urlError) elements.urlInput.removeAttribute("aria-invalid");
  }

  function updateInputTool() {
    const hasValue = elements.urlInput.value.trim().length > 0;
    elements.inputTool.textContent = hasValue ? "Clear" : "Paste";
    elements.inputTool.setAttribute("aria-label", hasValue ? "Clear media URL" : "Paste media URL from clipboard");
  }

  function parseSourceUrl() {
    const raw = elements.urlInput.value.trim();
    if (!raw) throw new Error("Paste a media URL first.");
    let parsed;
    try {
      parsed = new URL(raw);
    } catch {
      throw new Error("Enter a complete URL, including https://.");
    }
    if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
      throw new Error("Only http:// and https:// media URLs are supported.");
    }
    return parsed.href;
  }

  async function inspectSource(event) {
    event?.preventDefault();
    if (model.ui === UI_STATE.INSPECTING) return;

    let requestedUrl;
    const requestedAuth = model.authAvailable && elements.authToggle.checked;
    try {
      requestedUrl = parseSourceUrl();
    } catch (error) {
      transition(UI_STATE.FAILED, {
        inspectedUrl: null,
        inspectedUseAuth: null,
        metadata: null,
        error: { summary: "Check the URL", copy: error.message },
        failureScope: "inspect",
      });
      focusSoon(elements.urlError);
      return;
    }

    stopPolling();
    inspectionController?.abort();
    inspectionController = new AbortController();
    const controller = inspectionController;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, INSPECTION_TIMEOUT_MS);

    transition(UI_STATE.INSPECTING, {
      pendingUrl: requestedUrl,
      inspectedUrl: null,
      inspectedUseAuth: null,
      metadata: null,
      job: null,
      error: null,
      failureScope: null,
      networkIssue: null,
    });

    try {
      const data = await fetchJson(`${API_ROOT}/extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: requestedUrl, use_auth: requestedAuth }),
        signal: controller.signal,
      });
      if (controller !== inspectionController || controller.signal.aborted) return;
      if (!data || typeof data !== "object") throw new Error("The server returned an invalid media response.");
      if (!data.supports_video && !data.supports_audio) {
        throw new Error("No downloadable video or audio streams were found at this URL.");
      }

      model.inspectedUrl = requestedUrl;
      model.inspectedUseAuth = requestedAuth;
      model.pendingUrl = null;
      prepareResult(data);
      transition(UI_STATE.READY, { metadata: data, error: null, failureScope: null });
      focusSoon(elements.mediaTitle);
    } catch (error) {
      if (error.name === "AbortError" && !timedOut) return;
      const friendly = timedOut
        ? { summary: "The source took too long to respond", copy: "Try again in a moment or use another media URL." }
        : friendlyError(error, "inspect");
      transition(UI_STATE.FAILED, {
        pendingUrl: null,
        inspectedUrl: null,
        inspectedUseAuth: null,
        metadata: null,
        error: friendly,
        failureScope: "inspect",
      });
      focusSoon(elements.urlError);
    } finally {
      window.clearTimeout(timeout);
      if (inspectionController === controller) inspectionController = null;
    }
  }

  function cancelInspection() {
    inspectionController?.abort();
    inspectionController = null;
    transition(UI_STATE.IDLE, {
      pendingUrl: null,
      inspectedUrl: null,
      inspectedUseAuth: null,
      metadata: null,
      error: null,
      failureScope: null,
    });
    elements.urlInput.focus();
  }

  function prepareResult(metadata) {
    elements.mediaTitle.textContent = metadata.title || "Untitled media";
    elements.platformBadge.textContent = displayPlatform(metadata.platform);
    elements.durationText.textContent = metadata.is_live ? "Live stream" : formatDuration(metadata.duration);
    elements.liveBadge.hidden = !metadata.is_live;
    elements.uploaderText.textContent = metadata.uploader ? `By ${metadata.uploader}` : "";
    elements.uploaderText.hidden = !metadata.uploader;
    setThumbnail(metadata.thumbnail);

    const supportsVideo = Boolean(metadata.supports_video);
    const supportsAudio = Boolean(metadata.supports_audio);
    elements.videoModeGroup.hidden = !supportsVideo;
    elements.audioModeGroup.hidden = !supportsAudio;

    for (const input of elements.modeInputs) {
      input.disabled = VIDEO_MODES.has(input.value) ? !supportsVideo : !supportsAudio;
      input.checked = false;
    }

    model.selectedMode = supportsVideo ? "original" : "audio";
    const selectedModeInput = elements.modeInputs.find((input) => input.value === model.selectedMode);
    if (selectedModeInput) selectedModeInput.checked = true;

    renderQualities(metadata.qualities, supportsVideo);
    updateSelection();
  }

  function renderQualities(qualities, supportsVideo) {
    const supplied = Array.isArray(qualities) ? qualities.filter((quality) => quality && typeof quality === "object") : [];
    model.qualities = supplied.length
      ? supplied.map((quality, index) => normalizeQuality(quality, index))
      : supportsVideo
        ? [{ id: "best", label: "Best available", height: null, fps: null, note: "Highest quality offered by the source", estimated_size: null }]
        : [];
    model.selectedQuality = model.qualities[0] || null;

    const fragment = document.createDocumentFragment();
    model.qualities.forEach((quality, index) => {
      const label = document.createElement("label");
      label.className = "quality-card";

      const input = document.createElement("input");
      input.type = "radio";
      input.name = "video-quality";
      input.value = quality.id;
      input.checked = index === 0;
      input.dataset.qualityIndex = String(index);

      const body = document.createElement("span");
      body.className = "quality-card__body";
      const top = document.createElement("span");
      top.className = "quality-card__top";
      const title = document.createElement("strong");
      title.textContent = quality.label;
      top.append(title);
      const meta = document.createElement("span");
      meta.textContent = qualityMeta(quality);
      body.append(top, meta);

      const size = document.createElement("span");
      size.className = "quality-card__size";
      size.textContent = quality.estimated_size ? `≈ ${formatBytes(quality.estimated_size)}` : "";
      const mark = document.createElement("span");
      mark.className = "radio-mark";
      mark.setAttribute("aria-hidden", "true");
      label.append(input, body, size, mark);
      fragment.append(label);
    });
    elements.qualityList.replaceChildren(fragment);
  }

  function normalizeQuality(quality, index) {
    const height = Number(quality.height);
    const fps = Number(quality.fps);
    const estimatedSize = Number(quality.estimated_size);
    return {
      id: String(quality.id ?? `quality-${index}`),
      label: String(quality.label || (Number.isFinite(height) && height > 0 ? `${height}p` : "Best available")),
      height: Number.isFinite(height) && height > 0 ? Math.round(height) : null,
      fps: Number.isFinite(fps) && fps > 0 ? fps : null,
      note: quality.note ? String(quality.note) : "",
      estimated_size: Number.isFinite(estimatedSize) && estimatedSize > 0 ? estimatedSize : null,
    };
  }

  function qualityMeta(quality) {
    const pieces = [];
    if (quality.fps) pieces.push(`${formatNumber(quality.fps)} FPS`);
    if (quality.note && quality.note.toLowerCase() !== quality.label.toLowerCase()) pieces.push(quality.note);
    return pieces.join(" · ") || (quality.height ? `Up to ${quality.height} pixels high` : "Highest quality offered by the source");
  }

  function updateSelection() {
    const isVideo = VIDEO_MODES.has(model.selectedMode);
    elements.qualityFieldset.hidden = !isVideo;
    elements.qualityFieldset.disabled = !isVideo || model.ui === UI_STATE.CREATING;

    const qualityLabel = model.selectedQuality?.label || "Best available";
    const copy = {
      original: [`${qualityLabel} · Best source streams`, "The final container depends on the source formats."],
      mp4: [`${qualityLabel} · MP4 compatible`, "Prefers compatible MP4 video and M4A audio when available."],
      audio: ["Best source audio", "Keeps the source audio codec without conversion."],
      audio_mp3: ["MP3 audio", "Converted for compatibility; this is a lossy operation."],
    }[model.selectedMode] || ["Choose a format", ""];

    elements.selectionSummary.textContent = copy[0];
    elements.selectionDetail.textContent = model.inspectedUseAuth
      ? `${copy[1]} Uses the configured authenticated session.`
      : copy[1];
  }

  async function startDownload() {
    if (!model.inspectedUrl || !model.metadata || !model.selectedMode) return;
    stopPolling();
    creationController?.abort();
    creationController = new AbortController();
    const controller = creationController;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const maxHeight = VIDEO_MODES.has(model.selectedMode) ? model.selectedQuality?.height ?? null : null;

    transition(UI_STATE.CREATING, { error: null, failureScope: null, networkIssue: null, job: null });
    try {
      const job = await fetchJson(`${API_ROOT}/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: model.inspectedUrl,
          mode: model.selectedMode,
          max_height: maxHeight,
          use_auth: Boolean(model.inspectedUseAuth),
        }),
        signal: controller.signal,
      });
      if (!job?.job_id) throw new Error("The server did not return a job identifier.");
      applyJob(job);
      if (!TERMINAL_JOB_STATUSES.has(job.status)) startPolling(job.job_id);
    } catch (error) {
      const friendly = error.name === "AbortError"
        ? { summary: "The server did not start the download", copy: "The request timed out. Try again in a moment." }
        : friendlyError(error, "create");
      transition(UI_STATE.FAILED, { error: friendly, failureScope: "create", job: null });
      focusSoon(elements.actionError);
    } finally {
      window.clearTimeout(timeout);
      if (creationController === controller) creationController = null;
    }
  }

  function applyJob(job) {
    const normalized = { ...model.job, ...job };
    const next = uiStateForJob(normalized.status);
    const terminal = TERMINAL_JOB_STATUSES.has(normalized.status);
    if (terminal) stopPolling();
    transition(next, {
      job: normalized,
      error: null,
      failureScope: next === UI_STATE.FAILED ? "job" : null,
      networkIssue: null,
    });
  }

  function uiStateForJob(status) {
    if (status === "completed") return UI_STATE.COMPLETED;
    if (status === "rejected") return UI_STATE.REJECTED;
    if (status === "failed") return UI_STATE.FAILED;
    if (status === "cancelling") return UI_STATE.CANCELLING;
    if (status === "cancelled") return UI_STATE.CANCELLED;
    return UI_STATE.ACTIVE;
  }

  function startPolling(jobId) {
    stopPolling();
    poller.active = true;
    poller.jobId = jobId;
    poller.failures = 0;
    const generation = ++poller.generation;
    schedulePoll(generation, 0);
  }

  function stopPolling() {
    poller.active = false;
    poller.generation += 1;
    window.clearTimeout(poller.timer);
    poller.timer = null;
    poller.controller?.abort();
    poller.controller = null;
    poller.jobId = null;
    poller.failures = 0;
  }

  function schedulePoll(generation, delay) {
    if (!poller.active || generation !== poller.generation) return;
    window.clearTimeout(poller.timer);
    poller.timer = window.setTimeout(() => pollOnce(generation), delay);
  }

  async function pollOnce(generation) {
    if (!poller.active || generation !== poller.generation || !poller.jobId) return;
    const controller = new AbortController();
    poller.controller = controller;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let shouldContinue = true;

    try {
      const job = await fetchJson(`${API_ROOT}/jobs/${encodeURIComponent(poller.jobId)}`, { signal: controller.signal });
      if (!poller.active || generation !== poller.generation) return;
      poller.failures = 0;
      model.networkIssue = null;
      applyJob(job);
      shouldContinue = !TERMINAL_JOB_STATUSES.has(job.status);
    } catch (error) {
      if (!poller.active || generation !== poller.generation) return;
      if (error instanceof ApiError && error.status === 404) {
        shouldContinue = false;
        stopPolling();
        transition(UI_STATE.FAILED, {
          job: { ...model.job, status: "failed", error: "Job not found" },
          error: null,
          failureScope: "job",
          networkIssue: null,
        });
        return;
      }
      poller.failures += 1;
      model.networkIssue = poller.failures > 1
        ? `Retrying in ${Math.ceil(pollDelay(poller.failures) / 1000)} seconds…`
        : null;
      render();
    } finally {
      window.clearTimeout(timeout);
      if (poller.controller === controller) poller.controller = null;
    }

    if (shouldContinue && poller.active && generation === poller.generation) {
      const delay = document.hidden ? 5_000 : pollDelay(poller.failures);
      schedulePoll(generation, delay);
    }
  }

  function pollDelay(failures) {
    if (!failures) return 1_250;
    return Math.min(1_500 * (2 ** Math.min(failures - 1, 4)), 15_000);
  }

  async function cancelJob() {
    if (!model.job?.job_id || TERMINAL_JOB_STATUSES.has(model.job.status)) return;
    const jobId = model.job.job_id;
    stopPolling();
    transition(UI_STATE.CANCELLING, { job: { ...model.job, status: "cancelling" }, networkIssue: null });
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    try {
      const response = await fetchJson(`${API_ROOT}/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE", signal: controller.signal });
      const job = response?.job_id ? response : { ...model.job, status: response?.status || "cancelling" };
      applyJob(job);
      if (!TERMINAL_JOB_STATUSES.has(job.status)) startPolling(jobId);
    } catch (error) {
      transition(UI_STATE.ACTIVE, {
        job: { ...model.job, status: model.job.phase || "downloading" },
        networkIssue: "The cancellation request failed. The download may still be running.",
      });
      startPolling(jobId);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function renderJob(job) {
    const status = job.status || "queued";
    const labels = {
      queued: "Queued",
      inspecting: "Inspecting",
      downloading: "Downloading",
      processing: "Processing",
      completed: "Ready",
      rejected: "Rejected",
      failed: "Failed",
      cancelling: "Cancelling",
      cancelled: "Cancelled",
    };
    const tone = ["failed", "rejected", "cancelled"].includes(status) ? "danger" : status === "completed" ? "success" : "active";
    elements.jobStatusBadge.textContent = labels[status] || titleCase(status);
    elements.jobStatusBadge.dataset.tone = tone;
    elements.jobTitle.textContent = job.title || model.metadata?.title || "Preparing your download";
    elements.jobPhase.textContent = phaseCopy(job);
    elements.jobContextSource.textContent = [
      displayPlatform(job.platform || model.metadata?.platform),
      job.authenticated || model.inspectedUseAuth ? "Authorized session" : "Public access",
    ].join(" · ");
    elements.jobContextSelection.textContent = selectedDownloadLabel(job);

    const progress = clamp(Number(job.progress), 0, 100);
    const determinate = Number.isFinite(Number(job.progress)) && (progress > 0 || status === "completed");
    if (determinate) elements.jobProgress.value = status === "completed" ? 100 : progress;
    else elements.jobProgress.removeAttribute("value");
    elements.jobPercent.textContent = determinate ? `${formatNumber(status === "completed" ? 100 : progress)}%` : "—";
    updateSteps(job);

    elements.networkNotice.hidden = !model.networkIssue;
    elements.networkNoticeText.textContent = model.networkIssue || "";

    const completed = status === "completed";
    elements.outputDetails.hidden = !completed;
    elements.outputName.textContent = job.output_name || "Download";
    elements.outputSize.textContent = job.output_size ? formatBytes(job.output_size) : "Not reported";
    elements.outputExpiry.textContent = job.expires_at ? formatTimestamp(job.expires_at) : "Server default";

    const hasJobError = ["failed", "rejected", "cancelled"].includes(status);
    elements.jobPanel.classList.toggle("has-terminal-error", hasJobError);
    elements.jobError.hidden = !hasJobError;
    if (hasJobError) {
      const presentation = jobFailurePresentation(job);
      elements.jobErrorLabel.textContent = presentation.label;
      elements.jobErrorTitle.textContent = presentation.title;
      elements.jobErrorCopy.textContent = presentation.copy;
      elements.jobErrorGuidance.textContent = presentation.guidance;
      elements.jobErrorDetails.hidden = !job.error;
      elements.jobErrorTechnical.textContent = job.error
        ? `Status: ${status}\nJob: ${job.job_id}\nReason: ${job.error}`
        : "";
      elements.retryJob.textContent = presentation.retryLabel;
    }

    const cancellable = ["queued", "inspecting", "downloading", "processing"].includes(status);
    elements.cancelJob.hidden = !cancellable;
    elements.cancelJob.disabled = model.ui === UI_STATE.CANCELLING;
    elements.retryJob.hidden = !hasJobError;
    elements.changeOptions.hidden = !hasJobError || !model.metadata;
    elements.saveFile.hidden = !completed;
    elements.anotherDownload.hidden = !TERMINAL_JOB_STATUSES.has(status);
    if (completed) {
      elements.saveFile.href = safeDownloadUrl(job);
      if (job.output_name) elements.saveFile.setAttribute("download", job.output_name);
      else elements.saveFile.removeAttribute("download");
    }
  }

  function selectedDownloadLabel(job) {
    const quality = model.selectedQuality?.label;
    const labels = {
      original: `${quality || "Best available"} · Best source streams`,
      mp4: `${quality || "Best available"} · MP4 compatible`,
      audio: "Best source audio · No conversion",
      audio_mp3: "MP3 audio · Converted",
    };
    return labels[job.mode || model.selectedMode] || "Selected source quality";
  }

  function jobFailurePresentation(job) {
    const status = String(job.status || "failed").toLowerCase();
    const reason = String(job.error || "").trim();
    const normalized = reason.toLowerCase();
    if (status === "cancelled") {
      return {
        label: "Stopped safely",
        title: "Download cancelled",
        copy: "No finished file was created.",
        guidance: "You can restart with the same selection or change the format first.",
        retryLabel: "Restart download",
      };
    }
    if (normalized.includes("size limit")) {
      return {
        label: "Server limit reached",
        title: "This file is too large for this server",
        copy: reason,
        guidance: "Choose a lower video quality or audio-only mode, then try again.",
        retryLabel: "Retry download",
      };
    }
    if (normalized.includes("duration limit")) {
      return {
        label: "Server limit reached",
        title: "This media is longer than the server allows",
        copy: reason,
        guidance: "Use a shorter single-media URL or ask the server operator to raise the duration limit.",
        retryLabel: "Retry download",
      };
    }
    if (normalized.includes("authenticated session") || normalized.includes("login session")) {
      return {
        label: "Authorization required",
        title: "This source needs an authorized session",
        copy: reason,
        guidance: "Enable the configured login session, inspect the URL again, and confirm that account can view the media.",
        retryLabel: "Retry with session",
      };
    }
    if (normalized.includes("drm")) {
      return {
        label: "Protected media",
        title: "DRM-protected media cannot be downloaded",
        copy: reason,
        guidance: "OmniFetch does not bypass DRM or platform access controls.",
        retryLabel: "Retry download",
      };
    }
    if (normalized.includes("live stream")) {
      return {
        label: "Unsupported source type",
        title: "Live streams are not supported yet",
        copy: reason,
        guidance: "Try again after the broadcast becomes a normal replay video.",
        retryLabel: "Retry download",
      };
    }
    if (normalized.includes("duration is unavailable")) {
      return {
        label: "Source metadata changed",
        title: "The source omitted part of its metadata",
        copy: reason,
        guidance: "Retry now. OmniFetch can continue safely when finite media does not report a duration.",
        retryLabel: "Retry now",
      };
    }
    if (normalized.includes("ffmpeg is unavailable")) {
      return {
        label: "Server setup incomplete",
        title: "Media processing is not ready",
        copy: reason,
        guidance: "Restart OmniFetch after installing the current dependencies, or rebuild the Docker image.",
        retryLabel: "Retry after restart",
      };
    }
    if (normalized.includes("javascript challenge support")) {
      return {
        label: "Server setup incomplete",
        title: "YouTube support is not ready",
        copy: reason,
        guidance: "Install a supported JavaScript runtime and yt-dlp challenge scripts, then restart OmniFetch.",
        retryLabel: "Retry after restart",
      };
    }
    if (normalized.includes("browser verification")) {
      return {
        label: "Source verification changed",
        title: "The platform did not complete verification",
        copy: reason,
        guidance: "Retry once to request a fresh source page. If it persists, update and restart OmniFetch.",
        retryLabel: "Retry verification",
      };
    }
    if (normalized.includes("selected source format")) {
      return {
        label: "Source formats changed",
        title: "That quality is no longer available",
        copy: reason,
        guidance: "Inspect the URL again and choose one of the source's current quality options.",
        retryLabel: "Inspect again",
      };
    }
    if (normalized.includes("http 403")) {
      return {
        label: "Source access denied",
        title: "The source refused this transfer",
        copy: reason,
        guidance: "Inspect the URL again. If the media needs login, enable an authorized session that can view it.",
        retryLabel: "Inspect again",
      };
    }
    if (normalized.includes("http 404") || normalized.includes("link expired")) {
      return {
        label: "Media link expired",
        title: "The source changed its media link",
        copy: reason,
        guidance: "Inspect the original post again so OmniFetch can obtain a fresh media link.",
        retryLabel: "Inspect again",
      };
    }
    if (normalized.includes("media is unavailable")) {
      return {
        label: "Source unavailable",
        title: "This media is not available from the source",
        copy: reason,
        guidance: "Confirm the post still plays in your browser and that the configured account may view it.",
        retryLabel: "Inspect again",
      };
    }
    if (normalized.includes("source could not be downloaded")) {
      return {
        label: "Transfer interrupted",
        title: "The source did not deliver the media",
        copy: "The post was found, but its media transfer failed or expired.",
        guidance: "Retry once. If it still fails, inspect the URL again because the platform may have changed its media link.",
        retryLabel: "Retry transfer",
      };
    }
    return {
      label: status === "rejected" ? "Download stopped" : "Download interrupted",
      title: status === "rejected" ? "OmniFetch could not use this media" : "The download did not finish",
      copy: reason || "No finished file was created.",
      guidance: status === "rejected"
        ? "Review the reason below, change the selection if needed, and retry."
        : "Retry the transfer or inspect the source again if the platform changed the media link.",
      retryLabel: "Retry download",
    };
  }

  function updateSteps(job) {
    const phase = String(job.phase || "").toLowerCase();
    const status = String(job.status || "queued").toLowerCase();
    let current = 0;
    if (status === "completed") current = 3;
    else if (status === "processing" || phase.includes("process") || phase.includes("mux") || phase.includes("convert")) current = 2;
    else if (status === "downloading" || phase.includes("download")) current = 1;
    elements.jobSteps.forEach((step, index) => {
      step.classList.toggle("is-complete", index < current || status === "completed");
      step.classList.toggle("is-current", index === current && status !== "completed");
      step.classList.toggle(
        "is-error",
        index === current && ["failed", "rejected"].includes(status),
      );
      if (index === current && status !== "completed") step.setAttribute("aria-current", "step");
      else step.removeAttribute("aria-current");
    });
  }

  function phaseCopy(job) {
    if (job.status === "queued") return "Waiting for an available worker…";
    if (job.status === "inspecting") return "Checking the source and selected streams…";
    if (job.status === "downloading") return "Transferring the source media…";
    if (job.status === "processing") return "Preparing the final file…";
    if (job.status === "completed") return "Your file is ready to save.";
    if (job.status === "cancelling") return "Stopping the active job safely…";
    if (job.status === "cancelled") return "The job was stopped.";
    if (job.status === "rejected") return "Stopped while checking the source or selected stream.";
    if (job.status === "failed") return "The transfer ended before a file was ready.";
    return job.phase ? `${titleCase(job.phase)}…` : "Working…";
  }

  function safeDownloadUrl(job) {
    const fallback = `${API_ROOT}/jobs/${encodeURIComponent(job.job_id)}/file`;
    if (!job.download_url) return fallback;
    try {
      const resolved = new URL(job.download_url, window.location.origin);
      return resolved.origin === window.location.origin ? `${resolved.pathname}${resolved.search}${resolved.hash}` : fallback;
    } catch {
      return fallback;
    }
  }

  function resetApplication() {
    inspectionController?.abort();
    creationController?.abort();
    stopPolling();
    elements.urlInput.value = "";
    setThumbnail(null);
    transition(UI_STATE.IDLE, {
      inspectedUrl: null,
      inspectedUseAuth: null,
      pendingUrl: null,
      metadata: null,
      qualities: [],
      selectedMode: null,
      selectedQuality: null,
      job: null,
      error: null,
      failureScope: null,
      networkIssue: null,
    });
    elements.urlInput.focus();
  }

  function retryDownload() {
    if (model.inspectedUrl && model.metadata && model.selectedMode) {
      startDownload();
      return;
    }
    transition(UI_STATE.IDLE, { job: null, error: null, failureScope: null });
    focusSoon(elements.urlInput);
  }

  function changeDownloadOptions() {
    if (!model.metadata || !model.inspectedUrl) {
      resetApplication();
      return;
    }
    stopPolling();
    transition(UI_STATE.READY, {
      job: null,
      error: null,
      failureScope: null,
      networkIssue: null,
    });
    focusSoon(elements.mediaTitle);
  }

  function handleUrlInput() {
    hideInlineError(elements.urlError);
    updateInputTool();
    if (model.ui === UI_STATE.INSPECTING) {
      inspectionController?.abort();
      inspectionController = null;
      transition(UI_STATE.IDLE, { pendingUrl: null, error: null, failureScope: null });
      return;
    }
    if (model.inspectedUrl || (model.ui === UI_STATE.FAILED && model.failureScope === "inspect")) {
      transition(UI_STATE.IDLE, {
        inspectedUrl: null,
        inspectedUseAuth: null,
        metadata: null,
        qualities: [],
        selectedMode: null,
        selectedQuality: null,
        job: null,
        error: null,
        failureScope: null,
      });
    }
  }

  async function useInputTool() {
    if (elements.urlInput.value.trim()) {
      elements.urlInput.value = "";
      elements.urlInput.dispatchEvent(new Event("input", { bubbles: true }));
      elements.urlInput.focus();
      return;
    }
    try {
      if (!navigator.clipboard?.readText) throw new Error("Clipboard API unavailable");
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) throw new Error("Clipboard is empty");
      elements.urlInput.value = text;
      elements.urlInput.dispatchEvent(new Event("input", { bubbles: true }));
      elements.urlInput.focus();
    } catch {
      transition(UI_STATE.FAILED, {
        error: { summary: "Clipboard access is unavailable", copy: "Use your keyboard or device menu to paste the URL." },
        failureScope: "inspect",
      });
      focusSoon(elements.urlError);
    }
  }

  function handleAuthChange() {
    model.useAuth = model.authAvailable && elements.authToggle.checked;
    elements.accessNote.textContent = model.useAuth ? "Authorized session" : "Public URLs";
    if (model.inspectedUrl || model.metadata) {
      transition(UI_STATE.IDLE, {
        inspectedUrl: null,
        inspectedUseAuth: null,
        metadata: null,
        qualities: [],
        selectedMode: null,
        selectedQuality: null,
        job: null,
        error: null,
        failureScope: null,
      });
    } else {
      render();
    }
  }

  async function loadAuthenticationStatus() {
    try {
      const status = await fetchJson(`${API_ROOT}/auth/status`);
      model.authAvailable = Boolean(status?.enabled && status?.available);
    } catch {
      model.authAvailable = false;
    }
    if (!model.authAvailable) {
      elements.authToggle.checked = false;
      model.useAuth = false;
    }
    elements.authOption.hidden = !model.authAvailable;
    render();
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); }
      catch { payload = text; }
    }
    if (!response.ok) throw new ApiError(response.status, extractDetail(payload) || `Request failed (${response.status})`, payload);
    return payload;
  }

  function extractDetail(payload) {
    if (!payload) return "";
    if (typeof payload === "string") return payload;
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map((item) => item?.msg || String(item)).join("; ");
    if (typeof payload.message === "string") return payload.message;
    return "";
  }

  function friendlyError(error, scope) {
    if (error instanceof ApiError) {
      if (error.status === 429) return { summary: "This server is busy", copy: "Wait a moment, then try again." };
      if (error.status === 404) return { summary: "The job is no longer available", copy: "The server may have restarted or cleaned up an expired job." };
      if (error.status === 413) return { summary: "This media is too large", copy: "Choose a smaller quality or another source." };
      if (error.status === 409) return { summary: "Authenticated mode is unavailable", copy: error.message || "The configured login session is no longer available." };
      if (error.status === 400) return { summary: "This URL cannot be used", copy: error.message || "Check that it is a supported media URL." };
      if (error.status === 422) {
        const detail = String(error.message || "").toLowerCase();
        if (detail.includes("browser verification")) {
          return {
            summary: "The source changed its verification step",
            copy: "Update or restart OmniFetch, then inspect the original post again.",
          };
        }
        if (detail.includes("authorized login")) {
          return {
            summary: "This post needs an authorized session",
            copy: "Enable the configured login session and confirm that account can view the media.",
          };
        }
        if (detail.includes("not supported")) {
          return {
            summary: "This URL is not supported yet",
            copy: "Use the original post URL from a source supported by the current extraction engine.",
          };
        }
        if (detail.includes("unavailable")) {
          return {
            summary: "The source says this media is unavailable",
            copy: "Confirm the post still plays in your browser, then inspect it again.",
          };
        }
        return {
          summary: "No downloadable media was found",
          copy: error.message && !detail.includes("could not be inspected")
            ? error.message
            : "The post may be private, unsupported, expired, or unavailable.",
        };
      }
      if (error.status >= 500) return { summary: "The server hit a problem", copy: "Try again shortly. No download was started." };
    }
    if (error instanceof TypeError) return { summary: "Cannot reach the OmniFetch server", copy: "Check your connection and try again." };
    return {
      summary: scope === "create" ? "The download could not be started" : "This source could not be inspected",
      copy: error?.message || "Try again or use another media URL.",
    };
  }

  function setThumbnail(source) {
    const version = ++thumbnailVersion;
    elements.mediaArt.classList.remove("has-image");
    elements.thumbnail.removeAttribute("src");
    if (!source) return;
    elements.thumbnail.onload = () => {
      if (version === thumbnailVersion) elements.mediaArt.classList.add("has-image");
    };
    elements.thumbnail.onerror = () => {
      if (version === thumbnailVersion) elements.mediaArt.classList.remove("has-image");
    };
    elements.thumbnail.src = source;
  }

  function displayPlatform(value) {
    const key = String(value || "source").toLowerCase();
    const known = { youtube: "YouTube", twitter: "X / Twitter", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook", reddit: "Reddit", bluesky: "Bluesky", twitch: "Twitch" };
    return known[key] || titleCase(key);
  }

  function titleCase(value) {
    return String(value || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function formatDuration(value) {
    const total = Math.max(0, Math.round(Number(value) || 0));
    if (!total) return "Duration unavailable";
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}` : `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return "Unknown";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const amount = bytes / (1024 ** index);
    return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
  }

  function formatTimestamp(value) {
    const numeric = Number(value);
    const date = Number.isFinite(numeric)
      ? new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric)
      : new Date(value);
    if (Number.isNaN(date.getTime())) return "Server default";
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function formatNumber(value) {
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  function clamp(value, min, max) {
    if (!Number.isFinite(value)) return min;
    return Math.min(max, Math.max(min, value));
  }

  function focusSoon(element) {
    window.requestAnimationFrame(() => element?.focus({ preventScroll: false }));
  }

  elements.inspectForm.addEventListener("submit", inspectSource);
  elements.urlInput.addEventListener("input", handleUrlInput);
  elements.inputTool.addEventListener("click", useInputTool);
  elements.authToggle.addEventListener("change", handleAuthChange);
  elements.cancelInspection.addEventListener("click", cancelInspection);
  elements.downloadButton.addEventListener("click", startDownload);
  elements.cancelJob.addEventListener("click", cancelJob);
  elements.retryJob.addEventListener("click", retryDownload);
  elements.changeOptions.addEventListener("click", changeDownloadOptions);
  elements.anotherDownload.addEventListener("click", resetApplication);

  elements.modeInputs.forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      model.selectedMode = input.value;
      updateSelection();
    });
  });

  elements.qualityList.addEventListener("change", (event) => {
    const input = event.target.closest('input[name="video-quality"]');
    if (!input) return;
    model.selectedQuality = model.qualities[Number(input.dataset.qualityIndex)] || model.qualities[0] || null;
    updateSelection();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !poller.active || poller.controller) return;
    schedulePoll(poller.generation, 0);
  });

  window.addEventListener("pagehide", () => {
    inspectionController?.abort();
    creationController?.abort();
    stopPolling();
  });

  render();
  loadAuthenticationStatus();
})();
