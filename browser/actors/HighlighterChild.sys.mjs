/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

const CONTEXT_LENGTH = 32;
const SKIPPED_ELEMENTS = new Set(["script", "style", "noscript", "template"]);

function countMatchBefore(fullText, position, prefix) {
  let count = 0;
  while (
    count < prefix.length &&
    position - count - 1 >= 0 &&
    fullText[position - count - 1] == prefix[prefix.length - count - 1]
  ) {
    count++;
  }
  return count;
}

function countMatchAfter(fullText, position, suffix) {
  let count = 0;
  while (
    count < suffix.length &&
    position + count < fullText.length &&
    fullText[position + count] == suffix[count]
  ) {
    count++;
  }
  return count;
}

export class HighlighterChild extends JSWindowActorChild {
  async receiveMessage(message) {
    if (message.name == "Highlighter:Highlight") {
      let range = this.contentWindow.getSelection().getRangeAt(0);
      let page = this.collectText();
      let offsets = this.findRangeOffsets(page, range);
      if (!offsets) {
        return;
      }
      let { start, end } = offsets;
      let text = page.fullText.slice(start, end);
      let data = {
        text,
        prefix: page.fullText.slice(Math.max(0, start - CONTEXT_LENGTH), start),
        suffix: page.fullText.slice(end, end + CONTEXT_LENGTH),
        occurrence: this.countOccurrences(page.fullText, text, start),
        color: message.data.color,
      };
      let marks = this.wrapOffsets(page, start, end, data.color);
      let id = await this.sendQuery("Highlighter:Save", data);
      for (let mark of marks) {
        mark.dataset.highlightId = id;
      }
    } else if (message.name == "Highlighter:Remove") {
      this.removeMarks(message.data.id);
    }
  }

  async handleEvent(event) {
    if (event.type == "DOMContentLoaded") {
      let { highlights, scrollToId } = await this.sendQuery(
        "Highlighter:GetHighlights"
      );
      for (let highlight of highlights) {
        let page = this.collectText();
        let start = this.findBestMatch(page.fullText, highlight);
        if (start == -1) {
          continue;
        }
        let end = start + highlight.text.length;
        let marks = this.wrapOffsets(page, start, end, highlight.color);
        for (let mark of marks) {
          mark.dataset.highlightId = highlight.id;
        }
      }
      if (scrollToId != null) {
        this.document
          .querySelector(`mark[data-highlight-id="${scrollToId}"]`)
          ?.scrollIntoView({ block: "center" });
      }
    }
  }

  collectText() {
    let nodes = [];
    let fullText = "";
    if (!this.document.body) {
      return { nodes, fullText };
    }
    let walker = this.document.createTreeWalker(
      this.document.body,
      this.contentWindow.NodeFilter.SHOW_TEXT
    );
    let node;
    while ((node = walker.nextNode())) {
      if (SKIPPED_ELEMENTS.has(node.parentNode.localName)) {
        continue;
      }
      nodes.push({ node, start: fullText.length });
      fullText += node.data;
    }
    return { nodes, fullText };
  }

  findRangeOffsets(page, range) {
    let start = null;
    let end = null;
    for (let { node, start: nodeStart } of page.nodes) {
      if (!range.intersectsNode(node)) {
        continue;
      }
      let from = node == range.startContainer ? range.startOffset : 0;
      let to = node == range.endContainer ? range.endOffset : node.length;
      if (start == null) {
        start = nodeStart + from;
      }
      end = nodeStart + to;
    }
    if (start == null || start >= end) {
      return null;
    }
    return { start, end };
  }

  countOccurrences(fullText, text, before) {
    let count = 0;
    let position = fullText.indexOf(text);
    while (position != -1 && position < before) {
      count++;
      position = fullText.indexOf(text, position + 1);
    }
    return count;
  }

  findBestMatch(fullText, highlight) {
    if (!highlight.text) {
      return -1;
    }
    let best = -1;
    let bestScore = -1;
    let index = 0;
    let position = fullText.indexOf(highlight.text);
    while (position != -1) {
      let score =
        countMatchBefore(fullText, position, highlight.prefix) +
        countMatchAfter(
          fullText,
          position + highlight.text.length,
          highlight.suffix
        );
      if (index == highlight.occurrence) {
        score += 0.5;
      }
      if (score > bestScore) {
        best = position;
        bestScore = score;
      }
      index++;
      position = fullText.indexOf(highlight.text, position + 1);
    }
    return best;
  }

  wrapOffsets(page, start, end, color) {
    let marks = [];
    for (let { node, start: nodeStart } of page.nodes) {
      let nodeEnd = nodeStart + node.length;
      if (nodeEnd <= start || nodeStart >= end) {
        continue;
      }
      let from = Math.max(start, nodeStart) - nodeStart;
      let to = Math.min(end, nodeEnd) - nodeStart;
      let target = node;
      if (from > 0) {
        target = target.splitText(from);
      }
      if (to - from < target.length) {
        target.splitText(to - from);
      }
      if (!target.data.trim()) {
        continue;
      }
      let mark = this.document.createElement("mark");
      mark.style.backgroundColor = color;
      target.replaceWith(mark);
      mark.append(target);
      marks.push(mark);
    }
    return marks;
  }

  removeMarks(id) {
    let marks = this.document.querySelectorAll(
      `mark[data-highlight-id="${id}"]`
    );
    for (let mark of marks) {
      let parent = mark.parentNode;
      mark.replaceWith(...mark.childNodes);
      parent.normalize();
    }
  }
}
