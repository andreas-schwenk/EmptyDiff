/*
 * Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
 * Created in part with AI help (Claude, Anthropic).
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

// The only JavaScript file: small enhancements; the pages also work without it.

// Open dialogs prepared on the server (e.g. simulated mail) as a modal.
for (const d of document.querySelectorAll("dialog[data-autoshow]")) {
  d.removeAttribute("open");
  d.showModal();
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

// Marks the page as scripted: parts that need JavaScript to be usable (folding) are only then folded.
document.documentElement.classList.add("js");

// Student groups in the submission list of a lab: the button folds the earlier submissions in and out.
document.addEventListener("click", (e) => {
  const button = e.target.closest && e.target.closest("[data-group-toggle]");
  if (!button) return;
  const group = button.closest("tbody.group");
  const folded = group.classList.toggle("collapsed");
  button.setAttribute("aria-expanded", String(!folded));
});

// File upload: list the chosen files, highlight the drop zone while dragging.
for (const zone of document.querySelectorAll("[data-dropzone]")) {
  const input = zone.querySelector("input[type=file]");
  const list = zone.parentElement.querySelector("[data-file-list]");
  const render = () => {
    const items = Array.from(input.files).map((f) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      const size = document.createElement("span");
      name.textContent = f.name;
      size.textContent = formatSize(f.size);
      li.append(name, size);
      return li;
    });
    list.replaceChildren(...items);
    zone.classList.toggle("has-files", items.length > 0);
  };
  input.addEventListener("change", render);
  for (const ev of ["dragenter", "dragover"]) zone.addEventListener(ev, () => zone.classList.add("dragover"));
  for (const ev of ["dragleave", "drop"]) zone.addEventListener(ev, () => zone.classList.remove("dragover"));
  // Take the dropped files over ourselves (the browser would otherwise open them, Safari above all).
  zone.addEventListener("dragover", (e) => e.preventDefault());
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}

// A file dropped beside the drop zone must not make the browser open it and leave the page.
for (const ev of ["dragover", "drop"]) {
  window.addEventListener(ev, (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files")) e.preventDefault();
  });
}

// Forms with data-confirm ask before submitting (e.g. deleting), forms with data-busy disable the button so the form
// is not submitted twice. One listener for the whole document: it also serves forms that a page refresh swaps in.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.hasAttribute("data-confirm") && !confirm(form.dataset.confirm)) {
    e.preventDefault();
    return;
  }
  if (form.hasAttribute("data-busy")) {
    const button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = true;
      button.textContent = form.dataset.busy;
    }
  }
});

// Lists of file names (required files): one input per name, validated while typing.
// Without JavaScript the textarea (one name per line) stays usable; here it is only hidden and
// filled with the names of the list.
const X_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13"/></svg>';
const NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
for (const box of document.querySelectorAll("[data-names]")) {
  const area = box.querySelector("textarea");
  const d = box.dataset;
  const rows = document.createElement("div");
  rows.className = "name-rows";
  const empty = document.createElement("p");
  empty.className = "muted";
  empty.textContent = d.empty;
  const add = document.createElement("button");
  add.type = "button";
  add.className = "btn btn-ghost btn-sm name-add";
  add.textContent = "+ " + d.add;

  const inputs = () => Array.from(rows.querySelectorAll("input"));
  const check = () => {
    const values = inputs().map((i) => i.value.trim());
    inputs().forEach((input, n) => {
      const value = values[n];
      let message = "";
      if (/\s/.test(value)) message = d.msgSpace;
      else if (value && !NAME_RE.test(value)) message = d.msgInvalid;
      else if (value && values.indexOf(value) !== n) message = d.msgDup;
      input.setCustomValidity(message);
      input.classList.toggle("invalid", message !== "");
      input.parentElement.querySelector(".field-error").textContent = message;
    });
    area.value = values.filter(Boolean).join("\n");
    empty.hidden = inputs().length > 0;
  };
  const addRow = (value, focus) => {
    const row = document.createElement("div");
    row.className = "name-row";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "mono";
    input.value = value;
    input.placeholder = d.placeholder;
    input.spellcheck = false;
    input.autocapitalize = "none";
    input.setAttribute("aria-label", d.label);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-ghost btn-icon";
    remove.innerHTML = X_ICON;  // fixed string, no user data
    remove.title = d.remove;
    remove.setAttribute("aria-label", d.remove);
    const error = document.createElement("p");
    error.className = "field-error";
    error.setAttribute("role", "alert");
    row.append(input, remove, error);
    rows.append(row);
    input.addEventListener("input", check);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {  // Enter must not submit the whole form
        e.preventDefault();
        addRow("", true);
      }
    });
    remove.addEventListener("click", () => {
      row.remove();
      check();
    });
    if (focus) input.focus();
    check();
  };

  for (const part of area.value.split(/[\n,]/)) if (part.trim()) addRow(part.trim(), false);
  add.addEventListener("click", () => addRow("", true));
  area.hidden = true;
  area.after(rows, empty, add);
  check();
}

// Links with data-open-dialog open a dialog instead (e.g. confirmation before discarding);
// without JavaScript the link is followed directly.
for (const link of document.querySelectorAll("[data-open-dialog]")) {
  link.addEventListener("click", (e) => {
    const dialog = document.getElementById(link.dataset.openDialog);
    if (dialog) {
      e.preventDefault();
      dialog.showModal();
    }
  });
}

// Forms with data-inplace (switches in the administration): submit via fetch and replace only <main>,
// so that scroll position, expanded courses and focus are kept. On any error the form is
// submitted normally.
let inplaceBusy = false;
document.addEventListener("submit", async (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-inplace")) return;
  e.preventDefault();
  if (inplaceBusy) return;
  inplaceBusy = true;
  const main = document.querySelector("main");
  try {
    const resp = await fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" });
    const fresh = new DOMParser().parseFromString(await resp.text(), "text/html").querySelector("main");
    if (!resp.ok || !fresh || new URL(resp.url).pathname !== location.pathname) throw new Error("unexpected response");
    for (const d of main.querySelectorAll("details[id]")) {
      const next = fresh.querySelector("#" + CSS.escape(d.id));
      if (next) next.open = d.open;
    }
    const index = Array.from(main.querySelectorAll("button")).indexOf(e.submitter);
    const y = window.scrollY;
    main.replaceChildren(...fresh.childNodes);
    window.scrollTo(0, y);
    const button = index >= 0 ? main.querySelectorAll("button")[index] : null;
    if (button) button.focus({ preventScroll: true });
  } catch (err) {
    form.submit();
  } finally {
    inplaceBusy = false;
  }
});

// Syntax highlighting in textareas with data-highlight="<language>" (testbench, helper functions): a coloured copy of
// the text lies underneath the transparent textarea and scrolls with it. Without JavaScript the plain textarea stays.
// The tokenizer of a language is a small copy of its server-side one in emptydiff/core/languages/ (comments, strings,
// numbers, keywords, built-ins). A language without a tokenizer here is shown without colours.
const HL_KEYWORDS = new Set(`break case catch classdef continue do else elseif end end_try_catch
  end_unwind_protect endclassdef endfor endfunction endif endparfor endswitch endwhile enumeration events for
  function global if methods otherwise parfor persistent properties return switch try until unwind_protect
  unwind_protect_cleanup while`.split(/\s+/));
const HL_BUILTINS = new Set(`abs all any ceil clc clear close cos cumsum diag diff disp eig error exp eye factorial
  figure find fix floor fprintf hold inv isempty isnumeric ischar length linspace log log10 log2 max mean min mod
  nan norm numel ones plot pi printf prod rand rank rem reshape round sin size sort sprintf sqrt sum tan trace
  zeros inf true false check check_close`.split(/\s+/));
const HL_TOKEN = new RegExp(
  [
    "([ \\t]+)", // 1 whitespace
    "((?:[%#]|\\.\\.\\.)[^\\n]*)", // 2 comment
    '("(?:[^"\\\\\\n]|\\\\.|"")*")', // 3 string
    "((?:0[xX][0-9A-Fa-f]+|(?:\\d+(?:\\.(?!\\.)\\d*)?|\\.\\d+)(?:[eEdD][+-]?\\d+)?[ij]?))", // 4 number
    "([A-Za-z_]\\w*)", // 5 word
    "([\\s\\S])", // 6 anything else
  ].join("|"),
  "y",
);
const HL_SQ_STRING = /'(?:[^'\n]|'')*'/y;
const HL_TRANSPOSE_AFTER = /[\])}'.A-Za-z0-9_]/;

function highlightLine(line, add) {
  let pos = 0;
  while (pos < line.length) {
    HL_TOKEN.lastIndex = pos;
    const m = HL_TOKEN.exec(line);
    let text = m[0];
    let kind = m[2] ? "comment" : m[3] ? "string" : m[4] ? "number" : null;
    if (m[5]) kind = HL_KEYWORDS.has(text) ? "keyword" : HL_BUILTINS.has(text) ? "builtin" : null;
    if (m[6] && text === "'" && !(pos && HL_TRANSPOSE_AFTER.test(line[pos - 1]))) {
      HL_SQ_STRING.lastIndex = pos; // otherwise a transpose (a') and not the start of a string
      const sq = HL_SQ_STRING.exec(line);
      if (sq) {
        kind = "string";
        text = sq[0];
      }
    }
    add(kind, text);
    pos += text.length;
  }
}

const HIGHLIGHTERS = { octave: highlightLine };

function highlightNodes(text, language) {
  const tokenizeLine = HIGHLIGHTERS[language];
  if (!tokenizeLine) return [document.createTextNode(text)];
  const nodes = [];
  let last = null;
  const add = (kind, chunk) => {
    if (last && last.kind === kind) {
      last.text += chunk;
      return;
    }
    last = { kind, text: chunk };
    nodes.push(last);
  };
  let inBlock = false;
  text.split("\n").forEach((line, i) => {
    if (i) add(null, "\n");
    if (inBlock) {
      if (/^\s*[%#]\}\s*$/.test(line)) inBlock = false;
      add("comment", line);
    } else if (/^\s*[%#]\{\s*$/.test(line)) {
      inBlock = true;
      add("comment", line);
    } else {
      tokenizeLine(line, add);
    }
  });
  return nodes.map((n) => {
    if (!n.kind) return document.createTextNode(n.text);
    const span = document.createElement("span");
    span.className = "tok-" + n.kind;
    span.textContent = n.text;
    return span;
  });
}

for (const area of document.querySelectorAll("textarea[data-highlight]")) {
  const wrap = document.createElement("div");
  const layer = document.createElement("pre");
  const code = document.createElement("code");
  wrap.className = "hl-wrap";
  layer.className = "hl-layer";
  layer.setAttribute("aria-hidden", "true");
  layer.append(code);
  area.classList.add("hl-input");
  area.parentNode.insertBefore(wrap, area);
  wrap.append(layer, area);

  // The layer has no scroll bar of its own: leave room for the one of the textarea so that lines wrap alike.
  const fit = () => {
    layer.style.right = area.offsetWidth - area.clientWidth + "px";
    layer.scrollTop = area.scrollTop;
  };
  let queued = false;
  const paint = () => {
    queued = false;
    // a final line break needs a character, otherwise the empty last line would be missing in the layer
    code.replaceChildren(...highlightNodes(area.value, area.dataset.highlight), document.createTextNode(" "));
    fit();
  };
  const schedule = () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(paint);
    }
  };
  area.addEventListener("input", schedule);
  area.addEventListener("scroll", fit);
  if (window.ResizeObserver) new ResizeObserver(fit).observe(area);
  paint();
}

// Language of a lab (lab form): the help texts and the highlighting follow the chosen language.
for (const select of document.querySelectorAll("select[data-language-select]")) {
  const apply = () => {
    for (const block of document.querySelectorAll("[data-language-block]")) block.hidden = block.dataset.languageBlock !== select.value;
    for (const area of document.querySelectorAll("textarea[data-highlight]")) {
      area.dataset.highlight = select.value;
      area.dispatchEvent(new Event("input")); // repaint
    }
    const names = document.querySelector("[data-names][data-placeholder-template]");
    const example = select.selectedOptions[0] && select.selectedOptions[0].dataset.example;
    if (names && example) names.dataset.placeholder = names.dataset.placeholderTemplate.replace("{example}", example);
  };
  select.addEventListener("change", apply);
}

// A submission that is still being checked: ask for the page every two seconds. When the result is there, swap in the
// new <main>, move the focus to the heading and announce the result to screen readers (the page used to reload
// itself with <meta http-equiv="refresh">, which resets the reading position; that stays as the fallback without JS).
// A list with data-poll="list" (the submissions of a lab) is refreshed in place on every round while something in it
// is still being checked: the content of <main> is replaced, scroll position and focus stay.
const pending = document.querySelector("[data-poll]");
if (pending && window.DOMParser) {
  const announce = pending.dataset.announce || "";
  const listMode = pending.dataset.poll === "list";
  const tick = async () => {
    try {
      const resp = await fetch(location.href, { credentials: "same-origin" });
      const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
      const next = doc.querySelector("main");
      if (listMode && resp.ok && next) {
        const y = window.scrollY;
        const main = document.querySelector("main");
        const unfolded = Array.from(main.querySelectorAll("tbody.group:not(.collapsed)"), (g) => g.id);
        main.replaceChildren(...document.importNode(next, true).childNodes);
        for (const id of unfolded) {
          const group = id && main.querySelector("#" + CSS.escape(id));
          if (!group) continue;
          group.classList.remove("collapsed"); // what the person had opened stays open
          group.querySelector("[data-group-toggle]")?.setAttribute("aria-expanded", "true");
        }
        window.scrollTo(0, y);
        if (doc.querySelector("[data-poll]")) setTimeout(tick, 2000);
        return;
      }
      if (resp.ok && next && !doc.querySelector("[data-poll]")) {
        document.title = doc.title;
        document.querySelector("main").replaceWith(document.importNode(next, true));
        const main = document.querySelector("main");
        const heading = main.querySelector("h1");
        if (heading) {
          heading.tabIndex = -1;
          heading.focus();
        }
        const result = main.querySelector(".result");
        const live = document.getElementById("live");
        if (live && result) {
          const copy = result.cloneNode(true);
          for (const symbol of copy.querySelectorAll("[aria-hidden]")) symbol.remove(); // the tick or cross
          live.textContent = announce.replace("RESULT", copy.textContent.trim());
        }
        return;
      }
    } catch (e) {
      // network problem: try again
    }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
}

// Tooltips: the browser shows the text of a title attribute only after a pause and in a box that cannot be styled.
// Here every title is shown at once in our own box (class "tooltip"), on hover and on keyboard focus. The title is
// taken over when the element is first touched, before the browser's own box could appear; the text then stays as
// aria-description for screen readers. Without JavaScript the normal browser tooltip remains.
(() => {
  const box = document.createElement("div");
  box.className = "tooltip";
  box.setAttribute("role", "tooltip");
  box.hidden = true;
  document.body.append(box);
  let current = null;

  const adopt = (node) => {
    if (!node.hasAttribute || !node.hasAttribute("title")) return;
    const text = node.getAttribute("title").trim();
    node.removeAttribute("title");
    if (!text) return;
    node.dataset.tip = text;
    if (!node.hasAttribute("aria-label") && !node.hasAttribute("aria-labelledby") && !node.hasAttribute("aria-description")) {
      node.setAttribute("aria-description", text);
    }
  };
  const hide = () => {
    box.hidden = true;
    current = null;
  };
  const show = (el) => {
    current = el;
    (el.closest("dialog[open]") || document.body).append(box); // a modal dialog covers everything outside of it
    box.textContent = el.dataset.tip;
    box.style.left = "0px";
    box.style.top = "0px";
    box.hidden = false;
    const rect = el.getBoundingClientRect();
    const size = box.getBoundingClientRect();
    const margin = 8;
    let top = rect.bottom + 6;
    if (top + size.height > window.innerHeight - margin && rect.top - size.height - 6 > margin) top = rect.top - size.height - 6;
    const left = Math.min(Math.max(rect.left, margin), Math.max(window.innerWidth - size.width - margin, margin));
    box.style.left = left + "px";
    box.style.top = top + "px";
  };
  const target = (event) => {
    for (let node = event.target; node && node !== document; node = node.parentElement) adopt(node);
    return event.target.closest ? event.target.closest("[data-tip]") : null;
  };
  const enter = (event) => {
    const el = target(event);
    if (el && el !== current) show(el);
  };
  const leave = (event) => {
    if (current && !(event.relatedTarget && current.contains(event.relatedTarget))) hide();
  };
  document.addEventListener("mouseover", enter);
  document.addEventListener("focusin", enter);
  document.addEventListener("mouseout", leave);
  document.addEventListener("focusout", leave);
  document.addEventListener("mousedown", hide);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
})();
