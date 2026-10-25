"use strict";

const STORAGE_KEY = "urly.links";
const MAX_LINKS = 20;
const DEFAULT_COOLDOWN_S = 5;

const MESSAGES = {
  422: "Enter a valid link: it must start with http:// or https:// and be at most 2048 characters.",
  503: "Service temporarily unavailable. Try again in a few seconds.",
  network: "Can't reach the server.",
  other: "Something went wrong. Please try again.",
};

const $ = (id) => document.getElementById(id);
const form = $("form");
const input = $("url");
const submit = $("submit");
const errorBox = $("error");
const resultBox = $("result");
const resultLink = $("result-link");
const copyBtn = $("copy");
const clearBtn = $("clear");
const list = $("links");
const empty = $("empty");

// --- storage (localStorage can be missing, full, or edited by hand) ---

function isHttpUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function isLink(x) {
  return (
    x !== null &&
    typeof x === "object" &&
    ["short_code", "short_url", "original_url", "created_at"].every((k) => typeof x[k] === "string")
  );
}

function loadLinks() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter(isLink).slice(0, MAX_LINKS) : [];
  } catch {
    return [];
  }
}

function saveLinks(links) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(links));
  } catch {
    /* storage unavailable: the list just won't persist */
  }
}

// --- rendering: user data goes in via textContent / setAttribute, never innerHTML ---

function setLink(anchor, href, text) {
  anchor.textContent = text;
  if (isHttpUrl(href)) {
    anchor.setAttribute("href", href);
  } else {
    anchor.removeAttribute("href");
  }
}

function render() {
  const links = loadLinks();
  list.replaceChildren();
  for (const link of links) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    setLink(a, link.short_url, link.short_url);
    a.setAttribute("rel", "noopener");
    const orig = document.createElement("span");
    orig.className = "orig";
    orig.textContent = link.original_url;
    li.append(a, orig);
    list.append(li);
  }
  empty.hidden = links.length > 0;
  clearBtn.disabled = links.length === 0;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

// --- submit ---

let cooldownTimer = null;

function setBusy(busy) {
  submit.disabled = busy;
  submit.textContent = busy ? "Shortening…" : "Shorten";
}

function startCooldown(seconds) {
  let left = seconds;
  submit.disabled = true;
  const tick = () => {
    if (left <= 0) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
      clearError(); // the outage message is stale once we let the user try again
      setBusy(false);
      return;
    }
    submit.textContent = `Try again in ${left}s`;
    left -= 1;
  };
  tick();
  cooldownTimer = setInterval(tick, 1000);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return; // one request at a time: every submit creates a new Link
  clearError();
  setBusy(true);

  let response;
  try {
    response = await fetch("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() }),
    });
  } catch {
    showError(MESSAGES.network);
    startCooldown(DEFAULT_COOLDOWN_S); // same slow-down as a 503: no hammering a dead backend
    return;
  }

  if (response.status === 201) {
    let link = null;
    try {
      link = await response.json();
    } catch {
      /* handled below */
    }
    if (!isLink(link)) {
      showError(MESSAGES.other);
      setBusy(false);
      return;
    }
    saveLinks([link, ...loadLinks()].slice(0, MAX_LINKS));
    setLink(resultLink, link.short_url, link.short_url);
    resultBox.hidden = false;
    copyBtn.textContent = "Copy";
    input.value = "";
    render();
    setBusy(false);
    return;
  }

  // 503 from the API; 502/504 come from the load balancer when no healthy API task answers.
  if ([502, 503, 504].includes(response.status)) {
    showError(MESSAGES[503]);
    const retryAfter = parseInt(response.headers.get("Retry-After"), 10);
    startCooldown(retryAfter > 0 && retryAfter <= 60 ? retryAfter : DEFAULT_COOLDOWN_S);
    return;
  }

  showError(MESSAGES[response.status] || MESSAGES.other);
  setBusy(false);
});

copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(resultLink.textContent);
    copyBtn.textContent = "Copied!";
  } catch {
    copyBtn.textContent = "Copy failed";
  }
});

clearBtn.addEventListener("click", () => {
  saveLinks([]);
  render();
});

render();
