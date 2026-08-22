(() => {
  "use strict";

  const bootstrapElement = document.getElementById("netbox-ai-navigator-bootstrap");
  if (!bootstrapElement || document.getElementById("netbox-ai-navigator")) {
    return;
  }

  let config;
  try {
    config = JSON.parse(bootstrapElement.textContent);
  } catch (_error) {
    return;
  }

  const defaultTranslations = {
    open_assistant: "Open NetBox AI Navigator",
    dialog_label: "NetBox AI Navigator",
    subtitle: "Read-only · your permissions",
    expand_assistant: "Expand assistant",
    restore_assistant: "Restore assistant size",
    clear_conversation: "Clear conversation",
    close_assistant: "Close assistant",
    welcome: "Ask me about the NetBox data you are permitted to view on this page.",
    question: "Question",
    placeholder: "Ask about your NetBox…",
    send_question: "Send question",
    send: "Send",
    notice: "Answers may be inaccurate. Verify important data in NetBox.",
    thinking: "Thinking…",
    request_failed: "Request failed with HTTP {status}.",
    assistant_unavailable: "The assistant is currently unavailable.",
    reset_failed: "Reset failed with HTTP {status}.",
    conversation_cleared: "Conversation cleared. What would you like to explore?",
    conversation_clear_failed: "The conversation could not be cleared.",
  };
  const translations = {
    ...defaultTranslations,
    ...(config.translations && typeof config.translations === "object" ? config.translations : {}),
  };

  function translate(key, values = {}) {
    let value = typeof translations[key] === "string" ? translations[key] : defaultTranslations[key] || key;
    Object.entries(values).forEach(([name, replacement]) => {
      value = value.replaceAll(`{${name}}`, String(replacement));
    });
    return value;
  }

  const history = [];
  const historyStoragePrefix = "netbox-ai-navigator:history:";
  const activeStorageMarker = "netbox-ai-navigator:active-history";
  const historyStorageKey =
    typeof config.storage_token === "string" && config.storage_token
      ? `${historyStoragePrefix}${config.storage_token}`
      : null;
  let pending = false;

  const root = document.createElement("section");
  root.id = "netbox-ai-navigator";
  root.className = "nbai";
  root.innerHTML = `
    <button class="nbai__launcher" type="button" aria-expanded="false">
      <i class="mdi mdi-robot-outline" aria-hidden="true"></i>
    </button>
    <div class="nbai__panel" role="dialog" aria-hidden="true">
      <header class="nbai__header">
        <div>
          <strong class="nbai__title">AI Navigator</strong>
          <small class="nbai__subtitle"></small>
        </div>
        <div class="nbai__actions">
          <button class="nbai__icon-button nbai__expand" type="button" aria-pressed="false">
            <i class="mdi mdi-arrow-expand-all" aria-hidden="true"></i>
          </button>
          <button class="nbai__icon-button nbai__clear" type="button">
            <i class="mdi mdi-refresh" aria-hidden="true"></i>
          </button>
          <button class="nbai__icon-button nbai__close" type="button">
            <i class="mdi mdi-close" aria-hidden="true"></i>
          </button>
        </div>
      </header>
      <div class="nbai__messages" aria-live="polite">
        <div class="nbai__message nbai__message--assistant"></div>
      </div>
      <form class="nbai__form">
        <label class="visually-hidden nbai__question-label" for="nbai-question"></label>
        <textarea id="nbai-question" rows="1" maxlength="12000" required></textarea>
        <button class="nbai__send" type="submit"></button>
      </form>
      <div class="nbai__notice"></div>
    </div>`;
  document.body.appendChild(root);

  const launcher = root.querySelector(".nbai__launcher");
  const panel = root.querySelector(".nbai__panel");
  const closeButton = root.querySelector(".nbai__close");
  const clearButton = root.querySelector(".nbai__clear");
  const expandButton = root.querySelector(".nbai__expand");
  const expandIcon = expandButton.querySelector(".mdi");
  const messagesElement = root.querySelector(".nbai__messages");
  const form = root.querySelector(".nbai__form");
  const input = root.querySelector("textarea");
  const sendButton = root.querySelector(".nbai__send");
  let panelSizeBeforeExpand = null;

  launcher.setAttribute("aria-label", translate("open_assistant"));
  panel.setAttribute("aria-label", translate("dialog_label"));
  root.querySelector(".nbai__subtitle").textContent = translate("subtitle");
  clearButton.setAttribute("aria-label", translate("clear_conversation"));
  closeButton.setAttribute("aria-label", translate("close_assistant"));
  messagesElement.firstElementChild.textContent = translate("welcome");
  root.querySelector(".nbai__question-label").textContent = translate("question");
  input.placeholder = translate("placeholder");
  sendButton.setAttribute("aria-label", translate("send_question"));
  sendButton.textContent = translate("send");
  root.querySelector(".nbai__notice").textContent = translate("notice");

  function setOpen(open) {
    root.classList.toggle("nbai--open", open);
    launcher.setAttribute("aria-expanded", String(open));
    panel.setAttribute("aria-hidden", String(!open));
    if (open) {
      input.focus();
    }
  }

  function setExpanded(expanded) {
    if (expanded) {
      panelSizeBeforeExpand = { width: panel.style.width, height: panel.style.height };
      panel.style.removeProperty("width");
      panel.style.removeProperty("height");
    } else if (panelSizeBeforeExpand) {
      panel.style.width = panelSizeBeforeExpand.width;
      panel.style.height = panelSizeBeforeExpand.height;
      panelSizeBeforeExpand = null;
    }
    root.classList.toggle("nbai--expanded", expanded);
    expandButton.setAttribute("aria-pressed", String(expanded));
    expandButton.setAttribute("aria-label", translate(expanded ? "restore_assistant" : "expand_assistant"));
    expandIcon.classList.toggle("mdi-arrow-expand-all", !expanded);
    expandIcon.classList.toggle("mdi-arrow-collapse-all", expanded);
  }

  setExpanded(false);

  function appendInlineMarkdown(container, text) {
    const pattern = /(\[([^\]]+)]\(([^)\s]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*)/g;
    let cursor = 0;
    let match;

    while ((match = pattern.exec(text)) !== null) {
      container.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      if (match[2] && match[3]) {
        try {
          const target = new URL(match[3], window.location.origin);
          if (target.origin !== window.location.origin || !["http:", "https:"].includes(target.protocol)) {
            throw new Error("External link rejected");
          }
          const link = document.createElement("a");
          link.href = target.href;
          link.textContent = match[2];
          container.appendChild(link);
        } catch (_error) {
          container.appendChild(document.createTextNode(match[2]));
        }
      } else {
        const element = document.createElement(match[4] ? "code" : (match[5] || match[6]) ? "strong" : "em");
        const inlineContent = match[4] || match[5] || match[6] || match[7];
        if (match[4]) {
          element.textContent = inlineContent;
        } else {
          appendInlineMarkdown(element, inlineContent);
        }
        container.appendChild(element);
      }
      cursor = pattern.lastIndex;
    }
    container.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function splitTableRow(line) {
    let value = line.trim();
    if (value.startsWith("|")) {
      value = value.slice(1);
    }
    if (value.endsWith("|")) {
      value = value.slice(0, -1);
    }

    const cells = [];
    let cell = "";
    for (let index = 0; index < value.length; index += 1) {
      if (value[index] === "\\" && value[index + 1] === "|") {
        cell += "|";
        index += 1;
      } else if (value[index] === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += value[index];
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  function tableAlignments(line) {
    const cells = splitTableRow(line);
    if (cells.length < 2 || !cells.every((cell) => /^:?-{3,}:?$/.test(cell))) {
      return null;
    }
    return cells.map((cell) => {
      if (cell.startsWith(":") && cell.endsWith(":")) {
        return "center";
      }
      return cell.endsWith(":") ? "right" : "left";
    });
  }

  function renderMarkdown(container, text) {
    const lines = text.replace(/\r\n?/g, "\n").split("\n");
    let index = 0;

    function appendParagraph(values) {
      const paragraph = document.createElement("p");
      appendInlineMarkdown(paragraph, values.join(" ").trim());
      container.appendChild(paragraph);
    }

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*```([\w-]*)\s*$/);
      if (fence) {
        const values = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          values.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) {
          index += 1;
        }
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        if (fence[1]) {
          code.dataset.language = fence[1];
        }
        code.textContent = values.join("\n");
        pre.appendChild(code);
        container.appendChild(pre);
        continue;
      }

      const alignments = index + 1 < lines.length ? tableAlignments(lines[index + 1]) : null;
      if (alignments && line.includes("|")) {
        const headers = splitTableRow(line);
        const wrapper = document.createElement("div");
        wrapper.className = "nbai__table-wrap";
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const headRow = document.createElement("tr");
        headers.forEach((value, column) => {
          const header = document.createElement("th");
          header.scope = "col";
          header.dataset.align = alignments[column] || "left";
          appendInlineMarkdown(header, value);
          headRow.appendChild(header);
        });
        head.appendChild(headRow);
        table.appendChild(head);

        const body = document.createElement("tbody");
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          const row = document.createElement("tr");
          const cells = splitTableRow(lines[index]);
          headers.forEach((_header, column) => {
            const cell = document.createElement("td");
            cell.dataset.align = alignments[column] || "left";
            appendInlineMarkdown(cell, cells[column] || "");
            row.appendChild(cell);
          });
          body.appendChild(row);
          index += 1;
        }
        table.appendChild(body);
        wrapper.appendChild(table);
        container.appendChild(wrapper);
        continue;
      }

      const listMatch = line.match(/^\s*([-*+]\s+|\d+\.\s+)(.*)$/);
      if (listMatch) {
        const ordered = /\d+\./.test(listMatch[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const itemMatch = lines[index].match(/^\s*([-*+]\s+|\d+\.\s+)(.*)$/);
          if (!itemMatch || /\d+\./.test(itemMatch[1]) !== ordered) {
            break;
          }
          const item = document.createElement("li");
          appendInlineMarkdown(item, itemMatch[2]);
          list.appendChild(item);
          index += 1;
        }
        container.appendChild(list);
        continue;
      }

      const headingMatch = line.match(/^\s*#{1,4}\s+(.+)$/);
      if (headingMatch) {
        const heading = document.createElement("h3");
        heading.className = "nbai__heading";
        appendInlineMarkdown(heading, headingMatch[1]);
        container.appendChild(heading);
        index += 1;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quote = document.createElement("blockquote");
        const values = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          values.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        appendInlineMarkdown(quote, values.join(" "));
        container.appendChild(quote);
        continue;
      }

      if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
        container.appendChild(document.createElement("hr"));
        index += 1;
        continue;
      }

      const paragraph = [line];
      index += 1;
      while (
        index < lines.length &&
        lines[index].trim() &&
        !/^\s*```/.test(lines[index]) &&
        !/^\s*([-*+]\s+|\d+\.\s+)/.test(lines[index]) &&
        !/^\s*#{1,4}\s+/.test(lines[index]) &&
        !/^\s*>\s?/.test(lines[index]) &&
        !/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(lines[index]) &&
        !(index + 1 < lines.length && lines[index].includes("|") && tableAlignments(lines[index + 1]))
      ) {
        paragraph.push(lines[index]);
        index += 1;
      }
      appendParagraph(paragraph);
    }
  }

  function addMessage(role, content, modifier = "") {
    const message = document.createElement("div");
    message.className = `nbai__message nbai__message--${role}${modifier ? ` nbai__message--${modifier}` : ""}`;
    if (role === "assistant" && !modifier) {
      renderMarkdown(message, content);
      if (message.querySelector("table, pre, ul, ol, blockquote")) {
        message.classList.add("nbai__message--rich");
      }
    } else {
      message.textContent = content;
    }
    messagesElement.appendChild(message);
    messagesElement.scrollTop = messagesElement.scrollHeight;
    return message;
  }

  function trimHistory() {
    if (history.length > 20) {
      history.splice(0, history.length - 20);
    }
  }

  function persistHistory() {
    if (!historyStorageKey) {
      return;
    }
    try {
      localStorage.setItem(historyStorageKey, JSON.stringify(history));
    } catch (_error) {
      // Storage may be disabled by the browser; the current page still works normally.
    }
  }

  function restoreHistory() {
    if (!historyStorageKey) {
      return;
    }
    try {
      const previousKey = localStorage.getItem(activeStorageMarker);
      if (previousKey && previousKey !== historyStorageKey && previousKey.startsWith(historyStoragePrefix)) {
        localStorage.removeItem(previousKey);
      }
      localStorage.setItem(activeStorageMarker, historyStorageKey);

      const stored = JSON.parse(localStorage.getItem(historyStorageKey) || "[]");
      if (!Array.isArray(stored)) {
        return;
      }
      const restored = stored
        .filter(
          (message) =>
            message &&
            ["user", "assistant"].includes(message.role) &&
            typeof message.content === "string" &&
            message.content.trim() &&
            message.content.length <= 12000,
        )
        .slice(-20)
        .map((message) => ({ role: message.role, content: message.content }));
      if (!restored.length) {
        return;
      }

      history.push(...restored);
      messagesElement.replaceChildren();
      history.forEach((message) => addMessage(message.role, message.content));
    } catch (_error) {
      // Ignore unavailable or malformed browser storage and start with an empty view.
    }
  }

  function resizeInput() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }

  async function askAssistant(question) {
    history.push({ role: "user", content: question });
    trimHistory();
    persistHistory();

    const loading = addMessage("assistant", translate("thinking"), "loading");
    pending = true;
    input.disabled = true;
    sendButton.disabled = true;

    try {
      const pageContext = {
        ...(config.page_context || {}),
        title: document.title,
        url: `${window.location.pathname}${window.location.search}`,
      };
      const response = await fetch(config.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": config.csrf_token,
        },
        body: JSON.stringify({ messages: history, page_context: pageContext }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || translate("request_failed", { status: response.status }));
      }
      loading.remove();
      addMessage("assistant", data.answer);
      history.push({ role: "assistant", content: data.answer });
      trimHistory();
      persistHistory();
    } catch (error) {
      loading.remove();
      addMessage("assistant", error.message || translate("assistant_unavailable"), "error");
      if (history.at(-1)?.role === "user" && history.at(-1)?.content === question) {
        history.pop();
        persistHistory();
      }
    } finally {
      pending = false;
      input.disabled = false;
      sendButton.disabled = false;
      input.focus();
    }
  }

  async function clearConversation() {
    if (pending) {
      return;
    }

    pending = true;
    input.disabled = true;
    sendButton.disabled = true;
    clearButton.disabled = true;
    try {
      if (config.reset_endpoint) {
        const response = await fetch(config.reset_endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": config.csrf_token,
          },
          body: "{}",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.error || translate("reset_failed", { status: response.status }));
        }
      }

      history.splice(0);
      persistHistory();
      messagesElement.replaceChildren();
      addMessage("assistant", translate("conversation_cleared"));
    } catch (error) {
      addMessage("assistant", error.message || translate("conversation_clear_failed"), "error");
    } finally {
      pending = false;
      input.disabled = false;
      sendButton.disabled = false;
      clearButton.disabled = false;
      input.focus();
    }
  }

  restoreHistory();

  launcher.addEventListener("click", () => setOpen(!root.classList.contains("nbai--open")));
  expandButton.addEventListener("click", () => setExpanded(!root.classList.contains("nbai--expanded")));
  closeButton.addEventListener("click", () => setOpen(false));
  clearButton.addEventListener("click", clearConversation);
  input.addEventListener("input", resizeInput);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || pending) {
      return;
    }
    addMessage("user", question);
    input.value = "";
    resizeInput();
    askAssistant(question);
  });
})();
