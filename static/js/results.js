import { escapeHtml, formatMoney } from "./components.js";

const fieldLabels = {
  purchase_type: "Cel zakupu",
  household_type: "Kto będzie mieszkać",
  budget_min_pln: "Budżet od",
  budget_max_pln: "Budżet do",
  preferred_city: "Miasto",
  preferred_districts: "Dzielnice lub obszary",
  location_mode: "Tryb lokalizacji",
  rooms_min: "Liczba pokoi od",
  area_min_m2: "Metraż od",
  area_max_m2: "Metraż do",
  priority_primary: "Najważniejszy priorytet",
  lifestyle_modes: "Styl życia",
  must_haves: "Warunki konieczne",
  future_horizon: "Horyzont zakupu",
  work_from_home: "Praca z domu",
  needs_extra_room: "Dodatkowy pokój lub strefa",
  has_car: "Miejsce dla auta",
  wants_school_nearby: "Szkoła lub przedszkole blisko",
  investment_focus: "Cel inwestycji",
};

const locationModes = {
  districts: "Wybrane dzielnice",
  commute: "Dobry dojazd",
  map_area: "Zaznaczony obszar",
};

const rankingLabels = {
  priority_primary: "Dopasowanie do głównego priorytetu",
  lifestyle_modes: "Styl życia i otoczenie",
  must_haves: "Warunki konieczne",
  future_horizon: "Horyzont użytkowania",
  conditional_signals: "Sygnały kontekstowe",
};

export function renderProfilePreview(state) {
  const profile = state.buyerProfile;
  const rows = [
    ["purchase_type", labelValue(state.spec, "purchase_type", profile.purchase_type)],
    ["budget_max_pln", formatBudget(profile)],
    ["preferred_city", formatLocation(profile)],
    ["rooms_min", profile.rooms_min ? `${profile.rooms_min}+` : ""],
    ["area_min_m2", formatArea(profile)],
    ["priority_primary", labelValue(state.spec, "priority_primary", profile.priority_primary)],
    ["must_haves", labelList(state.spec, "must_haves", profile.must_haves)],
  ].filter(([, value]) => hasDisplayValue(value));

  if (!rows.length) {
    return `<p class="muted">Odpowiedzi pojawią się tutaj po wybraniu pierwszej karty.</p>`;
  }

  return `
    <dl class="profile-list">
      ${rows
        .map(
          ([key, value]) => `
            <div>
              <dt>${escapeHtml(fieldLabels[key] || key)}</dt>
              <dd>${escapeHtml(value)}</dd>
            </div>
          `,
        )
        .join("")}
    </dl>
  `;
}

export function buildOutcome(state) {
  const profile = state.buyerProfile;
  return {
    summaryText: buildSummaryText(state),
    searchFilters: buildSearchFilters(state),
    rankingProfile: buildRankingProfile(state),
    followUpQuestions: buildFollowUpQuestions(profile),
  };
}

export function renderOutcome(state) {
  const outcome = buildOutcome(state);

  return `
    <section class="result-section">
      <h3>Podsumowanie potrzeb</h3>
      <p>${escapeHtml(outcome.summaryText)}</p>
    </section>

    <section class="result-section">
      <h3>Gotowe filtry wyszukiwania</h3>
      <div class="filter-grid">
        ${outcome.searchFilters
          .map(
            (filter) => `
              <div class="filter-item">
                <span>${escapeHtml(filter.label)}</span>
                <strong>${escapeHtml(filter.value)}</strong>
              </div>
            `,
          )
          .join("")}
      </div>
    </section>

    <section class="result-section">
      <h3>Ranking profilu</h3>
      <div class="ranking-list">
        ${outcome.rankingProfile
          .map(
            (item) => `
              <div class="ranking-row">
                <div>
                  <strong>${escapeHtml(item.label)}</strong>
                  <span>${escapeHtml(item.detail)}</span>
                </div>
                <b>${Math.round(item.weight * 100)}%</b>
              </div>
            `,
          )
          .join("")}
      </div>
    </section>

    ${
      outcome.followUpQuestions.length
        ? `
          <section class="result-section">
            <h3>Pytania doprecyzowujące</h3>
            <ol class="follow-up-list">
              ${outcome.followUpQuestions.map((question) => `<li>${escapeHtml(question)}</li>`).join("")}
            </ol>
          </section>
        `
        : ""
    }
  `;
}

function buildSummaryText(state) {
  const profile = state.buyerProfile;
  const purchase = labelValue(state.spec, "purchase_type", profile.purchase_type) || "mieszkanie";
  const location = formatLocation(profile) || "wybranej lokalizacji";
  const rooms = profile.rooms_min ? `${profile.rooms_min}+ pok.` : "ustalonej liczbie pokoi";
  const area = formatArea(profile) || "dopasowanym metrażu";
  const budget = formatBudget(profile) || "ustalonym budżecie";
  const priority = labelValue(state.spec, "priority_primary", profile.priority_primary) || "najważniejszym priorytecie";

  return `Szukasz mieszkania: ${purchase.toLowerCase()}, w lokalizacji ${location}, z parametrami ${rooms} i ${area}. Filtry powinny trzymać się zakresu ${budget}, a ranking ofert ma najmocniej premiować: ${priority.toLowerCase()}.`;
}

function buildSearchFilters(state) {
  const profile = state.buyerProfile;
  const filters = [
    { label: "Miasto", value: profile.preferred_city },
    { label: "Lokalizacja", value: formatLocation(profile) },
    { label: "Budżet", value: formatBudget(profile) },
    { label: "Pokoje", value: profile.rooms_min ? `minimum ${profile.rooms_min}` : "" },
    { label: "Metraż", value: formatArea(profile) },
    { label: "Warunki konieczne", value: labelList(state.spec, "must_haves", profile.must_haves) },
  ];

  if (profile.has_car === true) filters.push({ label: "Parking", value: "wymagany" });
  if (profile.wants_school_nearby === true) filters.push({ label: "Edukacja", value: "szkoła lub przedszkole w pobliżu" });
  if (profile.needs_extra_room === true) filters.push({ label: "Układ", value: "dodatkowy pokój lub wydzielona strefa" });

  return filters.filter((filter) => hasDisplayValue(filter.value));
}

function buildRankingProfile(state) {
  const profile = state.buyerProfile;
  const weights = state.spec.scoring_rules?.soft_weights || {};
  const conditionals = [
    profile.work_from_home ? `praca z domu: ${labelValue(state.spec, "work_from_home", profile.work_from_home)}` : "",
    profile.has_car === true ? "potrzebne miejsce dla auta" : "",
    profile.wants_school_nearby === true ? "ważna edukacja blisko domu" : "",
    profile.investment_focus ? `inwestycja: ${labelValue(state.spec, "investment_focus", profile.investment_focus)}` : "",
  ].filter(Boolean);

  return Object.entries(weights).map(([key, weight]) => ({
    key,
    weight,
    label: rankingLabels[key] || key,
    detail: rankingDetail(state, key, conditionals),
  }));
}

function rankingDetail(state, key, conditionals) {
  const profile = state.buyerProfile;

  if (key === "priority_primary") {
    return labelValue(state.spec, "priority_primary", profile.priority_primary) || "do uzupełnienia";
  }
  if (key === "lifestyle_modes") {
    return labelList(state.spec, "lifestyle", profile.lifestyle_modes) || "brak dodatkowych preferencji";
  }
  if (key === "must_haves") {
    return labelList(state.spec, "must_haves", profile.must_haves) || "brak twardych wymagań poza filtrami";
  }
  if (key === "future_horizon") {
    return labelValue(state.spec, "future_horizon", profile.future_horizon) || "nieokreślony";
  }
  if (key === "conditional_signals") {
    return conditionals.join(", ") || "brak dodatkowych sygnałów";
  }

  return "";
}

function buildFollowUpQuestions(profile) {
  const questions = [];

  if (profile.purchase_type === "undecided") {
    questions.push("Czy decyzja jest bliżej zakupu dla siebie, czy inwestycji pod wynajem?");
  }
  if (!profile.future_horizon || profile.future_horizon === "undecided") {
    questions.push("Czy mieszkanie ma działać na najbliższe lata, czy ma być docelowe?");
  }
  if (!profile.lifestyle_modes?.length) {
    questions.push("Które otoczenie jest ważniejsze: centrum, zieleń, cisza czy szybki transport?");
  }
  if (!profile.must_haves?.length) {
    questions.push("Czy balkon, parking, winda albo komórka lokatorska są warunkiem koniecznym?");
  }
  if (profile.location_mode === "commute" && !profile.preferred_districts?.length) {
    questions.push("Do jakiego punktu miasta ma być liczony dobry dojazd?");
  }

  return questions.slice(0, 3);
}

function labelValue(spec, cardId, value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "boolean") return value ? "Tak" : "Nie";
  if (cardId === "location_mode") return locationModes[value] || value;

  const card = findCard(spec, cardId);
  const option = card?.options?.find((item) => item.id === String(value));
  return option?.label || String(value);
}

function labelList(spec, cardId, values = []) {
  if (!Array.isArray(values) || !values.length) return "";
  return values.map((value) => labelValue(spec, cardId, value)).join(", ");
}

function findCard(spec, cardId) {
  return [...(spec.cards || []), ...(spec.conditional_cards || [])].find((card) => card.id === cardId);
}

function formatBudget(profile) {
  const min = profile.budget_min_pln;
  const max = profile.budget_max_pln;
  if (min != null && max != null) return `${formatMoney(min)}-${formatMoney(max)} zł`;
  if (min != null) return `od ${formatMoney(min)} zł`;
  if (max != null) return `do ${formatMoney(max)} zł`;
  return "";
}

function formatArea(profile) {
  const min = profile.area_min_m2;
  const max = profile.area_max_m2;
  if (min != null && max != null) return `${min}-${max} m²`;
  if (min != null) return `od ${min} m²`;
  if (max != null) return `do ${max} m²`;
  return "";
}

function formatLocation(profile) {
  const city = profile.preferred_city;
  const districts = profile.preferred_districts || [];
  const mode = locationModes[profile.location_mode] || "";
  const area = districts.length ? districts.join(", ") : mode;

  if (city && area) return `${city}: ${area}`;
  return city || area || "";
}

function hasDisplayValue(value) {
  if (Array.isArray(value)) return value.length > 0;
  return value !== null && value !== undefined && value !== "";
}
