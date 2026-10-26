/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

import { HighlighterStore } from "moz-src:///browser/components/highlighter/HighlighterStore.sys.mjs";

export class HighlighterParent extends JSWindowActorParent {
  async receiveMessage(message) {
    let url = this.manager.documentURI.spec;
    if (message.name == "Highlighter:Save") {
      return HighlighterStore.add(url, message.data);
    } else if (message.name == "Highlighter:GetHighlights") {
      return HighlighterStore.getByUrl(url);
    }
    return null;
  }

  async removeHighlight(id) {
    let url = this.manager.documentURI.spec;
    await HighlighterStore.delete(id, url);
    this.sendAsyncMessage("Highlighter:Remove", { id });
  }

  async editMemo(id) {
    let url = this.manager.documentURI.spec;
    let highlight = await HighlighterStore.get(id, url);
    if (!highlight) {
      return;
    }
    let result = await Services.prompt.asyncPrompt(
      this.browsingContext.top,
      Ci.nsIPromptService.MODAL_TYPE_TAB,
      "メモを編集",
      highlight.text,
      highlight.memo,
      null,
      false
    );
    if (result.getProperty("ok")) {
      await HighlighterStore.updateMemo(id, url, result.getProperty("value"));
    }
  }
}
