(() => {
  "use strict";

  const filters = document.getElementById("archive-filters");
  const properties = document.getElementById("properties");
  const version = filters?.dataset.version;
  if (!properties || !version || !/^[0-9a-f]{16}$/.test(version)) return;

  const form = filters.querySelector("form");
  const { area, beds, price, change } = form.elements;
  const panel = filters.querySelector("details");
  const activeCount = filters.querySelector(".filter-active-count");
  const status = filters.querySelector(".filter-status");
  const retry = filters.querySelector(".filter-retry");
  const pagers = document.querySelectorAll(".pagination");
  const original = [...properties.childNodes];
  const page = Number(filters.dataset.page);
  const pages = Number(filters.dataset.pages);
  const total = Number(filters.dataset.total);
  const cachedPages = new Map([[page, properties.querySelectorAll("section[data-day]")]]);
  let archive;
  let revision = 0;

  class ArchiveChanged extends Error {}

  async function readPage(number) {
    // Only request this report's own published pages, when a visitor uses a filter.
    const file = number === 1 ? "index.html" : `page-${number}.html`;
    const response = await fetch(`${file}?archive=${version}`, {
      mode: "same-origin", credentials: "omit", redirect: "error", cache: "no-cache",
      signal: window.AbortSignal?.timeout?.(20000),
    });
    if (!response.ok) throw new Error("Page unavailable");
    // Template contents are inert. Unmatched photos and other page assets stay unloaded.
    const template = document.createElement("template");
    template.innerHTML = await response.text();
    const metadata = template.content.querySelector("#archive-filters")?.dataset;
    const results = template.content.querySelector("#properties");
    if (!results || metadata?.version !== version || Number(metadata.page) !== number ||
        Number(metadata.pages) !== pages || Number(metadata.total) !== total) {
      throw new ArchiveChanged();
    }
    return results.querySelectorAll("section[data-day]");
  }

  function sectionsFor(number) {
    if (!cachedPages.has(number)) {
      cachedPages.set(number, readPage(number).catch(error => {
        cachedPages.delete(number);
        throw error;
      }));
    }
    return cachedPages.get(number);
  }

  function groupDays(allPages) {
    const days = new Map();
    let count = 0;
    for (const sections of allPages) {
      for (const section of sections) {
        const key = section.dataset.day;
        if (!days.has(key)) {
          const shell = section.cloneNode(true);
          shell.querySelector("[data-cards]").replaceChildren();
          days.set(key, { shell, cards: [] });
        }
        const cards = section.querySelectorAll("[data-cards] > article[data-price]");
        days.get(key).cards.push(...cards);
        count += cards.length;
      }
    }
    if (count !== total) throw new Error("Incomplete archive");
    return days;
  }

  function loadArchive() {
    if (!archive) {
      const requests = Array.from({ length: pages }, (_, i) => sectionsFor(i + 1));
      archive = Promise.all(requests).then(allPages => {
        try {
          return groupDays(allPages);
        } catch (error) {
          // A partial or mixed snapshot should be fetched again on retry.
          for (const number of cachedPages.keys()) if (number !== page) cachedPages.delete(number);
          throw error;
        }
      }).catch(error => {
        archive = null;
        throw error;
      });
    }
    return archive;
  }

  function showOriginal() {
    if (properties.hasAttribute("data-filtered")) {
      properties.replaceChildren(...original);
      properties.removeAttribute("data-filtered");
      for (const pager of pagers) pager.hidden = false;
    }
  }

  function showMatches(days) {
    const output = document.createDocumentFragment();
    let count = 0;
    for (const { shell, cards } of days.values()) {
      const matches = cards.filter(({ dataset: d }) =>
        (!area.value || area.value === d.area) && (!beds.value || beds.value === d.beds) &&
        (!price.value || Number(d.price) <= price.valueAsNumber) &&
        (!change.value || (change.value === "changed" ? d.change : change.value === d.change)));
      if (!matches.length) continue;
      const section = shell.cloneNode(true);
      section.querySelector("[data-cards]").append(...matches.map(card => card.cloneNode(true)));
      const dayCount = section.querySelector("[data-day-count]");
      if (dayCount) dayCount.textContent = entries(matches.length);
      output.append(section);
      count += matches.length;
    }
    properties.replaceChildren(output);
    properties.setAttribute("data-filtered", "");
    for (const pager of pagers) pager.hidden = true;
    return count;
  }

  function entries(n) {
    return `${n} ${n === 1 ? "entry" : "entries"}`;
  }

  async function apply() {
    const current = ++revision;
    const active = [area.value, beds.value, price.value || !price.validity.valid, change.value].filter(Boolean).length;
    activeCount.hidden = !active;
    activeCount.textContent = active ? `${active} active` : "";
    retry.hidden = true;
    properties.removeAttribute("aria-busy");
    if (price.validity.valid) {
      price.removeAttribute("aria-invalid");
      price.removeAttribute("aria-describedby");
    } else {
      price.setAttribute("aria-invalid", "true");
      price.setAttribute("aria-describedby", "filter-status");
    }
    if (!active || !price.validity.valid) {
      showOriginal();
      status.textContent = price.validity.valid ? "" :
        "Enter a whole-pound maximum price of £0 or more. Showing the unfiltered page.";
      return;
    }
    properties.setAttribute("aria-busy", "true");
    status.textContent = "Searching the full archive…";
    try {
      const days = await loadArchive();
      if (current !== revision) return;
      const count = showMatches(days);
      status.textContent = count ? `Showing ${count} of ${entries(total)}.` :
        "No entries match your filters. Change or reset the filters to see more entries.";
    } catch (error) {
      if (current !== revision) return;
      showOriginal();
      const reason = error instanceof ArchiveChanged ? "The archive has changed." : "Couldn’t load the full archive.";
      status.textContent = `${reason} Showing unfiltered page ${page}. Retry or reload the page.`;
      retry.hidden = false;
    }
    properties.removeAttribute("aria-busy");
  }

  const wide = window.matchMedia("(min-width: 701px)");
  panel.open = wide.matches;
  wide.addEventListener("change", () => { panel.open = wide.matches; });
  form.addEventListener("change", apply);
  price.addEventListener("input", apply);
  // The native reset finishes after the reset event's handlers and microtasks.
  form.addEventListener("reset", () => setTimeout(apply, 0));
  form.addEventListener("submit", event => { event.preventDefault(); apply(); });
  retry.addEventListener("click", () => { panel.querySelector("summary").focus(); apply(); });
  filters.hidden = false;
  apply();
})();
