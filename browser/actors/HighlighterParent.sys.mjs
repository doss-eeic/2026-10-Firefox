/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

import { HighlighterStore } from "moz-src:///browser/components/highlighter/HighlighterStore.sys.mjs";

export class HighlighterParent extends JSWindowActorParent {
  async receiveMessage(message) {
    let url = this.manager.documentURI.spec;
    if (message.name == "Highlighter:Save") {
      await HighlighterStore.add(url, message.data.text, message.data.color);
    } else if (message.name == "Highlighter:GetHighlights") {
      return HighlighterStore.getByUrl(url);
    }
    return null;
  }
}

