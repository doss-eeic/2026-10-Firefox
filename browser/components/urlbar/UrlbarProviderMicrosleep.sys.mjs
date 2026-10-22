/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

import { XPCOMUtils } from "resource://gre/modules/XPCOMUtils.sys.mjs";

import { UrlbarProvider } from "moz-src:///browser/components/urlbar/UrlbarUtils.sys.mjs";

const lazy = {};

ChromeUtils.defineESModuleGetters(lazy, {
  clearInterval: "resource://gre/modules/Timer.sys.mjs",
  setInterval: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
  UrlbarPrefs: "moz-src:///browser/components/urlbar/UrlbarPrefs.sys.mjs",
  UrlbarResult: "chrome://browser/content/urlbar/UrlbarResult.mjs",
  UrlbarShared: "chrome://browser/content/urlbar/UrlbarShared.mjs",
});

ChromeUtils.defineLazyGetter(lazy, "l10n", () => {
  return new Localization(["browser/browser.ftl"], true);
});

XPCOMUtils.defineLazyServiceGetter(
  lazy,
  "ClipboardHelper",
  "@mozilla.org/widget/clipboardhelper;1",
  Ci.nsIClipboardHelper
);

// This pref is relative to the `browser.urlbar` branch.
const ENABLED_PREF = "suggest.calculator";

const DYNAMIC_RESULT_TYPE = "calculator";

const VIEW_TEMPLATE = {
  attributes: {
    selectable: true,
  },
  children: [
    {
      name: "content",
      tag: "span",
      children: [
        {
          name: "input",
          tag: "span",
        },
      ],
    },
  ],
};


export class UrlbarProviderMicrosleep extends UrlbarProvider {
  // Caching the sapName is safe because each supported SAP has its own instance
  // of this provider.
  #sapName;

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
    const input = queryContext.trimmedSearchString.toLowerCase().trim();
    const parts = input.split(/\s+/);
    if(parts[0] !== "microsleep" || queryContext.restrictInSearchMode() )
    {
      return false;
    }

    if(parts.length===1)
    {
      return true;
    }
    else if(parts.length===2 && parts[1]==="hori")
    {
      return true;
    }
    else if(parts.length===2 && 
      /^\d+(?:\.\d+)?$/.test(parts[1]) &&
      Number(parts[1])>0
    )
    {
      return true;
    }
    else
    {
      return false;
    }
  }

  /**
   * Starts querying.
   *
   * @param {UrlbarQueryContext} queryContext
   * @param {(provider: UrlbarProvider, result: UrlbarResult) => void} addCallback
   *   Callback invoked by the provider to add a new result.
   */
  async startQuery(queryContext, addCallback) {
    
    this.#sapName = queryContext.sapName;
    try {
      const input = queryContext.trimmedSearchString.toLowerCase().trim();
      const parts = input.split(/\s+/);

      let duration,horimessage;
      if(parts.length===1)
      {
        duration = 15;
      }
      else if(parts.length===2 && parts[1]==="hori")
      {
        duration  = 60*2.7;
        horimessage = "as Daisuke Hori do";
      }
      else if(parts.length===2 && 
        /^\d+(?:\.\d+)?$/.test(parts[1]) &&
        Number(parts[1])>0
      )
      {
        duration = Number(parts[1]);
      }
      else
      {
        return;
      }
      const message = "Urlbar microsleep for " + duration + "s" + (horimessage ? " " + horimessage : "");
      const result = new lazy.UrlbarResult({
        type: lazy.UrlbarShared.RESULT_TYPE.DYNAMIC,
        source: lazy.UrlbarShared.RESULT_SOURCE.OTHER_LOCAL,
        suggestedIndex: 1,
        payload: {
          message:message,
          duration:duration,
          dynamicType:DYNAMIC_RESULT_TYPE,
        },
      });
      addCallback(this, result);
    } catch (e) {}
  }

  getViewTemplate(_result) {
    return VIEW_TEMPLATE;
  }

  getViewUpdate(result) {
    const { message } = result.payload;

    return {
      input: {
        textContent: message,
      },
    };
  }

  /**
   * @param {UrlbarQueryContext} queryContext
   * @param {UrlbarParentController} controller
   * @param {object} details
   */

  onEngagement(queryContext, controller, details) {
    let duration = details.result.payload.duration;
    let addz = 1000;
    let finish = false;
    
    controller.input.setValue("( ˘ω˘ )");
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )z");
    }, addz);
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )zz");
    },addz*2);
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )zzZ");
    },addz*3);

    const timerId = lazy.setInterval(() => {
    controller.input.setValue("( ˘ω˘ )");
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )z");
    }, addz);
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )zz");
    },addz*2);
    lazy.setTimeout(() => {
      if(finish) return;
      controller.input.setValue("( ˘ω˘ )zzZ");
    },addz*3);
  }, addz*4);

  lazy.setTimeout(() => {
      lazy.clearInterval(timerId);
      finish = true;
      controller.input.setValue("");
  }, duration * 1000);
  
  }

  
}
