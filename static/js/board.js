// Шахматка: перетаскивание пациентов между ячейками и из блоков «Не распределены» /
// «Отменены» в сетку, а в тренажёрах — на другое время того же тренажёра; красным — время,
// где у пациента другие занятия (TZ.md FR-SCH-10a).
// Обработчики висят на document: после каждого действия шахматка приходит заново
// (hx-swap-oob), а слушатели остаются.
(function () {
  // Подключается на всех страницах (base.html) и работает только внутри #board.
  if (window.omrBoard) return;
  window.omrBoard = true;

  // Что тащат: в dragover dataTransfer не читается, поэтому источник хранится здесь.
  let dragged = null;

  // Подсветка: строки сетки, ячейки тренажёров и пункты «Перенести в…», которые по времени
  // пересекаются с другими занятиями пациента (data-busy: [[начало, конец, подпись], …]).
  function minutes(value) {
    const [hours, mins] = value.split(":").map(Number);
    return hours * 60 + mins;
  }

  function span(element) {
    const start = minutes(element.dataset.from);
    const end = element.dataset.to
      ? minutes(element.dataset.to)
      : start + Number(element.dataset.minutes || 0);
    return [start, end];
  }

  function clearBusy() {
    document.querySelectorAll(".board-conflict").forEach(function (element) {
      element.classList.remove("board-conflict");
      if (element.tagName === "OPTION") {
        element.textContent = element.dataset.text;
      } else {
        element.title = element.dataset.title || "";
      }
    });
  }

  function showBusy(json) {
    clearBusy();
    let busy;
    try {
      busy = JSON.parse(json || "[]");
    } catch (error) {
      return;
    }
    if (!busy.length) return;
    const elements = document.querySelectorAll("#board [data-from], #cell-panel option[data-from]");
    elements.forEach(function (element) {
      const [start, end] = span(element);
      const hits = busy.filter(function (item) {
        return item[0] < end && start < item[1];
      });
      if (!hits.length) return;
      const text = "Пациент занят: " + hits.map((item) => item[2]).join(", ");
      element.classList.add("board-conflict");
      if (element.tagName === "OPTION") {
        element.dataset.text = element.textContent;
        element.textContent += " — занят: " + hits.map((item) => item[2]).join(", ");
      } else {
        element.dataset.title = element.title;
        element.title = text;
      }
    });
  }

  // Панель пациента или ячейки с одним пациентом — подсветить сразу; с несколькими — по
  // наведению на пациента. Итог действия (без data-busy) снимает подсветку.
  function panelBusy() {
    const panel = document.getElementById("cell-panel");
    if (!panel) return;
    if (panel.dataset.busy !== undefined) return showBusy(panel.dataset.busy);
    const patients = panel.querySelectorAll("[data-busy]");
    if (patients.length === 1) return showBusy(patients[0].dataset.busy);
    clearBusy();
  }

  document.addEventListener("htmx:afterSettle", panelBusy);

  ["mouseover", "focusin"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      const patient = event.target.closest("#cell-panel [data-busy]");
      if (patient && patient.id !== "cell-panel") showBusy(patient.dataset.busy);
    });
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest("#cell-panel .btn-close")) clearBusy();
  });

  function dropTarget(event) {
    if (!dragged) return null;
    const selector = dragged.equipment ? "[data-equipment-drop]" : "[data-drop]";
    const seat = event.target.closest(selector);
    if (!seat || !seat.closest("#board")) return null;
    // Тренажёр задан процедурой: переносится только по времени, в своей колонке.
    if (dragged.equipment && seat.dataset.equipmentDrop !== dragged.equipment) return null;
    return seat;
  }

  document.addEventListener("dragstart", function (event) {
    const chip = event.target.closest("[data-drag]");
    if (!chip) return;
    dragged = {
      booking: chip.dataset.booking || "",
      version: chip.dataset.version || "",
      program: chip.dataset.program || "",
      equipment: chip.dataset.equipment || "",
    };
    showBusy(chip.dataset.busy);
    // Пока пациента тащат, обновление шахматки от других пользователей ждёт (live.js).
    document.body.dataset.dragging = "1";
    event.dataTransfer.effectAllowed = "move";
    // Без данных Firefox не начинает перетаскивание.
    event.dataTransfer.setData("text/plain", chip.textContent.trim());
  });

  document.addEventListener("dragend", function () {
    dragged = null;
    delete document.body.dataset.dragging;
    panelBusy();
    document.querySelectorAll(".board-drop").forEach(function (seat) {
      seat.classList.remove("board-drop");
    });
  });

  document.addEventListener("dragover", function (event) {
    const seat = dropTarget(event);
    if (!seat) return;
    event.preventDefault();
    seat.classList.add("board-drop");
  });

  document.addEventListener("dragleave", function (event) {
    const seat = dropTarget(event);
    if (seat && !seat.contains(event.relatedTarget)) seat.classList.remove("board-drop");
  });

  document.addEventListener("drop", function (event) {
    const seat = dropTarget(event);
    if (!seat) return;
    event.preventDefault();
    seat.classList.remove("board-drop");
    const data = dragged;
    dragged = null;
    const board = document.getElementById("board");
    let url;
    let values;
    if (data.equipment) {
      url = board.dataset.equipmentMoveUrl.replace("/0/", "/" + data.booking + "/");
      values = { start: seat.dataset.start, version: data.version };
    } else {
      values = {
        date: board.dataset.date,
        target: seat.dataset.instructor + ":" + seat.dataset.slot,
      };
      if (data.booking) {
        url = board.dataset.moveUrl.replace("/0/", "/" + data.booking + "/");
        values.version = data.version;
      } else if (data.program) {
        url = board.dataset.placeUrl;
        values.program = data.program;
      } else {
        return;
      }
    }
    // source — ячейка: от неё наследуется заголовок CSRF (hx-headers на body).
    htmx.ajax("POST", url, { source: seat, target: "#cell-panel", swap: "outerHTML", values: values });
  });
})();
