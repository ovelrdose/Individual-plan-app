// Назначения на странице программы: порядок строк в карте — перетаскиванием за ручку,
// закрытие панели правки — крестиком, «Отмена» или Esc.
// Обработчики висят на document: таблица приходит заново после каждого действия (HTMX).
(function () {
  // Подключается на всех страницах (base.html) и работает только внутри #prescriptions.
  if (window.omrPrescriptions) return;
  window.omrPrescriptions = true;

  let dragged = null;
  let before = "";

  function order(tbody) {
    return Array.from(tbody.querySelectorAll("input[name=order]"))
      .map((input) => input.value)
      .join(",");
  }

  // Строка становится перетаскиваемой только за ручку: иначе клик по строке и выделение
  // текста ломались бы.
  document.addEventListener("mousedown", function (event) {
    const handle = event.target.closest("#prescriptions [data-drag-handle]");
    if (handle) handle.closest("tr").draggable = true;
  });

  document.addEventListener("dragstart", function (event) {
    const row = event.target.closest && event.target.closest("#prescriptions tbody[data-sortable] > tr");
    if (!row) return;
    dragged = row;
    before = order(row.parentElement);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", "");
    row.classList.add("dragging");
    // live.js не обновляет страницу, пока строку тащат.
    document.body.dataset.dragging = "1";
  });

  document.addEventListener("dragover", function (event) {
    if (!dragged) return;
    const row = event.target.closest("tr");
    if (!row || row === dragged || row.parentElement !== dragged.parentElement) return;
    event.preventDefault();
    const box = row.getBoundingClientRect();
    const after = event.clientY > box.top + box.height / 2;
    row.parentElement.insertBefore(dragged, after ? row.nextSibling : row);
  });

  document.addEventListener("drop", function (event) {
    if (dragged) event.preventDefault();
  });

  document.addEventListener("dragend", function () {
    if (!dragged) return;
    const row = dragged;
    dragged = null;
    row.draggable = false;
    row.classList.remove("dragging");
    delete document.body.dataset.dragging;
    if (order(row.parentElement) === before) return;
    // Номера строк сразу, не дожидаясь ответа сервера.
    row.parentElement.querySelectorAll("tr").forEach(function (item, index) {
      const cell = item.children[1];
      if (cell) cell.textContent = index + 1;
    });
    htmx.trigger("#prescription-order", "reorder");
  });

  function closePanel() {
    const panel = document.getElementById("prescription-panel");
    if (!panel || panel.classList.contains("d-none")) return;
    panel.replaceChildren();
    panel.className = "d-none";
    document
      .querySelectorAll("#prescriptions tr.table-active")
      .forEach((row) => row.classList.remove("table-active"));
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("#prescription-panel [data-panel-close]")) closePanel();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && document.getElementById("prescription-panel")) closePanel();
  });

  // «Групповые занятия»: панель группы или тренажёра приходит в модальное окно #edit-modal.
  // Пришла панель — окно открывается; панель закрыли (крестик, «Отмена», пустой ответ) —
  // закрывается. Esc и клик мимо окна закрывают его средствами Bootstrap.
  function editModal() {
    const element = document.getElementById("edit-modal");
    return element && window.bootstrap ? bootstrap.Modal.getOrCreateInstance(element) : null;
  }

  function closeSidePanels() {
    document.querySelectorAll(".side-panel").forEach(function (panel) {
      panel.replaceChildren();
      panel.className = "d-none";
    });
    document
      .querySelectorAll("[data-panel-table] .table-active")
      .forEach((row) => row.classList.remove("table-active"));
  }

  document.addEventListener("htmx:afterSettle", function () {
    const modal = editModal();
    if (!modal) return;
    if (document.querySelector("#edit-modal .side-panel")) modal.show();
    else modal.hide();
  });

  document.addEventListener("hidden.bs.modal", function (event) {
    if (event.target.id === "edit-modal") closeSidePanels();
  });

  document.addEventListener("click", function (event) {
    if (!event.target.closest(".side-panel [data-panel-close]")) return;
    const modal = editModal();
    if (modal) modal.hide();
    else closeSidePanels();
  });
})();
