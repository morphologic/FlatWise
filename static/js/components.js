export function renderQuestionCard(card, value, error) {
  const requiredText = card.required ? "Pytanie wymagane" : "Możesz pominąć";

  return `
    <article class="question-card" data-card-id="${escapeHtml(card.id)}">
      <div class="question-copy">
        <p class="question-meta">${requiredText}</p>
        <h2 id="card-title">${escapeHtml(card.title || "Pytanie")}</h2>
        ${card.subtitle ? `<p class="subtitle">${escapeHtml(card.subtitle)}</p>` : ""}
      </div>

      ${renderControl(card, value)}

      <p class="inline-error ${error ? "" : "hidden"}" role="alert">${escapeHtml(error || "")}</p>
    </article>
  `;
}

export function readCardValue(card, root) {
  if (card.type === "single_select") {
    return root.querySelector(`input[name="${cssEscape(card.id)}"]:checked`)?.value ?? null;
  }

  if (card.type === "multi_select") {
    return [...root.querySelectorAll(`input[name="${cssEscape(card.id)}"]:checked`)].map(
      (input) => input.value,
    );
  }

  if (card.type === "range_with_presets") {
    return {
      min: readNumberInput(root, "range-min"),
      max: readNumberInput(root, "range-max"),
      label: root.querySelector("[data-range-label]")?.value || null,
      source: "custom",
    };
  }

  if (card.type === "location_select") {
    return {
      city: root.querySelector("[data-location-city]")?.value.trim() || "",
      mode: root.querySelector(`input[name="${cssEscape(card.id)}-mode"]:checked`)?.value || "",
      districts: splitList(root.querySelector("[data-location-districts]")?.value || ""),
    };
  }

  return null;
}

export function validateAnswer(card, value) {
  if (!card.required && isEmptyAnswer(card, value)) {
    return { valid: true, message: "" };
  }

  if (card.type === "single_select") {
    return value ? ok() : fail("Wybierz jedną odpowiedź, żeby przejść dalej.");
  }

  if (card.type === "multi_select") {
    if (!card.required) return ok();
    return Array.isArray(value) && value.length ? ok() : fail("Zaznacz co najmniej jedną odpowiedź.");
  }

  if (card.type === "range_with_presets") {
    const hasMin = value?.min !== null && value?.min !== undefined;
    const hasMax = value?.max !== null && value?.max !== undefined;
    if (!hasMin && !hasMax) return fail("Wybierz zakres albo wpisz własny budżet.");
    if (hasMin && hasMax && Number(value.min) > Number(value.max)) {
      return fail("Minimalna kwota nie może być większa niż maksymalna.");
    }
    return ok();
  }

  if (card.type === "location_select") {
    if (!value?.city) return fail("Wpisz miasto, w którym szukasz mieszkania.");
    if (!value?.mode) return fail("Wybierz sposób określenia lokalizacji.");
    if (value.mode !== "commute" && !value.districts?.length) {
      return fail("Dopisz co najmniej jedną dzielnicę lub obszar.");
    }
    return ok();
  }

  return ok();
}

export function isEmptyAnswer(card, value) {
  if (value === null || value === undefined) return true;
  if (card.type === "multi_select") return !Array.isArray(value) || value.length === 0;
  if (card.type === "range_with_presets") return value.min == null && value.max == null;
  if (card.type === "location_select") {
    return !value.city && !value.mode && (!value.districts || value.districts.length === 0);
  }
  return value === "";
}

export function getPresetAnswer(card, presetIndex) {
  const preset = card.presets?.[presetIndex];
  if (!preset) return null;
  return {
    min: preset.min,
    max: preset.max,
    label: preset.label,
    source: "preset",
  };
}

export function formatMoney(value) {
  if (value === null || value === undefined) return "";
  return new Intl.NumberFormat("pl-PL").format(value);
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function renderControl(card, value) {
  if (card.type === "single_select") return renderSingleSelect(card, value);
  if (card.type === "multi_select") return renderMultiSelect(card, value);
  if (card.type === "range_with_presets") return renderRangeWithPresets(card, value);
  if (card.type === "location_select") return renderLocationSelect(card, value);

  return `<div class="empty-control">Ten typ pytania nie ma jeszcze widoku.</div>`;
}

function renderSingleSelect(card, value) {
  return `
    <div class="option-grid" role="radiogroup" aria-label="${escapeHtml(card.title)}">
      ${(card.options || []).map((option) => renderOption(card, option, value === option.id, "radio")).join("")}
    </div>
  `;
}

function renderMultiSelect(card, value = []) {
  const selected = Array.isArray(value) ? value : [];
  const helper = card.max_selected ? `<p class="field-hint">Maksymalnie ${card.max_selected} odpowiedzi.</p>` : "";

  return `
    ${helper}
    <div class="option-grid" role="group" aria-label="${escapeHtml(card.title)}">
      ${(card.options || [])
        .map((option) => renderOption(card, option, selected.includes(option.id), "checkbox"))
        .join("")}
    </div>
  `;
}

function renderOption(card, option, checked, inputType) {
  return `
    <label class="option-tile ${checked ? "selected" : ""}">
      <input
        type="${inputType}"
        name="${escapeHtml(card.id)}"
        value="${escapeHtml(option.id)}"
        ${checked ? "checked" : ""}
      />
      <span>${escapeHtml(option.label)}</span>
    </label>
  `;
}

function renderRangeWithPresets(card, value = {}) {
  const min = value?.min ?? "";
  const max = value?.max ?? "";
  const selectedLabel = value?.label ?? "";

  return `
    <div class="preset-grid" role="group" aria-label="${escapeHtml(card.title)}">
      ${(card.presets || [])
        .map(
          (preset, index) => `
            <button
              class="preset-button ${selectedLabel === preset.label ? "selected" : ""}"
              type="button"
              data-preset-index="${index}"
            >
              ${escapeHtml(preset.label)}
            </button>
          `,
        )
        .join("")}
    </div>

    ${card.allow_custom_input ? `
      <div class="range-inputs">
        <input type="hidden" data-range-label value="${escapeHtml(selectedLabel)}" />
        <label class="field">
          <span>Od</span>
          <input data-range-min type="number" min="0" step="10000" value="${escapeHtml(min)}" placeholder="np. 500000" />
        </label>
        <label class="field">
          <span>Do</span>
          <input data-range-max type="number" min="0" step="10000" value="${escapeHtml(max)}" placeholder="np. 900000" />
        </label>
      </div>
    ` : ""}
  `;
}

function renderLocationSelect(card, value = {}) {
  const modes = card.modes || [];
  const selectedMode = value?.mode || modes[0]?.id || "";
  const areaLabel = selectedMode === "map_area" ? "Obszar" : "Dzielnice";
  const areaPlaceholder =
    selectedMode === "map_area" ? "np. okolice metra, wybrany rejon miasta" : "np. Mokotów, Wola, Żoliborz";

  return `
    <div class="location-stack">
      <label class="field">
        <span>Miasto</span>
        <input data-location-city type="text" value="${escapeHtml(value?.city || "")}" placeholder="np. Warszawa" autocomplete="address-level2" />
      </label>

      <div class="segment-group" role="radiogroup" aria-label="Sposób wyboru lokalizacji">
        ${modes
          .map(
            (mode) => `
              <label class="segment ${selectedMode === mode.id ? "selected" : ""}">
                <input
                  type="radio"
                  name="${escapeHtml(card.id)}-mode"
                  value="${escapeHtml(mode.id)}"
                  ${selectedMode === mode.id ? "checked" : ""}
                />
                <span>${escapeHtml(mode.label)}</span>
              </label>
            `,
          )
          .join("")}
      </div>

      <label class="field ${selectedMode === "commute" ? "soft-hidden" : ""}">
        <span>${areaLabel}</span>
        <textarea data-location-districts rows="3" placeholder="${escapeHtml(areaPlaceholder)}">${escapeHtml((value?.districts || []).join(", "))}</textarea>
      </label>
    </div>
  `;
}

function readNumberInput(root, selector) {
  const value = root.querySelector(`[data-${selector}]`)?.value;
  if (value === "" || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function splitList(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function ok() {
  return { valid: true, message: "" };
}

function fail(message) {
  return { valid: false, message };
}

function cssEscape(value) {
  return window.CSS?.escape ? CSS.escape(value) : String(value).replace(/"/g, '\\"');
}
