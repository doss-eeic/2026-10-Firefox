/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

export class HighlighterChild extends JSWindowActorChild {
  receiveMessage(message) {
    if (message.name == "Highlighter:Highlight") {
      let selection = this.contentWindow.getSelection();
      let text = selection.toString();
      this.wrapRange(selection.getRangeAt(0), message.data.color);
      this.sendAsyncMessage("Highlighter:Save", {
        text,
        color: message.data.color,
      });
    }
  }

  async handleEvent(event) {
    if (event.type == "DOMContentLoaded") {
      let highlights = await this.sendQuery("Highlighter:GetHighlights");
      for (let highlight of highlights) {
        let range = this.findText(highlight.text);
        if (range) {
          this.wrapRange(range, highlight.color);
        }
      }
    }
  }

  wrapRange(range, color) {
    let mark = this.document.createElement("mark");
    mark.style.backgroundColor = color;
    range.surroundContents(mark);
  }

  findText(text) {
    if (!this.document.body) {
      return null;
    }
    let walker = this.document.createTreeWalker(
      this.document.body,
      this.contentWindow.NodeFilter.SHOW_TEXT
    );
    let node;
    while ((node = walker.nextNode())) {
      let index = node.data.indexOf(text);
      if (index != -1) {
        let range = this.document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + text.length);
        return range;
      }
    }
    return null;
  }
}

