// Обновление страниц в реальном времени (TZ.md NFR-11). Контейнер с data-live="темы"
// перезапрашивается с текущего адреса, когда другой пользователь что-то изменил: поток
// /events/ (Server-Sent Events) присылает темы изменений. Прокрутка внутри [data-keep-scroll]
// сохраняется; пока пользователь тащит пациента или вводит текст — обновление ждёт.
(function () {
  if (window.omrLive) return;
  window.omrLive = true;

  // Номер вкладки: свои правки вкладка уже показала — их событие пропускаем.
  const client = Math.random().toString(36).slice(2) + Date.now().toString(36);
  let source = null;
  let subscribed = "";
  let timer = null;
  const pending = new Set();

  function cookie(name) {
    const item = document.cookie.split("; ").find((row) => row.startsWith(name + "="));
    return item ? decodeURIComponent(item.split("=")[1]) : "";
  }

  // CSRF — из cookie, а не из атрибута body: после входа через hx-boost токен новый.
  document.addEventListener("htmx:configRequest", function (event) {
    event.detail.headers["X-Live-Client"] = client;
    const token = cookie("csrftoken");
    if (token) event.detail.headers["X-CSRFToken"] = token;
  });

  function containers() {
    return Array.from(document.querySelectorAll("[data-live][id]"));
  }

  function topicsOf(element) {
    return element.dataset.live.split(/\s+/).filter(Boolean);
  }

  // То же правило, что apps/live/topics.matches.
  function matches(wanted, topics) {
    return topics.some(function (topic) {
      if (wanted.includes(topic)) return true;
      if (topic === "board") return wanted.some((item) => item.startsWith("board:"));
      return topic.startsWith("board:") && wanted.includes("board");
    });
  }

  function connect() {
    const all = Array.from(new Set(containers().flatMap(topicsOf))).sort().join(",");
    if (all === subscribed) return;
    subscribed = all;
    if (source) source.close();
    source = null;
    if (!all) return;
    source = new EventSource(document.body.dataset.liveUrl + "?topics=" + encodeURIComponent(all));
    source.onmessage = function (message) {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch (error) {
        return;
      }
      if (data.client === client) return;
      const hit = containers().filter((element) => matches(topicsOf(element), data.topics));
      if (!hit.length) return;
      hit.forEach((element) => pending.add(element.id));
      notice(data.by);
      // Одна правка может прийти несколькими событиями подряд — обновляем один раз.
      clearTimeout(timer);
      timer = setTimeout(flush, 300);
    };
  }

  function busy(element) {
    if (document.body.dataset.dragging) return true;
    const active = document.activeElement;
    return Boolean(
      active && element.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)
    );
  }

  function flush() {
    Array.from(pending).forEach(function (id) {
      const element = document.getElementById(id);
      if (!element) {
        pending.delete(id);
        return;
      }
      if (busy(element)) return;
      pending.delete(id);
      const scrolls = Array.from(element.querySelectorAll("[data-keep-scroll]")).map((node) => [
        node.scrollTop,
        node.scrollLeft,
      ]);
      htmx
        .ajax("GET", location.pathname + location.search, {
          target: "#" + id,
          select: "#" + id,
          swap: "outerHTML",
        })
        .then(function () {
          const fresh = document.getElementById(id);
          if (!fresh) return;
          fresh.querySelectorAll("[data-keep-scroll]").forEach(function (node, index) {
            if (scrolls[index]) [node.scrollTop, node.scrollLeft] = scrolls[index];
          });
        });
    });
  }

  function notice(by) {
    let box = document.getElementById("live-notice");
    if (!box) {
      box = document.createElement("div");
      box.id = "live-notice";
      box.className = "live-notice";
      box.setAttribute("role", "status");
      document.body.appendChild(box);
    }
    box.textContent = by ? "Обновлено. Автор изменений: " + by : "Обновлено";
    box.classList.add("show");
    clearTimeout(box.timer);
    box.timer = setTimeout(() => box.classList.remove("show"), 4000);
  }

  document.addEventListener("focusout", () => setTimeout(flush, 0));
  document.addEventListener("dragend", () => setTimeout(flush, 0));
  // После перехода по hx-boost на странице другие контейнеры — переподписываемся.
  document.addEventListener("htmx:afterSettle", connect);
  document.addEventListener("DOMContentLoaded", connect);
})();
