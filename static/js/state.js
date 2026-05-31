export function createState(spec) {
  return {
    spec,
    answers: {},
    buyerProfile: createInitialProfile(spec),
    currentIndex: 0,
    errors: {},
    completed: false,
  };
}

export function createInitialProfile(spec) {
  const model = spec.data_model?.buyer_profile || {};
  return Object.fromEntries(
    Object.entries(model).map(([key, type]) => {
      if (String(type).includes("[]")) return [key, []];
      return [key, null];
    }),
  );
}

export function getAllCards(spec) {
  return [...(spec.cards || []), ...(spec.conditional_cards || [])];
}

export function getVisibleCards(state) {
  const baseCards = state.spec.cards || [];
  const conditionalCards = (state.spec.conditional_cards || []).filter((card) =>
    evaluateCondition(card.show_if, state.buyerProfile),
  );
  return [...baseCards, ...conditionalCards];
}

export function saveAnswer(state, card, value) {
  state.answers[card.id] = value;
  delete state.errors[card.id];
  rebuildProfile(state);
  pruneHiddenAnswers(state);
  clampIndex(state);
}

export function skipAnswer(state, card) {
  delete state.answers[card.id];
  delete state.errors[card.id];
  rebuildProfile(state);
  pruneHiddenAnswers(state);
  clampIndex(state);
}

export function rebuildProfile(state) {
  const profile = createInitialProfile(state.spec);
  const cardsById = new Map(getAllCards(state.spec).map((card) => [card.id, card]));

  for (const [cardId, answer] of Object.entries(state.answers)) {
    const card = cardsById.get(cardId);
    if (!card) continue;
    applyAnswerToProfile(profile, state.spec, card, answer);
  }

  state.buyerProfile = profile;
}

function pruneHiddenAnswers(state) {
  let changed = false;
  const visibleIds = new Set(getVisibleCards(state).map((card) => card.id));

  for (const card of state.spec.conditional_cards || []) {
    if (!visibleIds.has(card.id) && Object.hasOwn(state.answers, card.id)) {
      delete state.answers[card.id];
      changed = true;
    }
  }

  if (changed) rebuildProfile(state);
}

function clampIndex(state) {
  const visibleCards = getVisibleCards(state);
  state.currentIndex = Math.min(state.currentIndex, Math.max(visibleCards.length - 1, 0));
}

function applyAnswerToProfile(profile, spec, card, answer) {
  const target = card.maps_to;
  if (!target) return;

  if (Array.isArray(target)) {
    applyMultiTarget(profile, target, card, answer);
    return;
  }

  setPath(profile, target, normalizeValueForField(spec, target, getScalarAnswer(card, answer)));
}

function applyMultiTarget(profile, targets, card, answer) {
  if (card.type === "range_with_presets") {
    setPath(profile, targets[0], numberOrNull(answer?.min));
    setPath(profile, targets[1], numberOrNull(answer?.max));
    return;
  }

  if (card.type === "location_select") {
    setPath(profile, targets[0], cleanText(answer?.city));
    setPath(profile, targets[1], normalizeStringArray(answer?.districts));
    setPath(profile, targets[2], answer?.mode || null);
    return;
  }

  const option = findOption(card, answer);
  if (option && ("min" in option || "max" in option)) {
    setPath(profile, targets[0], numberOrNull(option.min));
    setPath(profile, targets[1], numberOrNull(option.max));
    return;
  }

  targets.forEach((target, index) => {
    setPath(profile, target, Array.isArray(answer) ? answer[index] : answer);
  });
}

function getScalarAnswer(card, answer) {
  if (card.type === "multi_select") return Array.isArray(answer) ? answer : [];
  if (card.type === "single_select") return answer ?? null;
  return answer ?? null;
}

function normalizeValueForField(spec, target, value) {
  const fieldName = target.split(".").at(-1);
  const modelType = spec.data_model?.buyer_profile?.[fieldName] || "";

  if (String(modelType).includes("boolean")) {
    if (value === "yes") return true;
    if (value === "no") return false;
  }

  if (String(modelType).includes("integer")) return numberOrNull(value);
  if (String(modelType).includes("number")) return numberOrNull(value);
  if (String(modelType).includes("[]")) return normalizeStringArray(value);

  return value ?? null;
}

function findOption(card, optionId) {
  return (card.options || []).find((option) => option.id === optionId);
}

export function evaluateCondition(condition, profile) {
  if (!condition) return true;

  if (condition.any) {
    return condition.any.some((child) => evaluateCondition(child, profile));
  }

  if (condition.all) {
    return condition.all.every((child) => evaluateCondition(child, profile));
  }

  const value = getPath(profile, condition.field);
  if ("equals" in condition) return value === condition.equals;
  if ("in" in condition) return condition.in.includes(value);
  if ("not_equals" in condition) return value !== condition.not_equals;

  return Boolean(value);
}

export function getPath(source, path) {
  let parts = String(path).split(".");
  if (parts[0] === "buyer_profile" && !source?.buyer_profile) {
    parts = parts.slice(1);
  }

  return parts.reduce((cursor, key) => (cursor == null ? undefined : cursor[key]), source);
}

function setPath(target, path, value) {
  const parts = String(path).split(".");
  const usableParts = parts[0] === "buyer_profile" ? parts.slice(1) : parts;
  let cursor = target;

  for (const part of usableParts.slice(0, -1)) {
    cursor[part] = cursor[part] || {};
    cursor = cursor[part];
  }

  cursor[usableParts.at(-1)] = value;
}

function cleanText(value) {
  const text = String(value || "").trim();
  return text || null;
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeStringArray(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }

  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
