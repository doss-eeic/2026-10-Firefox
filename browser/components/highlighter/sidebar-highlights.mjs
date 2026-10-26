/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

const { HighlighterStore } = ChromeUtils.importESModule(
  "moz-src:///browser/components/highlighter/HighlighterStore.sys.mjs"
);

async function render() {
  let highlights = await HighlighterStore.getAll();
  let list = document.getElementById("highlight-list");
  list.replaceChildren();
  for (let highlight of highlights) {
    let item = document.createElement("li");
    let text = document.createElement("span");
    text.className = "highlight-text";
    text.textContent = highlight.text;
    text.style.borderInlineStartColor = highlight.color || "yellow";
    let site = document.createElement("span");
    site.className = "highlight-site";
    site.textContent = new URL(highlight.url).hostname;
    item.append(text);
    if (highlight.memo) {
      let memo = document.createElement("span");
      memo.className = "highlight-memo";
      memo.textContent = highlight.memo;
      item.append(memo);
    }
    item.append(site);
    item.addEventListener("click", () => openHighlight(highlight));
    list.append(item);
  }
  document.getElementById("empty-message").hidden = highlights.length > 0;
}

function openHighlight(highlight) {
  HighlighterStore.requestScroll(highlight.url, highlight.id);
  window.top.openTrustedLinkIn(highlight.url, "tab");
}

const observer = () => render();
Services.obs.addObserver(observer, "highlighter-changed");
window.addEventListener("unload", () => {
  Services.obs.removeObserver(observer, "highlighter-changed");
});

render();
