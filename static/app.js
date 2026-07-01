const elements = {
  chatLog: document.querySelector("#chat-log"),
  chatForm: document.querySelector("#chat-form"),
  chatInput: document.querySelector("#chat-input"),
  sendButton: document.querySelector("#send-button"),
  stepPill: document.querySelector("#step-pill"),
  fileQuestion: document.querySelector("#file-question"),
  floorPlanInput: document.querySelector("#floor-plan-input"),
  skipFileButton: document.querySelector("#skip-file-button"),
  inputSummary: document.querySelector("#input-summary"),
  floorPlanButton: document.querySelector("#floor-plan-button"),
  analyzeButton: document.querySelector("#analyze-button"),
  resetButton: document.querySelector("#reset-button"),
  statusText: document.querySelector("#status-text"),
  reportPanel: document.querySelector("#report-panel"),
  reportRoot: document.querySelector("#report-root"),
};

const initialState = () => ({
  step: "listing_url",
  messages: [],
  inputs: {
    url: "",
    notes: "",
    floorPlan: null,
    linkedFloorPlan: null,
    floorPlanSkipped: false,
  },
  checkingListing: false,
  analyzingFloorPlan: false,
  analyzing: false,
  generatingAlternatives: false,
  comparingLayouts: false,
  ready: false,
  resultsMode: false,
  currentAnalysis: null,
  alternatives: [],
  selectedAlternative: null,
  comparison: null,
});

let state = initialState();

init();

function init() {
  bindEvents();
  addBotMessage(
    "Cześć! Podeślij link do oferty dewelopera. Jeśli mam sprawdzić Geoportal, dopisz dokładny adres: miasto, ulica i numer.",
  );
  render();
}

function bindEvents() {
  elements.chatForm.addEventListener("submit", handleChatSubmit);
  elements.floorPlanInput.addEventListener("change", handleFileSelected);
  elements.skipFileButton.addEventListener("click", skipFloorPlan);
  elements.floorPlanButton.addEventListener("click", analyzeFloorPlanOnly);
  elements.analyzeButton.addEventListener("click", analyzeFlat);
  elements.resetButton.addEventListener("click", resetConversation);
  elements.reportRoot.addEventListener("click", handleReportAction);
}

async function handleChatSubmit(event) {
  event.preventDefault();
  const text = elements.chatInput.value.trim();
  if (!text) return;

  addUserMessage(text);
  elements.chatInput.value = "";

  if (state.step === "listing_url") {
    await handleListingAnswer(text);
  } else if (state.step === "floor_plan") {
    handleFloorPlanTextAnswer(text);
  } else if (state.step === "priorities") {
    handlePrioritiesAnswer(text);
  } else if (state.ready) {
    addBotMessage("Mam już komplet danych. Możesz uruchomić analizę przyciskiem po prawej stronie.");
  }

  render();
}

async function handleListingAnswerLegacy(text) {
  const url = extractUrl(text);

  if (!url) {
    addBotMessage("Potrzebuję linku zaczynającego się od http:// albo https://. Wklej proszę adres oferty.");
    return;
  }

  state.inputs.url = url;
  const possibleNotes = text.replace(url, "").trim();
  if (possibleNotes.length > 12) {
    appendNotes(possibleNotes);
  }

  state.step = "floor_plan";
  addBotMessage(
    "Super, mam link. Masz rzut mieszkania jako obraz albo PDF? Możesz go dodać tutaj albo kliknąć „Nie mam rzutu”.",
  );
}

async function handleListingAnswer(text) {
  const url = extractUrl(text);

  if (!url) {
    addBotMessage("Potrzebuje linku zaczynajacego sie od http:// albo https://. Wklej prosze adres oferty.");
    return;
  }

  state.inputs.url = url;
  const possibleNotes = text.replace(url, "").trim();
  if (possibleNotes.length > 12) {
    appendNotes(possibleNotes);
  }

  state.checkingListing = true;
  state.step = "checking_listing";
  addBotMessage("Mam link. Sprawdzam, czy strona oferty zawiera rzut mieszkania.");
  render();

  try {
    const preview = await fetchListingPreview(url);
    if (preview.linked_floor_plan) {
      state.inputs.linkedFloorPlan = preview.linked_floor_plan;
      state.inputs.floorPlanSkipped = false;
      addBotMessage("Znalazlem prawdopodobny rzut w ofercie, wiec nie musisz dodawac pliku.");
      finishFloorPlanStep();
      return;
    }

    state.step = "floor_plan";
    addBotMessage("Nie znalazlem wiarygodnego rzutu w ofercie. Dodaj rzut jako obraz albo PDF, albo kliknij przycisk pomijania.");
  } catch (error) {
    state.step = "floor_plan";
    addBotMessage(`Nie udalo sie sprawdzic rzutu w ofercie: ${error.message}. Dodaj rzut jako plik albo pomin ten krok.`);
  } finally {
    state.checkingListing = false;
    render();
  }
}

async function fetchListingPreview(url) {
  const formData = new FormData();
  formData.append("url", url);

  const response = await fetch("/api/listing-preview", {
    method: "POST",
    body: formData,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "Nie udalo sie sprawdzic oferty.");
  }
  return payload;
}

function handleFloorPlanTextAnswer(text) {
  if (isSkipIntent(text)) {
    state.inputs.floorPlanSkipped = true;
    finishFloorPlanStep();
    return;
  }

  addBotMessage("Jeśli masz rzut, dodaj plik przyciskiem pod rozmową. Jeśli nie masz, napisz „nie mam” albo kliknij przycisk pomijania.");
}

function handleFileSelected(event) {
  const file = event.target.files?.[0];
  if (!file) return;

  state.inputs.floorPlan = file;
  state.inputs.linkedFloorPlan = null;
  state.inputs.floorPlanSkipped = false;
  addUserMessage(`Dodałem rzut: ${file.name}`);
  finishFloorPlanStep();
  render();
}

function skipFloorPlan() {
  state.inputs.floorPlan = null;
  state.inputs.linkedFloorPlan = null;
  state.inputs.floorPlanSkipped = true;
  addUserMessage("Nie mam teraz rzutu mieszkania.");
  finishFloorPlanStep();
  render();
}

function finishFloorPlanStep() {
  if (state.inputs.notes.length >= 20) {
    markReady();
    return;
  }

  state.step = "priorities";
  addBotMessage(
    "Ostatnie pytanie: co mam szczególnie sprawdzić? Możesz też dopisać dokładny adres do Geoportalu, np. „Warszawa, ul. Marszałkowska 1”.",
  );
}

function handlePrioritiesAnswer(text) {
  appendNotes(text);
  markReady();
}

function markReady() {
  state.step = "ready";
  state.ready = true;
  addBotMessage("Mam komplet danych do analizy. Sprawdź podsumowanie po prawej i kliknij „Analizuj mieszkanie”.");
}

async function analyzeFlat() {
  if (!state.ready || state.analyzing) return;

  state.analyzing = true;
  state.resultsMode = true;
  elements.reportPanel.classList.remove("hidden");
  elements.reportRoot.innerHTML = `
    <section class="loading-state">
      <span class="badge">Analiza w toku</span>
      <h2>Przygotowuję wizualizację 3D i raport mieszkania.</h2>
      <p>Najpierw identyfikuję rzut w przesłanym pliku, potem sprawdzam Geoportal tylko wtedy, gdy podano dokładny adres.</p>
    </section>
  `;
  render();

  const formData = new FormData();
  formData.append("url", state.inputs.url);
  formData.append("notes", state.inputs.notes || "Brak dodatkowych priorytetów.");
  if (state.inputs.floorPlan) {
    formData.append("floor_plan", state.inputs.floorPlan);
  }

  try {
    const response = await fetch("/api/current-analysis", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.detail || "Nie udało się wykonać analizy.");
    }

    addBotMessage("Analiza gotowa. Raport pojawił się po prawej stronie.");
    state.currentAnalysis = payload.current_analysis;
    state.alternatives = [];
    state.selectedAlternative = null;
    state.comparison = null;
    renderReport(payload.report, payload.visualization, payload.geo_context, payload.floor_plan_analysis, state.currentAnalysis);
    elements.reportPanel.classList.remove("hidden");
  } catch (error) {
    addBotMessage(`Nie udało się wykonać analizy: ${error.message}`);
    elements.reportPanel.classList.remove("hidden");
    elements.reportRoot.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
  } finally {
    state.analyzing = false;
    render();
  }
}

async function analyzeFloorPlanOnly() {
  if (!state.inputs.url || state.analyzingFloorPlan || state.checkingListing) return;

  state.analyzingFloorPlan = true;
  state.resultsMode = true;
  elements.reportPanel.classList.remove("hidden");
  elements.reportRoot.innerHTML = `
    <section class="loading-state">
      <span class="badge">Floor plan</span>
      <h2>Analizuje tylko rzut mieszkania.</h2>
      <p>Szukam rzutu w ofercie albo uzywam przeslanego pliku, potem uruchamiam lokalny skan i interpretacje ukladu.</p>
    </section>
  `;
  render();

  const formData = new FormData();
  formData.append("url", state.inputs.url);
  formData.append("notes", state.inputs.notes || "");
  if (state.inputs.floorPlan) {
    formData.append("floor_plan", state.inputs.floorPlan);
  }

  try {
    const response = await fetch("/api/floor-plan-analysis", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Nie udalo sie przeanalizowac rzutu.");
    }
    addBotMessage(
      payload.acquisition?.status === "found"
        ? "Analiza rzutu jest gotowa."
        : "Nie znalazlem wiarygodnego rzutu do analizy.",
    );
    renderFloorPlanOnlyResult(payload);
    elements.reportPanel.classList.remove("hidden");
  } catch (error) {
    addBotMessage(`Nie udalo sie przeanalizowac rzutu: ${error.message}`);
    elements.reportPanel.classList.remove("hidden");
    elements.reportRoot.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
  } finally {
    state.analyzingFloorPlan = false;
    render();
  }
}

async function handleReportAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;

  if (button.dataset.action === "generate-alternatives") {
    await generateAlternatives();
    return;
  }

  if (button.dataset.action === "select-alternative") {
    const alternative = state.alternatives.find((item) => item.id === button.dataset.id);
    if (!alternative) return;
    state.selectedAlternative = alternative;
    renderWorkflowPanelOnly();
    return;
  }

  if (button.dataset.action === "compare-layouts") {
    await compareSelectedAlternative();
  }
}

async function generateAlternatives() {
  if (!state.currentAnalysis || state.generatingAlternatives) return;
  state.generatingAlternatives = true;
  renderWorkflowPanelOnly();

  const formData = new FormData();
  formData.append("current_analysis_json", JSON.stringify(state.currentAnalysis));
  formData.append("preferences", state.inputs.notes || "");

  try {
    const response = await fetch("/api/layout-alternatives", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Nie udalo sie wygenerowac wariantow.");
    }
    state.alternatives = payload.alternatives || [];
    state.selectedAlternative = state.alternatives[0] || null;
    state.comparison = null;
    addBotMessage("Warianty ukladu sa gotowe. Wybierz jeden do porownania.");
  } catch (error) {
    addBotMessage(`Nie udalo sie wygenerowac wariantow: ${error.message}`);
  } finally {
    state.generatingAlternatives = false;
    renderWorkflowPanelOnly();
    render();
  }
}

async function compareSelectedAlternative() {
  if (!state.currentAnalysis || !state.selectedAlternative || state.comparingLayouts) return;
  state.comparingLayouts = true;
  renderWorkflowPanelOnly();

  const formData = new FormData();
  formData.append("current_analysis_json", JSON.stringify(state.currentAnalysis));
  formData.append("selected_alternative_json", JSON.stringify(state.selectedAlternative));

  try {
    const response = await fetch("/api/compare-layouts", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Nie udalo sie porownac ukladow.");
    }
    state.comparison = payload;
    addBotMessage("Porownanie ukladu bazowego z wybranym wariantem jest gotowe.");
  } catch (error) {
    addBotMessage(`Nie udalo sie porownac ukladow: ${error.message}`);
  } finally {
    state.comparingLayouts = false;
    renderWorkflowPanelOnly();
    render();
  }
}

function renderWorkflowPanelOnly() {
  const panel = document.querySelector("#workflow-panel");
  if (panel) {
    const container = panel.closest("details") || panel;
    container.outerHTML = renderWorkflowPanel(
      state.currentAnalysis,
      state.alternatives,
      state.selectedAlternative,
      state.comparison,
    );
  }
}

function resetConversation() {
  state = initialState();
  elements.floorPlanInput.value = "";
  elements.reportPanel.classList.add("hidden");
  elements.reportRoot.innerHTML = "";
  addBotMessage(
    "Zaczynamy od nowa. Podeślij link do oferty dewelopera. Jeśli mam sprawdzić Geoportal, dopisz dokładny adres.",
  );
  render();
}

function render() {
  renderMode();
  renderMessages();
  renderStep();
  renderInputs();
}

function renderMode() {
  document.body.classList.toggle("ready-mode", state.ready);
  document.body.classList.toggle("results-mode", state.resultsMode);
}

function renderMessages() {
  elements.chatLog.innerHTML = state.messages
    .map(
      (message) => `
        <article class="message ${message.role}">
          <span>${message.role === "bot" ? "FlatWise" : "Ty"}</span>
          <p>${escapeHtml(message.text)}</p>
        </article>
      `,
    )
    .join("");
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
}

function renderStep() {
  const labels = {
    listing_url: "Pytanie 1/3",
    checking_listing: "Sprawdzam oferte",
    floor_plan: state.inputs.notes.length >= 20 ? "Pytanie 2/2" : "Pytanie 2/3",
    priorities: "Pytanie 3/3",
    ready: "Gotowe",
  };

  elements.stepPill.textContent = state.analyzing || state.analyzingFloorPlan ? "Analiza" : labels[state.step] || "Start";
  elements.fileQuestion.classList.toggle("hidden", state.step !== "floor_plan");
  elements.chatInput.disabled = state.analyzing || state.analyzingFloorPlan || state.checkingListing;
  elements.sendButton.disabled = state.analyzing || state.analyzingFloorPlan || state.checkingListing;
  elements.floorPlanButton.disabled = !state.inputs.url || state.analyzing || state.analyzingFloorPlan || state.checkingListing;
  elements.floorPlanButton.textContent = state.analyzingFloorPlan ? "Analizuje rzut..." : "Analizuj tylko rzut";
  elements.analyzeButton.disabled = !state.ready || state.analyzing || state.analyzingFloorPlan || state.checkingListing;
  elements.analyzeButton.textContent = state.analyzing ? "Analizuję..." : "Analizuj mieszkanie";

  if (state.checkingListing) {
    elements.chatInput.placeholder = "Sprawdzam obrazy w ofercie...";
  } else if (state.step === "floor_plan") {
    elements.chatInput.placeholder = "Napisz „nie mam”, jeśli chcesz pominąć rzut...";
  } else if (state.step === "priorities") {
    elements.chatInput.placeholder = "Np. cisza, dobry układ, odsprzedaż, dojazd...";
  } else {
    elements.chatInput.placeholder = "Napisz odpowiedź...";
  }
}

function renderInputs() {
  const rows = [
    ["Link do oferty", state.inputs.url || "Jeszcze nie podano"],
    ["Rzut mieszkania", formatFloorPlan()],
    ["Priorytety do analizy", state.inputs.notes || "Jeszcze nie podano"],
  ];

  elements.inputSummary.innerHTML = rows
    .map(
      ([label, value]) => `
        <div>
          <dt>${escapeHtml(label)}</dt>
          <dd>${escapeHtml(value)}</dd>
        </div>
      `,
    )
    .join("");

  if (state.checkingListing) {
    elements.statusText.textContent = "Sprawdzam obrazy z oferty, zeby znalezc rzut mieszkania.";
  } else if (state.analyzingFloorPlan) {
    elements.statusText.textContent = "Analizuje tylko rzut mieszkania.";
  } else if (state.analyzing) {
    elements.statusText.textContent = "Pobieram ofertę i przygotowuję raport. To może potrwać kilkanaście sekund.";
  } else if (state.ready) {
    elements.statusText.textContent = "Dane są gotowe. Możesz uruchomić analizę.";
  } else {
    elements.statusText.textContent = "Odpowiedz na pytania w czacie, żeby uruchomić analizę.";
  }
}

function renderFloorPlanOnlyResult(payload = {}) {
  const acquisition = payload.acquisition || {};
  const layoutReview = payload.layout_review || {};
  elements.reportRoot.innerHTML = `
    ${renderCollapsibleSegment(
      "Status pozyskania rzutu",
      `
        <div class="geo-grid">
          <div>
            <h4>Status</h4>
            <p>${escapeHtml(acquisition.status || "unknown")}</p>
          </div>
          <div>
            <h4>Zrodlo</h4>
            <p>${escapeHtml(acquisition.source || "none")}</p>
          </div>
          <div>
            <h4>Galeria</h4>
            <p>${escapeHtml(acquisition.browser_gallery_status?.status || "not_checked")}</p>
          </div>
        </div>
      `,
      { eyebrow: "Floor plan module", meta: payload.schema_version || "", open: true },
    )}
    ${renderSourceFloorPlan({ source_image_data_url: payload.source_image_data_url }, payload.floor_plan_analysis)}
    ${renderFloorPlanAnalysis(payload.floor_plan_analysis)}
    ${renderFloorPlanLayoutReview(layoutReview)}
  `;
}

function renderFloorPlanLayoutReview(review = {}) {
  if (!review || !review.status) return "";
  const detected = review.detected_layout || {};
  return renderCollapsibleSegment(
    "Interpretacja ukladu",
    `
      <section class="verdict">
        <span class="badge">${escapeHtml(review.status)}</span>
        <strong>${escapeHtml(review.summary || "Brak podsumowania.")}</strong>
        <p>Pewnosc: ${escapeHtml(String(review.confidence ?? "?"))}%</p>
      </section>
      <div class="geo-grid">
        <div>
          <h4>Pomieszczenia</h4>
          <p>${escapeHtml((detected.rooms || []).join(", ") || "brak danych")}</p>
        </div>
        <div>
          <h4>Strefy</h4>
          <p>${escapeHtml((detected.functional_zones || []).join(", ") || "brak danych")}</p>
        </div>
        <div>
          <h4>Komunikacja</h4>
          <p>${escapeHtml(detected.circulation || "brak danych")}</p>
        </div>
      </div>
      ${renderMiniList("Plusy", review.strengths)}
      ${renderMiniList("Ryzyka", review.risks)}
      ${renderMiniList("Ograniczenia remontowe do weryfikacji", review.renovation_constraints)}
      ${renderMiniList("Braki danych", review.missing_information)}
    `,
    { eyebrow: "Vision/layout", meta: `${escapeHtml(String(review.confidence ?? "?"))}%`, open: true },
  );
}

function renderReport(report, visualization = {}, geoContext = {}, floorPlanAnalysis = {}, currentAnalysis = null) {
  elements.reportRoot.innerHTML = `
    ${renderWorkflowPanel(currentAnalysis, state.alternatives, state.selectedAlternative, state.comparison)}
    ${renderSourceFloorPlan(visualization, floorPlanAnalysis)}
    ${renderVisualization(visualization)}
    ${renderFloorPlanAnalysis(floorPlanAnalysis)}

    ${renderCollapsibleSegment(
      "Rekomendacja",
      `
        <section class="verdict">
          <span class="badge">${escapeHtml(report.verdict || "Wymaga sprawdzenia")}</span>
          <strong>${escapeHtml(report.one_sentence || "Brak krótkiego podsumowania.")}</strong>
          <p>Pewność: ${escapeHtml(String(report.confidence ?? "?"))}%</p>
        </section>
      `,
      { eyebrow: "Wynik", meta: `${escapeHtml(String(report.confidence ?? "?"))}%`, open: true },
    )}
    ${renderModelDiagnostics(report)}

    ${renderCollapsibleSegment(
      "Oceny cząstkowe",
      `
        <section class="report-grid">
          ${renderAnalysisCard("Układ", report.layout_analysis)}
          ${renderAnalysisCard("Lokalizacja", report.location_analysis)}
          ${renderAnalysisCard("Finanse i odsprzedaż", report.financial_resale_view)}
        </section>
      `,
      { eyebrow: "Analiza", open: true },
    )}

    ${renderGeoContext(geoContext, report.geoportal_assessment)}
    ${renderOrientationAnalysis(report.orientation_analysis)}
    ${renderPriceMetrics(report.price_metrics_assessment)}
    ${renderListSection("Fakty z oferty", report.facts_found)}
    ${renderListSection("Ryzyka i deal-breakery", report.deal_breakers)}
    ${renderListSection("Pytania do dewelopera", report.questions_for_developer)}
    ${renderListSection("Następne kroki due diligence", report.due_diligence_next_steps)}
    ${renderListSection("Co zmieniłoby rekomendację", report.what_would_change_my_mind)}
    ${renderListSection("Notatki o źródłach", report.source_notes)}
    ${renderInfographics(report, geoContext)}
  `;
}

function renderModelDiagnostics(report = {}) {
  if (!report.raw_response && !report.parse_error) return "";

  return renderCollapsibleSegment(
    "Diagnostyka odpowiedzi modelu",
    `
      ${report.parse_error ? `<p class="error-box">${escapeHtml(report.parse_error)}</p>` : ""}
      ${report.raw_response ? `<pre>${escapeHtml(report.raw_response)}</pre>` : ""}
    `,
    { eyebrow: "JSON", open: true },
  );
}

function renderOrientationAnalysis(data = {}) {
  if (!data || (!data.status && !data.summary)) return "";

  const directions = Array.isArray(data.directions) ? data.directions.join(", ") : "";
  return renderCollapsibleSegment(
    "Orientacja mieszkania",
    `
      <div class="geo-section">
        <div class="geo-grid">
          <div>
            <h4>Status</h4>
            <p>${escapeHtml(data.status || "unknown")}</p>
          </div>
          <div>
            <h4>Kierunki</h4>
            <p>${escapeHtml(directions || "Brak potwierdzenia")}</p>
          </div>
          <div>
            <h4>Pewność</h4>
            <p>${escapeHtml(String(data.confidence ?? "?"))}%</p>
          </div>
          <div>
            <h4>Północ na rzucie</h4>
            <p>${escapeHtml(data.north_on_page || "unknown")}</p>
          </div>
        </div>
        <p>${escapeHtml(data.summary || "Brak opisu orientacji.")}</p>
        ${renderMiniList("Dowody", data.evidence)}
        ${renderMiniList("Ryzyka", data.risks)}
        ${renderMiniList("Braki danych", data.missing_information)}
      </div>
    `,
    { eyebrow: "Ekspozycja", meta: data.status || "", open: true },
  );
}

function renderSourceFloorPlan(visualization = {}) {
  if (!visualization.source_image_data_url) return "";
  return renderCollapsibleSegment(
    "Rzut zrodlowy",
    `
      <div>
        <figure>
          <img src="${escapeAttribute(visualization.source_image_data_url)}" alt="Rzut mieszkania uzyty jako zrodlo analizy" />
          <figcaption>Rzut z oferty lub przeslanego pliku</figcaption>
        </figure>
      </div>
    `,
    { eyebrow: "Floor plan", open: true, className: "visualization-section muted-visualization" },
  );
}

function renderWorkflowPanel(currentAnalysis, alternatives = [], selectedAlternative = null, comparison = null) {
  if (!currentAnalysis) return "";

  const selectedId = selectedAlternative?.id || "";
  const alternativesHtml = alternatives.length
    ? `
      <div class="geo-status-list">
        ${alternatives
          .map(
            (item) => `
              <article class="${item.id === selectedId ? "selected-option" : ""}">
                <strong>${escapeHtml(item.name || item.id || "Wariant")}</strong>
                <span>${escapeHtml(item.cost_level || "cost unknown")} cost / ${escapeHtml(item.renovation_risk || "risk unknown")} risk</span>
                <p>${escapeHtml(item.goal || "")}</p>
                ${renderListSection("Zmiany", item.changes)}
                ${renderListSection("Ograniczenia", item.constraints)}
                <button class="button secondary" type="button" data-action="select-alternative" data-id="${escapeAttribute(item.id)}">
                  Wybierz
                </button>
              </article>
            `,
          )
          .join("")}
      </div>
    `
    : `<p>Najpierw wygeneruj warianty ukladu na podstawie obecnej analizy.</p>`;

  const comparisonHtml = comparison
    ? `
      <div class="geo-section">
        <h4>Porownanie</h4>
        <p>${escapeHtml(comparison.summary || "")}</p>
        <div class="geo-status-list">
          ${(comparison.comparison || [])
            .map(
              (item) => `
                <article>
                  <strong>${escapeHtml(item.criterion || "Kryterium")}</strong>
                  <span>Wygrywa: ${escapeHtml(item.winner || "unknown")}</span>
                  <p>Obecnie: ${escapeHtml(item.original || "")}</p>
                  <p>Wariant: ${escapeHtml(item.alternative || "")}</p>
                </article>
              `,
            )
            .join("")}
        </div>
        ${renderListSection("Do potwierdzenia", comparison.must_verify_before_action)}
        ${renderListSection("Nastepne kroki", comparison.final_next_steps)}
      </div>
    `
    : "";

  return renderCollapsibleSegment(
    "Workflow analizy",
    `
      <div id="workflow-panel">
        <div class="geo-grid">
          <div>
            <h4>1. Current Analysis</h4>
            <p>${escapeHtml(currentAnalysis.schema_version || "ready")}</p>
          </div>
          <div>
            <h4>2. Alternatives</h4>
            <p>${escapeHtml(String(alternatives.length))}</p>
          </div>
          <div>
            <h4>3. Compare</h4>
            <p>${escapeHtml(comparison ? "gotowe" : "oczekuje")}</p>
          </div>
        </div>

        <div class="action-row">
          <button class="button primary" type="button" data-action="generate-alternatives" ${state.generatingAlternatives ? "disabled" : ""}>
            ${state.generatingAlternatives ? "Generuje..." : "Generate Alternatives"}
          </button>
          <button class="button secondary" type="button" data-action="compare-layouts" ${!selectedAlternative || state.comparingLayouts ? "disabled" : ""}>
            ${state.comparingLayouts ? "Porownuje..." : "Compare Selected"}
          </button>
        </div>

        ${alternativesHtml}
        ${comparisonHtml}
      </div>
    `,
    { eyebrow: "Etapy", meta: currentAnalysis.analysis_id || "", open: true },
  );
}

function renderFloorPlanAnalysis(data = {}) {
  if (!data || data.status !== "ready") return "";

  const wallSummary = data.wall_run_summary || {};
  const rooms = data.room_candidates || [];
  const cubicasa = data.cubicasa5k || {};
  const cubicasaCounts = cubicasa.raw_counts || {};
  return renderCollapsibleSegment(
    "Structured floor-plan scan",
    `
      <div class="geo-grid">
        <div>
          <h4>Estimated spaces</h4>
          <p>${escapeHtml(data.estimated_room_like_spaces ?? 0)}</p>
        </div>
        <div>
          <h4>Elongated spaces</h4>
          <p>${escapeHtml(data.elongated_space_count ?? 0)}</p>
        </div>
        <div>
          <h4>Ink ratio</h4>
          <p>${escapeHtml(Math.round((Number(data.ink_ratio) || 0) * 1000) / 10)}%</p>
        </div>
      </div>

      <div class="geo-section">
        <h4>CubiCasa5K adapter</h4>
        <p>${escapeHtml(cubicasa.status || "not_configured")}</p>
        <div class="buffer-list">
          <div><strong>${escapeHtml(cubicasaCounts.walls ?? 0)}</strong><span>walls</span></div>
          <div><strong>${escapeHtml(cubicasaCounts.doors ?? 0)}</strong><span>doors</span></div>
          <div><strong>${escapeHtml(cubicasaCounts.windows ?? 0)}</strong><span>windows</span></div>
          <div><strong>${escapeHtml(cubicasaCounts.rooms ?? 0)}</strong><span>rooms</span></div>
        </div>
      </div>

      <div class="geo-section">
        <h4>Wall-line signal</h4>
        <div class="buffer-list">
          <div><strong>${escapeHtml(wallSummary.horizontal_long_runs ?? 0)}</strong><span>horizontal runs</span></div>
          <div><strong>${escapeHtml(wallSummary.vertical_long_runs ?? 0)}</strong><span>vertical runs</span></div>
          <div><strong>${escapeHtml(wallSummary.longest_horizontal_ratio ?? 0)}</strong><span>longest horizontal ratio</span></div>
          <div><strong>${escapeHtml(wallSummary.longest_vertical_ratio ?? 0)}</strong><span>longest vertical ratio</span></div>
        </div>
      </div>

      ${
        rooms.length
          ? `
            <div class="geo-section">
              <h4>Largest room-like regions</h4>
              <div class="geo-status-list">
                ${rooms
                  .slice(0, 6)
                  .map(
                    (room, index) => `
                      <article>
                        <strong>Region ${index + 1}</strong>
                        <span>${escapeHtml(room.classification || "room_like_space")}</span>
                        <p>Area share: ${escapeHtml(room.area_ratio_of_drawing)}, aspect: ${escapeHtml(room.aspect_ratio)}</p>
                      </article>
                    `,
                  )
                  .join("")}
              </div>
            </div>
          `
          : ""
      }

      ${renderListSection("Detection limits", data.interpretation_notes)}
    `,
    {
      eyebrow: "Preprocessor",
      meta: data.method || "local",
      open: false,
      className: "floor-plan-analysis",
    },
  );
}

function renderGeoContext(context = {}, assessment = {}) {
  if (!context || context.status === "not_started") return "";

  const coordinates = context.coordinates?.wgs84
    ? `${context.coordinates.wgs84.lat}, ${context.coordinates.wgs84.lon}`
    : "Nie ustalono";
  const parcel = context.parcel?.id
    ? `${context.parcel.id}${context.parcel.number ? ` / nr ${context.parcel.number}` : ""}`
    : "Nie ustalono";

  return renderCollapsibleSegment(
    "Kontekst działki i otoczenia",
    `
      ${
        context.web_map_image_data_url
          ? `<img class="geo-map" src="${escapeAttribute(context.web_map_image_data_url)}" alt="Mapa pogladowa OpenStreetMap z lokalizacja mieszkania" />`
          : ""
      }
      ${
        context.map_image_data_url
          ? `<img class="geo-map" src="${escapeAttribute(context.map_image_data_url)}" alt="Mapa poglądowa z Geoportalu z lokalizacją mieszkania i buforami analizy" />`
          : ""
      }

      <div class="geo-grid">
        <div>
          <h4>Adres</h4>
          <p>${escapeHtml(context.address || "Nie ustalono jednoznacznego adresu.")}</p>
        </div>
        <div>
          <h4>Współrzędne</h4>
          <p>${escapeHtml(coordinates)}</p>
        </div>
        <div>
          <h4>Działka</h4>
          <p>${escapeHtml(parcel)}</p>
        </div>
      </div>

      ${renderMapLinks(context.map_links)}
      ${renderLocationValidation(context.location_validation)}
      ${renderGeoAssessment(assessment)}
      ${renderGeoSections("Planowanie", context.planning_context)}
      ${renderGeoSections("Otoczenie fizyczne", context.physical_context)}
      ${renderGeoSections("Infrastruktura", context.infrastructure_context)}
      ${renderWarsawContext(context.warsaw_context)}
      ${renderGeoSections("Rynek", context.market_context ? [context.market_context] : [])}
      ${renderGeoBuffers(context.buffers)}
      ${renderGeoIntersections(context.intersections)}
      ${renderGeoStatuses(context.service_status)}
      ${renderListSection("Ostrzeżenia Geoportalu", context.warnings)}
    `,
    {
      eyebrow: "Geoportal",
      meta: context.status || "partial",
      open: Boolean(context.address && context.status !== "missing_address"),
      className: "geo-context",
    },
  );
}

function renderGeoAssessment(data = {}) {
  if (!data || (!data.summary && data.score === undefined)) return "";

  return `
    <div class="geo-assessment">
      <h4>Ocena przestrzenna modelu <span>${escapeHtml(String(data.score ?? "?"))}/10</span></h4>
      ${data.summary ? `<p>${escapeHtml(data.summary)}</p>` : ""}
      <div class="price-metrics-grid">
        ${renderMetricColumn("Atuty", data.value_drivers)}
        ${renderMetricColumn("Ryzyka", data.risk_drivers)}
        ${renderMetricColumn("Braki danych", data.missing_information)}
      </div>
      ${renderMiniList("Bufory", data.buffers)}
      ${renderMiniList("Status źródeł", data.service_status)}
    </div>
  `;
}

function renderMapLinks(links = {}) {
  if (!links || (!links.google_maps && !links.openstreetmap)) return "";
  return `
    <div class="action-row">
      ${links.google_maps ? `<a class="button secondary" href="${escapeAttribute(links.google_maps)}" target="_blank" rel="noreferrer">Google Maps</a>` : ""}
      ${links.openstreetmap ? `<a class="button secondary" href="${escapeAttribute(links.openstreetmap)}" target="_blank" rel="noreferrer">OpenStreetMap</a>` : ""}
    </div>
  `;
}

function renderLocationValidation(data = {}) {
  if (!data || data.status === "missing") return "";
  const google = data.google_geocoding || null;
  const osm = data.osm_geocoding || null;
  const listing = data.listing_coordinates || null;
  return `
    <div class="geo-section">
      <h4>Walidacja lokalizacji</h4>
      <div class="geo-status-list">
        <article>
          <strong>Wybrane zrodlo</strong>
          <span>${escapeHtml(data.status || "unknown")}</span>
          <p>${escapeHtml(data.selected_provider || "Nie ustalono")}</p>
          ${data.distance_m ? `<p>Roznica mapa/oferta: ${escapeHtml(data.distance_m)} m</p>` : ""}
        </article>
        ${
          listing
            ? `
              <article>
                <strong>Dane z oferty</strong>
                <span>${listing.exact_address ? "exact" : "partial"}</span>
                <p>${escapeHtml(listing.address || "")}</p>
                <p>${escapeHtml(formatLatLon(listing))}</p>
              </article>
            `
            : ""
        }
        ${
          google
            ? `
              <article>
                <strong>Google Maps</strong>
                <span>${escapeHtml(google.location_type || "checked")}</span>
                <p>${escapeHtml(google.address || "")}</p>
                <p>${escapeHtml(formatLatLon(google))}</p>
                ${google.google_maps_url ? `<a href="${escapeAttribute(google.google_maps_url)}" target="_blank" rel="noreferrer">Otworz w Google Maps</a>` : ""}
              </article>
            `
            : ""
        }
        ${
          osm
            ? `
              <article>
                <strong>OpenStreetMap</strong>
                <span>${escapeHtml(osm.osm_class || osm.osm_type || "checked")}</span>
                <p>${escapeHtml(osm.display_address || osm.address || "")}</p>
                ${osm.geocoder_address && osm.geocoder_address !== (osm.display_address || osm.address) ? `<p>OSM match: ${escapeHtml(osm.geocoder_address)}</p>` : ""}
                ${osm.approximate ? `<p>Approximate street-level point.</p>` : ""}
                <p>${escapeHtml(formatLatLon(osm))}</p>
                ${osm.osm_url ? `<a href="${escapeAttribute(osm.osm_url)}" target="_blank" rel="noreferrer">Otworz w OSM</a>` : ""}
              </article>
            `
            : ""
        }
      </div>
      ${renderMiniList("Ostrzezenia lokalizacji", data.warnings)}
    </div>
  `;
}

function formatLatLon(value = {}) {
  if (value.lat === undefined || value.lon === undefined || value.lat === null || value.lon === null) return "";
  return `${value.lat}, ${value.lon}`;
}

function renderWarsawContext(context = {}) {
  if (!context || context.status === "not_warsaw") return "";
  const themes = context.themes || {};
  const sections = Object.entries(themes)
    .map(([theme, items]) => renderGeoSections(`Warszawa: ${theme}`, items))
    .join("");
  return `
    <div class="geo-section">
      <h4>Mapa Warszawy</h4>
      <p>${escapeHtml(context.summary || "")}</p>
      ${sections}
      ${renderGeoSections("Warszawa: halas", context.noise_context ? [context.noise_context] : [])}
      ${renderGeoSections("Warszawa: planowanie WMS", context.planning_wms_context ? [context.planning_wms_context] : [])}
      ${renderListSection("Ograniczenia danych Warszawy", context.warnings)}
    </div>
  `;
}

function renderGeoSections(title, items = []) {
  const usefulItems = (items || []).filter(Boolean);
  if (!usefulItems.length) return "";

  return `
    <div class="geo-section">
      <h4>${escapeHtml(title)}</h4>
      <div class="geo-status-list">
        ${usefulItems
          .map(
            (item) => `
              <article>
                <strong>${escapeHtml(item.service || "Źródło")}</strong>
                <span>${escapeHtml(item.status || "unknown")}</span>
                <p>${escapeHtml(item.summary || "Brak dodatkowego opisu.")}</p>
                ${renderFeatureProperties(item.features)}
              </article>
            `,
          )
          .join("")}
      </div>
    </div>
  `;
}

function renderFeatureProperties(features = []) {
  if (!features?.length) return "";
  return `
    <ul class="geo-feature-list">
      ${features
        .map((feature) => {
          const entries = Object.entries(feature || {}).slice(0, 6);
          return `<li>${entries.map(([key, value]) => `${escapeHtml(key)}: ${escapeHtml(value)}`).join(", ")}</li>`;
        })
        .join("")}
    </ul>
  `;
}

function renderGeoBuffers(buffers = []) {
  if (!buffers?.length) return "";
  return `
    <div class="geo-section">
      <h4>Bufory analizy</h4>
      <div class="buffer-list">
        ${buffers
          .map(
            (buffer) => `
              <div>
                <strong>${escapeHtml(buffer.radius_m)} m</strong>
                <span>${escapeHtml(buffer.use)}</span>
              </div>
            `,
          )
          .join("")}
      </div>
    </div>
  `;
}

function renderGeoIntersections(intersections = []) {
  if (!intersections?.length) return "";
  return `
    <div class="geo-section">
      <h4>Przecięcia i bufory</h4>
      <div class="geo-status-list">
        ${intersections
          .map(
            (item) => `
              <article>
                <strong>${escapeHtml(item.label || item.theme || "Warstwa")}</strong>
                <span>${escapeHtml(item.status || "unknown")}</span>
                <p>${escapeHtml(item.summary || "Brak opisu.")}</p>
                <p>Bufory: ${escapeHtml((item.buffers_m || []).join(" m, "))}${item.buffers_m?.length ? " m" : ""}</p>
              </article>
            `,
          )
          .join("")}
      </div>
    </div>
  `;
}

function renderGeoStatuses(statuses = []) {
  if (!statuses?.length) return "";
  return `
    <details class="geo-status-details">
      <summary>Status usług Geoportalu</summary>
      <ul>
        ${statuses
          .map(
            (item) => `
              <li>
                <strong>${escapeHtml(item.service || "Usługa")}:</strong>
                ${escapeHtml(item.status || "unknown")}
              </li>
            `,
          )
          .join("")}
      </ul>
    </details>
  `;
}

function renderPriceMetrics(data = {}) {
  if (!data || (!data.summary && data.score === undefined)) return "";

  return renderCollapsibleSegment(
    "Ocena według metryk wartości mieszkania",
    `
      ${data.summary ? `<p>${escapeHtml(data.summary)}</p>` : ""}
      <div class="price-metrics-grid">
        ${renderMetricColumn("Co wspiera wartość", data.value_drivers)}
        ${renderMetricColumn("Co obniża atrakcyjność", data.risk_drivers)}
        ${renderMetricColumn("Brakujące dane", data.missing_information)}
      </div>
    `,
    {
      eyebrow: "Metryki ceny",
      meta: `${escapeHtml(String(data.score ?? "?"))}/10`,
      open: true,
      className: "price-metrics",
    },
  );
}

function renderMetricColumn(title, items = []) {
  if (!items?.length) return "";
  return `
    <div class="metric-column">
      <h4>${escapeHtml(title)}</h4>
      <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </div>
  `;
}

function renderVisualization(visualization = {}) {
  if (visualization.status === "disabled") return "";
  if (visualization.status === "ready" && visualization.image_data_url) {
    return renderCollapsibleSegment(
      "Wizualizacja rzutu mieszkania",
      `
        <img src="${escapeAttribute(visualization.image_data_url)}" alt="Wizualizacja 3D rzutu mieszkania wygenerowana na podstawie przesłanego planu" />
      `,
      { eyebrow: "Wizualizacja 3D", open: true, className: "visualization-section" },
    );
  }

  if (visualization.source_image_data_url) {
    return renderCollapsibleSegment(
      "Nie udało się wygenerować wizualizacji",
      `
        <img src="${escapeAttribute(visualization.source_image_data_url)}" alt="Grafika rzutu mieszkania wyodrębniona z przesłanego pliku" />
        <p>${escapeHtml(visualization.message || "Pokazuję zidentyfikowany rzut źródłowy zamiast wizualizacji 3D.")}</p>
      `,
      { eyebrow: "Wizualizacja 3D", open: true, className: "visualization-section muted-visualization" },
    );
  }

  return renderCollapsibleSegment(
    "Brak wizualizacji rzutu",
    `
      <p>${escapeHtml(visualization.message || "Nie dodano rzutu albo nie udało się zidentyfikować grafiki w PDF.")}</p>
    `,
    { eyebrow: "Wizualizacja 3D", open: false, className: "visualization-section muted-visualization" },
  );
}

function renderAnalysisCard(title, data = {}) {
  return `
    <details class="analysis-card" open>
      <summary>
        <h3>${escapeHtml(title)} <span>${escapeHtml(String(data.score ?? "?"))}/10</span></h3>
        <span class="collapse-icon" aria-hidden="true">⌄</span>
      </summary>
      <div class="analysis-card-body">
        ${renderMiniList("Plusy", data.positives)}
        ${renderMiniList("Ryzyka", data.risks)}
        ${renderMiniList("Braki danych", data.missing_information)}
      </div>
    </details>
  `;
}

function renderMiniList(title, items = []) {
  if (!items?.length) return "";
  return `
    <div class="list-section compact">
      <h4>${escapeHtml(title)}</h4>
      <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </div>
  `;
}

function renderListSection(title, items = []) {
  if (!items?.length) return "";
  return renderCollapsibleSegment(
    title,
    `
    <section class="list-section">
      <ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </section>
  `,
    { eyebrow: "Sekcja", meta: String(items.length), open: true },
  );
}

function renderCollapsibleSegment(title, body, options = {}) {
  if (!body) return "";
  const openAttribute = options.open === false ? "" : " open";
  const className = options.className ? ` ${options.className}` : "";
  const meta = options.meta ? `<span class="segment-meta">${escapeHtml(options.meta)}</span>` : "";

  return `
    <details class="collapsible-segment${className}"${openAttribute}>
      <summary>
        <div>
          ${options.eyebrow ? `<p class="eyebrow">${escapeHtml(options.eyebrow)}</p>` : ""}
          <h3>${escapeHtml(title)}</h3>
        </div>
        <span class="segment-actions">
          ${meta}
          <span class="collapse-icon" aria-hidden="true">⌄</span>
        </span>
      </summary>
      <div class="segment-body">${body}</div>
    </details>
  `;
}

function renderInfographics(report = {}) {
  const scoreItems = [
    ["Układ", report.layout_analysis?.score],
    ["Lokalizacja", report.location_analysis?.score],
    ["Finanse", report.financial_resale_view?.score],
    ["Cena", report.price_metrics_assessment?.score],
    ["Geo", report.geoportal_assessment?.score],
  ].filter(([, score]) => score !== undefined && score !== null);

  const evidenceItems = [
    ["Plusy", countItems(report.layout_analysis?.positives) + countItems(report.location_analysis?.positives) + countItems(report.financial_resale_view?.positives)],
    ["Ryzyka", countItems(report.layout_analysis?.risks) + countItems(report.location_analysis?.risks) + countItems(report.financial_resale_view?.risks) + countItems(report.deal_breakers)],
    ["Braki danych", countItems(report.layout_analysis?.missing_information) + countItems(report.location_analysis?.missing_information) + countItems(report.financial_resale_view?.missing_information)],
  ];

  const maxEvidence = Math.max(...evidenceItems.map(([, value]) => value), 1);
  const recommendation = recommendationPercent(report);

  return renderCollapsibleSegment(
    "Infografiki podsumowujące",
    `
      <section class="infographics">
        <article class="info-panel">
          <h4>Profil ocen</h4>
          <div class="score-bars">
            ${scoreItems.map(([label, score]) => renderScoreBar(label, score)).join("")}
          </div>
        </article>

        <article class="info-panel">
          <h4>Bilans analizy</h4>
          <div class="evidence-bars">
            ${evidenceItems
              .map(
                ([label, value]) => `
                  <div class="evidence-row">
                    <span>${escapeHtml(label)}</span>
                    <div><i style="width: ${Math.round((value / maxEvidence) * 100)}%"></i></div>
                    <strong>${escapeHtml(value)}</strong>
                  </div>
                `,
              )
              .join("")}
          </div>
        </article>

        <article class="info-panel">
          <h4>Rekomendacja</h4>
          <div class="recommendation-donut" style="${recommendationDonutStyle(recommendation)}">
            <span>${escapeHtml(recommendation)}%</span>
          </div>
          <p class="recommendation-copy">${escapeHtml(recommendationLabel(report.verdict, recommendation))}</p>
        </article>
      </section>
    `,
    { eyebrow: "Na koniec", open: true, className: "infographics-segment" },
  );
}

function renderScoreBar(label, score) {
  const normalized = Math.max(0, Math.min(10, Number(score) || 0));
  return `
    <div class="score-row">
      <span>${escapeHtml(label)}</span>
      <div><i style="width: ${normalized * 10}%"></i></div>
      <strong>${escapeHtml(normalized)}/10</strong>
    </div>
  `;
}

function countItems(items = []) {
  return Array.isArray(items) ? items.length : 0;
}

function recommendationPercent(report = {}) {
  const confidence = clampPercent(Number(report.confidence));
  const scores = [
    report.layout_analysis?.score,
    report.location_analysis?.score,
    report.financial_resale_view?.score,
    report.price_metrics_assessment?.score,
    report.geoportal_assessment?.score,
  ]
    .map((score) => Number(score))
    .filter((score) => Number.isFinite(score));

  const scoreAverage = scores.length
    ? Math.round((scores.reduce((sum, score) => sum + score, 0) / scores.length) * 10)
    : null;

  const verdictBaseline = verdictPercent(report.verdict);
  const parts = [confidence, scoreAverage, verdictBaseline].filter((value) => value !== null);
  if (!parts.length) return 50;
  return clampPercent(Math.round(parts.reduce((sum, value) => sum + value, 0) / parts.length));
}

function verdictPercent(verdict = "") {
  const normalized = String(verdict).toLowerCase();
  if (normalized.includes("dobry")) return 82;
  if (normalized.includes("rozważenia") || normalized.includes("rozwazenia")) return 65;
  if (normalized.includes("unikać") || normalized.includes("unikac")) return 25;
  if (normalized.includes("wymaga")) return 50;
  return null;
}

function clampPercent(value) {
  if (!Number.isFinite(value)) return null;
  return Math.max(0, Math.min(100, Math.round(value)));
}

function recommendationDonutStyle(percent) {
  return `--recommendation: conic-gradient(#216b55 0 ${percent}%, #d8dfd8 ${percent}% 100%)`;
}

function recommendationLabel(verdict, percent) {
  if (percent >= 75) return verdict || "Mieszkanie wygląda na mocno rekomendowane.";
  if (percent >= 55) return verdict || "Mieszkanie wygląda na warte dalszego sprawdzenia.";
  if (percent >= 40) return verdict || "Rekomendacja jest niejednoznaczna.";
  return verdict || "Mieszkanie ma niski poziom rekomendacji.";
}

function addBotMessage(text) {
  state.messages.push({ role: "bot", text });
}

function addUserMessage(text) {
  state.messages.push({ role: "user", text });
}

function appendNotes(text) {
  const cleaned = text.trim();
  if (!cleaned) return;
  state.inputs.notes = state.inputs.notes ? `${state.inputs.notes}\n${cleaned}` : cleaned;
}

function extractUrl(text) {
  const match = text.match(/https?:\/\/[^\s]+/i);
  if (!match) return "";

  try {
    return new URL(match[0]).toString();
  } catch {
    return "";
  }
}

function isSkipIntent(text) {
  return /\b(nie mam|pomiń|pomin|bez rzutu|brak|skip)\b/i.test(text);
}

function formatFloorPlan() {
  if (state.inputs.floorPlan) return state.inputs.floorPlan.name;
  if (state.inputs.linkedFloorPlan) return "Znaleziono w ofercie";
  if (state.inputs.floorPlanSkipped) return "Pominięto";
  return "Jeszcze nie podano";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}
