/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

export class HighlighterChild extends JSWindowActorChild {
  receiveMessage(message) {
    if (message.name == "Highlighter:Highlight") {
      let selection = this.contentWindow.getSelection();
      let range = selection.getRangeAt(0);
      let mark = this.document.createElement("mark");
      mark.style.backgroundColor = message.data.color;
      range.surroundContents(mark);
    }
  }
}
