"use strict";

const MAX_LENGTH = 8000;

const TEST_LEADS = [
  {
    label: "Целевая заявка",
    name: "Анна Смирнова",
    phone: "+7 900 123-45-67",
    email: "a.smirnova@alfa-logistics.ru",
    text:
      "Здравствуйте! Я офис-менеджер компании «Альфа-Логистик». Мы переезжаем в новый офис " +
      "на 40 рабочих мест и хотим заказать поставку и сборку офисной мебели: столы, кресла " +
      "и шкафы. Переезд — в начале следующего месяца, бюджет около 1,5 млн рублей. " +
      "Пришлите, пожалуйста, коммерческое предложение.",
  },
  {
    label: "Мало данных",
    name: "Игорь Петров",
    phone: "+7 912 345-67-89",
    email: "",
    text: "Добрый день, сколько у вас стоят офисные кресла? Нужно несколько штук.",
  },
  {
    label: "Жалоба",
    name: "Марина Ковалёва",
    phone: "",
    email: "m.kovaleva@example.ru",
    text:
      "Это уже третье письмо! Заказ должны были привезти в понедельник, сейчас четверг — " +
      "ни мебели, ни звонка. Менеджер не берёт трубку. Если завтра ничего не изменится, " +
      "будем отказываться и требовать возврат предоплаты.",
  },
];

const form = document.getElementById("lead-form");
const nameInput = document.getElementById("lead-name");
const phoneInput = document.getElementById("lead-phone");
const emailInput = document.getElementById("lead-email");
const textarea = document.getElementById("lead-text");
const submitButton = document.getElementById("submit");
const clearButton = document.getElementById("clear");
const counter = document.getElementById("counter");
const samplesBox = document.getElementById("samples");
const resultPanel = document.getElementById("result");
const emptyState = document.getElementById("empty-state");
const resultBody = document.getElementById("result-body");
const modelLabel = document.getElementById("model-label");
const maskedPanel = document.getElementById("masked-panel");
const maskedText = document.getElementById("masked-text");
const maskedStats = document.getElementById("masked-stats");
const maskedCopy = document.getElementById("copy-masked");

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const MASKED_PLACEHOLDER =
  "Обработайте заявку — здесь появится точный текст, который получит GigaChat.";

const MASKED_LABELS = {
  name: "Имя",
  phone: "Телефон",
  email: "Почта",
  address: "Адрес",
  passport: "Паспорт",
  snils: "СНИЛС",
  inn: "ИНН",
  card: "Карта",
  birth_date: "Дата рождения",
  company: "Компания",
};

let isBusy = false;
let lastMaskedText = "";

/* ---------- вспомогательные ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function updateCounter() {
  const length = textarea.value.length;
  counter.textContent = `${length} / ${MAX_LENGTH}`;
  counter.classList.toggle("is-limit", length > MAX_LENGTH - 400);
}

/* ---------- анонимизированный текст ---------- */

function renderMaskedStats(stats) {
  maskedStats.replaceChildren();
  if (!stats) {
    maskedStats.hidden = true;
    return;
  }

  const entries = Object.entries(stats).filter(([, count]) => Number(count) > 0);
  if (!entries.length) {
    maskedStats.hidden = true;
    return;
  }

  maskedStats.append(el("span", "masked-panel__stats-label", "Замаскировано:"));
  entries.forEach(([key, count]) => {
    const item = el("span", "masked-panel__stat");
    item.append(el("span", null, MASKED_LABELS[key] || key));
    item.append(el("strong", null, "×" + count));
    maskedStats.append(item);
  });
  maskedStats.hidden = false;
}

function renderMasked(text, stats) {
  if (!text) {
    setMaskedPlaceholder("Текст запроса недоступен.");
    return;
  }

  lastMaskedText = text;
  maskedText.textContent = text;
  maskedText.classList.remove("is-placeholder");
  maskedPanel.setAttribute("aria-busy", "false");
  maskedCopy.hidden = false;
  renderMaskedStats(stats);
}

function setMaskedPlaceholder(message = MASKED_PLACEHOLDER) {
  lastMaskedText = "";
  maskedText.textContent = message;
  maskedText.classList.add("is-placeholder");
  maskedPanel.setAttribute("aria-busy", "false");
  maskedCopy.hidden = true;
  maskedStats.hidden = true;
  maskedStats.replaceChildren();
}

function showMaskedLoading() {
  lastMaskedText = "";
  maskedText.textContent = "Маскируем персональные данные и отправляем запрос…";
  maskedText.classList.add("is-placeholder");
  maskedPanel.setAttribute("aria-busy", "true");
  maskedCopy.hidden = true;
  maskedStats.hidden = true;
  maskedStats.replaceChildren();
}

/* ---------- форма ---------- */

function fieldWrap(input) {
  return input.closest(".form-field");
}

function setFieldError(input, message) {
  const wrap = fieldWrap(input);
  if (!wrap) return;
  const error = wrap.querySelector(".field-error");
  wrap.classList.add("has-error");
  input.setAttribute("aria-invalid", "true");
  if (error) {
    error.textContent = message;
    error.hidden = false;
  }
}

function clearFieldError(input) {
  const wrap = fieldWrap(input);
  if (!wrap) return;
  const error = wrap.querySelector(".field-error");
  wrap.classList.remove("has-error");
  input.removeAttribute("aria-invalid");
  if (error) {
    error.textContent = "";
    error.hidden = true;
  }
}

function clearAllErrors() {
  [nameInput, phoneInput, emailInput, textarea].forEach(clearFieldError);
}

function readForm() {
  return {
    name: nameInput.value.trim(),
    phone: phoneInput.value.trim(),
    email: emailInput.value.trim(),
    text: textarea.value.trim(),
  };
}

function validateForm() {
  clearAllErrors();
  const data = readForm();
  let firstInvalid = null;

  if (!data.name) {
    setFieldError(nameInput, "Укажите имя клиента.");
    firstInvalid = firstInvalid || nameInput;
  }

  if (!data.phone && !data.email) {
    setFieldError(phoneInput, "Укажите телефон или почту для связи.");
    setFieldError(emailInput, "Укажите телефон или почту для связи.");
    firstInvalid = firstInvalid || phoneInput;
  } else {
    if (data.phone && data.phone.replace(/\D/g, "").length < 10) {
      setFieldError(phoneInput, "Проверьте номер: нужно не менее 10 цифр.");
      firstInvalid = firstInvalid || phoneInput;
    }
    if (data.email && !EMAIL_RE.test(data.email)) {
      setFieldError(emailInput, "Проверьте адрес почты, например client@company.ru.");
      firstInvalid = firstInvalid || emailInput;
    }
  }

  if (!data.text) {
    setFieldError(textarea, "Впишите текст заявки.");
    firstInvalid = firstInvalid || textarea;
  }

  if (firstInvalid) {
    firstInvalid.focus();
    return null;
  }
  return data;
}

function fillForm(lead) {
  nameInput.value = lead.name || "";
  phoneInput.value = lead.phone || "";
  emailInput.value = lead.email || "";
  textarea.value = lead.text || "";
  clearAllErrors();
  updateCounter();
}

function resetForm() {
  fillForm({ name: "", phone: "", email: "", text: "" });
  clearSamplesHighlight();
  showEmpty();
  setMaskedPlaceholder();
  nameInput.focus();
}

function clearSamplesHighlight() {
  samplesBox.querySelectorAll(".sample-btn").forEach((button) => {
    button.classList.remove("is-active");
  });
}

function showEmpty() {
  resultBody.hidden = true;
  resultBody.replaceChildren();
  emptyState.hidden = false;
  resultPanel.setAttribute("aria-busy", "false");
}

function showLoading() {
  emptyState.hidden = true;
  resultBody.hidden = false;
  resultPanel.setAttribute("aria-busy", "true");

  const box = el("div", "loading");
  box.append(
    el("p", "loading__title", "ИИ анализирует заявку…"),
    el("p", "loading__hint", "Обычно это занимает 5–20 секунд. Не закрывайте страницу.")
  );

  const skeleton = el("div", "skeleton skeleton--head");
  const badge = el("div", "skeleton skeleton--badge");
  const banner = el("div", "skeleton skeleton--banner");
  const block1 = el("div", "skeleton skeleton--block");
  const block2 = el("div", "skeleton skeleton--block");
  const reply = el("div", "skeleton skeleton--reply");
  box.append(skeleton, badge, banner, block1, block2, reply);

  resultBody.replaceChildren(box);
}

function renderError(message, hint) {
  emptyState.hidden = true;
  resultBody.hidden = false;
  resultPanel.setAttribute("aria-busy", "false");

  const box = el("div", "error");
  box.append(el("div", "error__icon", "!"));
  box.append(el("h2", null, "Не получилось обработать заявку"));
  box.append(el("p", null, message || "Неизвестная ошибка."));

  if (hint) {
    box.append(el("div", "error__hint", hint));
  }

  const retry = el("button", "retry", "Попробовать снова");
  retry.type = "button";
  retry.addEventListener("click", submitLead);
  box.append(retry);

  resultBody.replaceChildren(box);
}

/* ---------- отрисовка карточки ---------- */

const BADGES = {
  "целевой": { className: "badge badge--target", text: "Целевой лид" },
  "нецелевой": { className: "badge badge--not", text: "Нецелевой лид" },
  "неясно": { className: "badge badge--unclear", text: "Тип неясен" },
};

const CRM_STATUS = {
  created: { className: "crm crm--ok", title: "Сохранено в Битрикс24" },
  partial: { className: "crm crm--warn", title: "Сохранено в Битрикс24 частично" },
  error: { className: "crm crm--error", title: "Не удалось сохранить в Битрикс24" },
  disabled: { className: "crm crm--muted", title: "Битрикс24 не подключён" },
  unsupported_mode: { className: "crm crm--warn", title: "CRM в неподдерживаемом режиме" },
};

function crmLink(href, text) {
  if (!href) return null;
  const link = el("a", "crm__link", text);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function renderCrm(crm) {
  if (!crm) return null;

  const info = CRM_STATUS[crm.status] || CRM_STATUS.error;
  const box = el("div", info.className);

  const head = el("div", "crm__head");
  head.append(el("span", "crm__dot"));
  head.append(el("span", null, info.title));
  box.append(head);

  if (crm.message) {
    box.append(el("p", "crm__message", crm.message));
  }

  const links = el("div", "crm__links");
  const contactLink = crmLink(crm.contact_url, "Карточка клиента");
  const dealLink = crmLink(crm.deal_url, "Сделка");
  if (contactLink) links.append(contactLink);
  if (dealLink) links.append(dealLink);
  if (links.childElementCount) box.append(links);

  const notes = [];
  if (crm.task_id) notes.push(`Задача №${crm.task_id}`);
  if (crm.notified) notes.push("Уведомление отправлено ответственному.");
  if (notes.length) box.append(el("span", "crm__note", notes.join(" ")));

  if (Array.isArray(crm.warnings) && crm.warnings.length) {
    const warnings = el("ul", "crm__warnings");
    crm.warnings.forEach((warning) => warnings.append(el("li", null, warning)));
    box.append(warnings);
  }

  return box;
}

function field(title, value, options = {}) {
  const wrap = el("div", "field" + (options.wide ? " field--wide" : "") + (options.accent ? " field--accent" : ""));
  wrap.append(el("dt", null, title));
  wrap.append(el("dd", null, value || "не указано"));
  return wrap;
}

function copyToClipboard(text, button) {
  const done = () => {
    const original = button.textContent;
    button.textContent = "Скопировано";
    button.disabled = true;
    window.setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 1500);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.append(helper);
  helper.select();
  try {
    document.execCommand("copy");
    done();
  } catch {
    /* ничего не делаем: пользователь может скопировать вручную */
  } finally {
    helper.remove();
  }
}

function renderCard(result, model, client, crm, anonymizedText, anonymizationStats) {
  emptyState.hidden = true;
  resultBody.hidden = false;
  resultPanel.setAttribute("aria-busy", "false");
  renderMasked(anonymizedText, anonymizationStats);

  const card = el("article", "card");

  // Шапка: компания + тип лида
  const head = el("div", "card__head");
  const headText = el("div");
  headText.append(el("div", "card__eyebrow", "Компания клиента"));
  headText.append(el("h2", "card__company", result.company || "не указано"));

  if (client && (client.name || client.phone || client.email)) {
    const contacts = el("div", "card__contacts");
    if (client.name) contacts.append(el("span", "card__client-name", client.name));
    if (client.phone) {
      const phone = el("a", null, client.phone);
      phone.href = "tel:" + client.phone.replace(/[^\d+]/g, "");
      contacts.append(phone);
    }
    if (client.email) {
      const email = el("a", null, client.email);
      email.href = "mailto:" + client.email;
      contacts.append(email);
    }
    headText.append(contacts);
  }

  const badgeInfo = BADGES[result.lead_type] || BADGES["неясно"];
  head.append(headText, el("span", badgeInfo.className, badgeInfo.text));
  card.append(head);

  // Нужен ли человек
  const human = el("div", "human " + (result.needs_human ? "human--yes" : "human--no"));
  human.append(el("span", "human__dot"));
  const humanText = el("div");
  const humanStrong = el("strong", null, result.needs_human ? "Нужен человек: да." : "Нужен человек: нет.");
  humanText.append(humanStrong);
  if (result.needs_human && result.human_reason) {
    humanText.append(document.createTextNode(" " + result.human_reason));
  } else if (!result.needs_human) {
    humanText.append(document.createTextNode(" Заявку можно обработать по стандартному сценарию."));
  }
  human.append(humanText);
  card.append(human);

  // Поля карточки
  const grid = el("div", "grid");
  grid.append(field("Суть запроса", result.summary, { wide: true }));
  grid.append(field("Известные данные", result.key_data));
  grid.append(field("Чего не хватает", result.missing_data, { accent: true }));
  grid.append(field("Следующий шаг", result.next_step, { wide: true }));
  if (result.needs_human && result.human_reason) {
    grid.append(field("Причина передачи человеку", result.human_reason, { wide: true }));
  }
  card.append(grid);

  // Черновик ответа
  const reply = el("div", "reply");
  const replyHead = el("div", "reply__head");
  replyHead.append(el("span", "reply__title", "Черновик ответа клиенту"));
  const copyButton = el("button", "copy", "Скопировать");
  copyButton.type = "button";
  copyButton.addEventListener("click", () => copyToClipboard(result.reply_draft || "", copyButton));
  replyHead.append(copyButton);
  reply.append(replyHead);
  reply.append(el("p", "reply__text", result.reply_draft || "не указано"));
  card.append(reply);

  // Статус интеграции с Битрикс24
  const crmBlock = renderCrm(crm);
  if (crmBlock) card.append(crmBlock);

  // Служебная строка
  const meta = el("div", "card__meta");
  meta.append(el("span", null, "Модель: " + (model || "GigaChat")));
  meta.append(el("span", null, "Обработано в " + new Date().toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
  })));
  card.append(meta);

  resultBody.replaceChildren(card);
}

/* ---------- отправка ---------- */

function setBusy(value) {
  isBusy = value;
  submitButton.disabled = value;
  submitButton.classList.toggle("is-loading", value);
  submitButton.querySelector(".primary__text").textContent = value
    ? "ИИ анализирует…"
    : "Обработать заявку";
}

async function submitLead() {
  if (isBusy) return;

  const data = validateForm();
  if (!data) return;

  setBusy(true);
  showLoading();
  showMaskedLoading();

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }

    if (!response.ok || !payload || !payload.result) {
      const message = payload && payload.error ? payload.error.message : "Сервер не смог обработать заявку.";
      const hint =
        payload && payload.error && payload.error.hint
          ? payload.error.hint
          : `Сервер ответил кодом ${response.status}. Попробуйте ещё раз.`;
      if (payload && payload.anonymized_text) {
        renderMasked(payload.anonymized_text, payload.anonymization_stats);
      } else {
        setMaskedPlaceholder("Запрос в нейросеть не отправлялся.");
      }
      renderError(message, hint);
      return;
    }

    renderCard(
      payload.result,
      payload.model,
      payload.client,
      payload.crm,
      payload.anonymized_text,
      payload.anonymization_stats
    );
  } catch {
    setMaskedPlaceholder("Нет связи с сервером — текст в нейросеть не отправлялся.");
    renderError(
      "Нет связи с сервером.",
      "Проверьте, что сервер запущен (в терминале должно быть сообщение о запуске), и обновите страницу."
    );
  } finally {
    setBusy(false);
  }
}

/* ---------- события ---------- */

TEST_LEADS.forEach((lead, index) => {
  const button = el("button", "sample-btn", lead.label);
  button.type = "button";
  button.addEventListener("click", () => {
    fillForm(lead);
    clearSamplesHighlight();
    button.classList.add("is-active");
    if (!isBusy) {
      showEmpty();
      setMaskedPlaceholder();
    }
    textarea.focus();
  });
  samplesBox.append(button);
});

[ nameInput, phoneInput, emailInput ].forEach((input) => {
  input.addEventListener("input", () => {
    clearFieldError(input);
    if (!isBusy) {
      clearSamplesHighlight();
      setMaskedPlaceholder();
    }
  });
});

textarea.addEventListener("input", () => {
  updateCounter();
  clearFieldError(textarea);
  if (!isBusy) {
    clearSamplesHighlight();
    setMaskedPlaceholder();
  }
});

maskedCopy.addEventListener("click", () => {
  if (lastMaskedText) {
    copyToClipboard(lastMaskedText, maskedCopy);
  }
});

[ nameInput, phoneInput, emailInput, textarea ].forEach((input) => {
  input.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitLead();
    }
  });
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitLead();
});

clearButton.addEventListener("click", () => {
  if (isBusy) return;
  resetForm();
});

/* ---------- проверка настроек при загрузке ---------- */

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) return;
    const data = await response.json();
    if (data.model) {
      modelLabel.textContent = "Модель: " + data.model;
    }
    if (!data.configured) {
      modelLabel.textContent += " · ключ GigaChat не настроен";
      modelLabel.classList.add("footer__warn");
    }
  } catch {
    /* сервер может быть ещё не готов — не мешаем работе */
  }
}

updateCounter();
setMaskedPlaceholder();
loadHealth();
