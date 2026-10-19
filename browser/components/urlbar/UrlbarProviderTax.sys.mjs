/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

import { XPCOMUtils } from "resource://gre/modules/XPCOMUtils.sys.mjs";

import { UrlbarProvider } from "moz-src:///browser/components/urlbar/UrlbarUtils.sys.mjs";

const lazy = {};

ChromeUtils.defineESModuleGetters(lazy, {
  UrlbarResult: "chrome://browser/content/urlbar/UrlbarResult.mjs",
  UrlbarShared: "chrome://browser/content/urlbar/UrlbarShared.mjs",
});


XPCOMUtils.defineLazyServiceGetter(
  lazy,
  "ClipboardHelper",
  "@mozilla.org/widget/clipboardhelper;1",
  Ci.nsIClipboardHelper
);

const DYNAMIC_RESULT_TYPE = "tax";
const TAX_REGEXP = /^tax\s+(\d+)$/i;


const VIEW_TEMPLATE = {
  attributes: {
    selectable: true,
  },
  children: [
    {
      name: "content",
      tag: "span",
      attributes: { class: "urlbarView-no-wrap" },
      children: [
        {
          name: "icon",
          tag: "img",
          attributes: { class: "urlbarView-favicon" },
        },
        {
          name: "input",
          tag: "strong",
        },
        {
          name: "action",
          tag: "span",
        },
      ],
    },
  ],
};


/**
 * A provider that returns a suggested url to the user based on what
 * they have currently typed so they can navigate directly.
 */
export class UrlbarProviderTax extends UrlbarProvider {
  //A provider that rolls a die when the user types "dice".

  /**
   * @returns {Values<typeof lazy.UrlbarShared.PROVIDER_TYPE>}
   */
  get type() {
    return lazy.UrlbarShared.PROVIDER_TYPE.PROFILE;
  }

  /**
   * Whether this provider should be invoked for the given context.
   * If this method returns false, the providers manager won't start a query
   * with this provider, to save on resources.
   *
   * @param {UrlbarQueryContext} queryContext The query context object
   */
  async isActive(queryContext) {
    return TAX_REGEXP.test(queryContext.trimmedSearchString);
  }



  /**
   * Starts querying.
   *
   * @param {UrlbarQueryContext} queryContext
   * @param {(provider: UrlbarProvider, result: UrlbarResult) => void} addCallback
   *   Callback invoked by the provider to add a new result.
   */
  async startQuery(queryContext, addCallback) {
    let match = queryContext.trimmedSearchString.match(TAX_REGEXP);
    let price = Number(match[1]);
    let value = Math.floor((price * 110) / 100);
    const result = new lazy.UrlbarResult({
      type: lazy.UrlbarShared.RESULT_TYPE.DYNAMIC,
      source: lazy.UrlbarShared.RESULT_SOURCE.OTHER_LOCAL,
      suggestedIndex: 1,
      payload: {
        value: value.toLocaleString(),
        input: queryContext.searchString,
        dynamicType: DYNAMIC_RESULT_TYPE,
      },
    });
    addCallback(this, result);
  }

  getViewTemplate(_result) {
    return VIEW_TEMPLATE;
  }

  getViewUpdate(result) {
    const { value } = result.payload;

    return {
      icon: {
        attributes: {
          src: "chrome://global/skin/icons/edit-copy.svg",
        },
      },
      input: {
        textContent: `税込み ${value}円`,
        attributes: { dir: "ltr" },
      },
      action: {
        l10n: { id: "urlbar-result-action-copy-to-clipboard" },
      },
    };
  }

  /**
   * @param {UrlbarQueryContext} queryContext
   * @param {UrlbarParentController} controller
   * @param {object} details
   */
  onEngagement(queryContext, controller, details) {
    lazy.ClipboardHelper.copyString(details.result.payload.value);
}}

