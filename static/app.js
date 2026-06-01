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
    floorPlanSkipped: false,
  },
  analyzing: false,
  ready: false,
  resultsMode: false,
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
  elements.analyzeButton.addEventListener("click", analyzeFlat);
  elements.resetButton.addEventListener("click", resetConversation);
}

function handleChatSubmit(event) {
  event.preventDefault();
  const text = elements.chatInput.value.trim();
  if (!text) return;

  addUserMessage(text);
  elements.chatInput.value = "";

  if (state.step === "listing_url") {
    handleListingAnswer(text);
  } else if (state.step === "floor_plan") {
    handleFloorPlanTextAnswer(text);
  } else if (state.step === "priorities") {
    handlePrioritiesAnswer(text);
  } else if (state.ready) {
    addBotMessage("Mam już komplet danych. Możesz uruchomić analizę przyciskiem po prawej stronie.");
  }

  render();
}

function handleListingAnswer(text) {
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
  state.inputs.floorPlanSkipped = false;
  addUserMessage(`Dodałem rzut: ${file.name}`);
  finishFloorPlanStep();
  render();
}

function skipFloorPlan() {
  state.inputs.floorPlan = null;
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
    const response = await fetch("/api/analyze", {
      method: "POST",
      body: formData,
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.detail || "Nie udało się wykonać analizy.");
    }

    addBotMessage("Analiza gotowa. Raport pojawił się po prawej stronie.");
    renderReport(payload.report, payload.visualization, payload.geo_context, payload.floor_plan_analysis);
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
    floor_plan: state.inputs.notes.length >= 20 ? "Pytanie 2/2" : "Pytanie 2/3",
    priorities: "Pytanie 3/3",
    ready: "Gotowe",
  };

  elements.stepPill.textContent = state.analyzing ? "Analiza" : labels[state.step] || "Start";
  elements.fileQuestion.classList.toggle("hidden", state.step !== "floor_plan");
  elements.chatInput.disabled = state.analyzing;
  elements.sendButton.disabled = state.analyzing;
  elements.analyzeButton.disabled = !state.ready || state.analyzing;
  elements.analyzeButton.textContent = state.analyzing ? "Analizuję..." : "Analizuj mieszkanie";

  if (state.step === "floor_plan") {
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

  if (state.analyzing) {
    elements.statusText.textContent = "Pobieram ofertę i przygotowuję raport. To może potrwać kilkanaście sekund.";
  } else if (state.ready) {
    elements.statusText.textContent = "Dane są gotowe. Możesz uruchomić analizę.";
  } else {
    elements.statusText.textContent = "Odpowiedz na pytania w czacie, żeby uruchomić analizę.";
  }
}

function renderReport(report, visualization = {}, geoContext = {}, floorPlanAnalysis = {}) {
  elements.reportRoot.innerHTML = `
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
    ${renderPriceMetrics(report.price_metrics_assessment)}
    ${renderListSection("Fakty z oferty", report.facts_found)}
    ${renderListSection("Ryzyka i deal-breakery", report.deal_breakers)}
    ${renderListSection("Pytania do dewelopera", report.questions_for_developer)}
    ${renderListSection("Następne kroki due diligence", report.due_diligence_next_steps)}
    ${renderListSection("Co zmieniłoby rekomendację", report.what_would_change_my_mind)}
    ${renderListSection("Notatki o źródłach", report.source_notes)}
    ${renderInfographics(report, geoContext)}
    ${report.raw_response ? `<pre>${escapeHtml(report.raw_response)}</pre>` : ""}
  `;
}

function renderFloorPlanAnalysis(data = {}) {
  if (!data || data.status !== "ready") return "";

  const wallSummary = data.wall_run_summary || {};
  const rooms = data.room_candidates || [];
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

      ${renderGeoAssessment(assessment)}
      ${renderGeoSections("Planowanie", context.planning_context)}
      ${renderGeoSections("Otoczenie fizyczne", context.physical_context)}
      ${renderGeoSections("Infrastruktura", context.infrastructure_context)}
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
