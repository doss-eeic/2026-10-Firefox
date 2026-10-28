/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

const BrowserTerminalCommandDispatcher = {
  parse_part(input) {
    const trimmedInput = input.trim();
    if (!trimmedInput) {
      return null;
    }
    const commands = trimmedInput
      .split("|")
      .map(cmd => cmd.trim())
      .filter(cmd => !!cmd.length);
    return {
      input: trimmedInput,
      commands,
    };
  },

  parse_single(input) {
    const trimmedInput = input.trim();
    if (!trimmedInput) {
      return null;
    }
    const [name, ...args] = trimmedInput.split(/\s+/);
    return {
      input: trimmedInput,
      name: name.toLowerCase(),
      args,
    };
  },
};

const BrowserTerminalCommands = {
  echo: "executeEcho",
  help: "executeHelp",
  clear: "executeClear",
  url: "executeUrl",
  title: "executeTitle",
  scroll: "executeScroll",
  checkchar: "executeCheckChar",
  back: "executeBack",
  forward: "executeForward",
  reload: "executeReload",
  tab: "executeTab",
};

var BrowserTerminal = {
  _initialized: false,
  _didShowWelcomeMessage: false,
  _commandHistory: [],
  _historyIndex: null,
  _historyInputBeforeNavigation: "",
  _isScrollMode: false,

  get panel() {
    return document.getElementById("browser-terminal-panel");
  },

  get splitter() {
    return document.getElementById("browser-terminal-splitter");
  },

  get output() {
    return document.getElementById("browser-terminal-output");
  },

  get input() {
    return document.getElementById("browser-terminal-input");
  },

  init() {
    if (this._initialized) {
      return;
    }

    this.input.addEventListener("keydown", this);
    this._initialized = true;
  },

  handleEvent(event) {
    if (event.type !== "keydown") {
      return;
    }

    if (this._isScrollMode) {
      if (event.key === "ArrowUp") {
        event.preventDefault();
        this.scrollSelectedTabByLine(-1);
        return;
      }

      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.scrollSelectedTabByLine(1);
        return;
      }

      if (event.key.toLowerCase() === "q") {
        event.preventDefault();
        this._isScrollMode = false;
        return;
      }
    }

    if (event.key === "ArrowUp") {
      if (this.navigateHistory(-1)) {
        event.preventDefault();
      }
      return;
    }

    if (event.key === "ArrowDown") {
      if (this.navigateHistory(1)) {
        event.preventDefault();
      }
      return;
    }

    if (event.key !== "Enter") {
      return;
    }

    event.preventDefault();

    let command = this.input.value;
    if (!command.trim()) {
      return;
    }

    this.addCommandToHistory(command);
    this.resetHistoryNavigation();

    this.input.value = "";
    this.executeCommand(command).catch(console.error);
  },

  addCommandToHistory(command) {
    const normalizedCommand = command.trim();
    if (!normalizedCommand) {
      return;
    }

    const lastCommand = this._commandHistory[this._commandHistory.length - 1];
    if (lastCommand === normalizedCommand) {
      return;
    }

    this._commandHistory.push(normalizedCommand);
  },

  resetHistoryNavigation() {
    this._historyIndex = null;
    this._historyInputBeforeNavigation = "";
  },

  navigateHistory(direction) {
    if (!this._commandHistory.length) {
      return false;
    }

    const historyLength = this._commandHistory.length;
    if (this._historyIndex === null) {
      if (direction > 0) {
        return false;
      }

      this._historyInputBeforeNavigation = this.input.value;
      this._historyIndex = historyLength;
    }

    const nextIndex = Math.min(
      historyLength,
      Math.max(0, this._historyIndex + direction)
    );
    this._historyIndex = nextIndex;

    if (nextIndex === historyLength) {
      this.input.value = this._historyInputBeforeNavigation;
      return true;
    }

    this.input.value = this._commandHistory[nextIndex];
    return true;
  },

  toggle() {
    if (this.panel.collapsed) {
      this.open();
      return;
    }

    this.close();
  },

  open() {
    this.init();
    this.panel.collapsed = false;
    this.splitter.collapsed = false;

    if (!this._didShowWelcomeMessage) {
      this._didShowWelcomeMessage = true;
      this.appendLocalizedOutput("browser-terminal-output-welcome").catch(
        console.error
      );
    }

    this.input.focus();
  },

  close() {
    this.panel.collapsed = true;
    this.splitter.collapsed = true;
  },

  async executeCommand(rawInput) {
    const parsedCommand = BrowserTerminalCommandDispatcher.parse_part(rawInput);
    if (!parsedCommand) {
      return;
    }

    this.appendOutput(`> ${parsedCommand.input}`, "command");
    const result = await this.executePipeline(parsedCommand.commands);

    this.focusInput();

    if (result !== undefined && result !== null) {
      await this.appendOutput(String(result));
    }
  },

  async executePipeline(commands) {
    let output;
    for (const command of commands) {
      const parsedCommand =
        BrowserTerminalCommandDispatcher.parse_single(command);
      if (!parsedCommand) {
        continue;
      }
      output = await this.executeSingleCommand(parsedCommand, output);
      if (output === null) {
        return null;
      }
    }
    return output;
  },
  async executeSingleCommand(command, output) {
    const methodName = BrowserTerminalCommands[command.name];
    if (methodName && typeof this[methodName] === "function") {
      return await this[methodName](command.args, output);
    }
    await this.appendLocalizedOutput(
      "browser-terminal-output-unknown",
      {
        command: command.name,
      },
      "error"
    );
    return null;
  },

  focusInput() {
    if (this.panel.collapsed) {
      return;
    }
    requestAnimationFrame(() => {
      setTimeout(() => {
        if (!this.panel.collapsed) {
          this.input.focus();
        }
      }, 0);
    });
  },

  //-------------------------------- Command 関数群 -------------------------------//
  async executeClear(_args, _output) {
    this.clearOutput();
  },
  async executeUrl(_args, _output) {
    return gBrowser.selectedBrowser.currentURI.spec;
  },
  async executeTitle(_args, _output) {
    let title =
      gBrowser.selectedBrowser.contentTitle || gBrowser.selectedTab.label || "";
    return title;
  },
  async executeEcho(args, output) {
    if(output==null)return args.join(" ");
    return output;
  },
  async executeHelp(_args, _output) {
  return await document.l10n.formatValue(
    "browser-terminal-output-help"
  );
},
  async executeCheckChar(args, output) {
    const options = args.filter(arg => arg.startsWith("-"));
    const input =
      output == null
        ? args.filter(arg => !arg.startsWith("-")).join(" ")
        : output;

    if (input.length === 0) {
      await this.appendLocalizedOutput(
        "browser-terminal-output-checkchar-noinput",
        {},
        "error"
      );
      return null;
    }

    const charPatterns = {
      en: "a-zA-Z0-9",
      letters: "\\p{L}",
      numbers: "\\p{N}",
      symbols: "!@#$%^&*()_+=\\-.:/?\\[\\]~'&,;%",
      url: "a-zA-Z0-9._~:/?#\\[\\]@!$&'()*+,;=%\\-",
      // 文字体系
      ja: "\\u3000-\\u303F\\u3040-\\u30FF\\u3400-\\u4DBF\\u4E00-\\u9FFF",
      cyr: "\\u0400-\\u04FF\\u0500-\\u052F",
      gr: "\\u0370-\\u03FF",
      ara: "\\u0600-\\u06FF",
      kr: "\\uAC00-\\uD7AF",
      tha: "\\u0E00-\\u0E7F",
      heb: "\\u0590-\\u05FF",
    };

    const front = "^[",
      back = "]+$";
    if (!options.length) {
      await this.appendLocalizedOutput(
        "browser-terminal-output-checkchar-nooption",
        {},
        "error"
      );
      return null;
    }

    const hasUrlOption = options.includes("-url");
    let finalRegex = front + (hasUrlOption ? "" : " ");
    for (const option of options) {
      const key = option.slice(1);
      if (Object.hasOwn(charPatterns, key)) {
        finalRegex += charPatterns[key];
      } else if (!Object.hasOwn(charPatterns, key)) {
        await this.appendLocalizedOutput(
          "browser-terminal-output-checkchar-invalidoption",
          {
            option,
          },
          "error"
        );
        return null;
      }
    }

    finalRegex += back;
    const allowed = new RegExp(finalRegex, "u");
    const isValid = allowed.test(input);

    if (isValid) {
      return input;
    }

    await this.appendLocalizedOutput(
      "browser-terminal-output-checkchar-invalid",
      {
        input,
      },
      "error"
    );
    return null;
  },
  async executeBack(_args, _output) {
    gBrowser.selectedBrowser.goBack();
  },
  async executeForward(_args, _output) {
    gBrowser.selectedBrowser.goForward();
  },
  async executeReload(_args, _output) {
    gBrowser.selectedBrowser.reload();
  },
  async executeScroll(args, _output) {
    if (!args.length) {
      this._isScrollMode = true;
      await this.appendLocalizedOutput(
        "browser-terminal-output-scroll-activated",
        {},
        "info"
      );
      return;
    }

    if (args.length !== 1) {
      await this.appendLocalizedOutput(
        "browser-terminal-output-scroll-invalid",
        {},
        "error"
      );
      return null;
    }

    if (args[0] === "top" || args[0] === "-t") {
      this.scrollSelectedTab("cmd_scrollTop");
      return;
    }

    if (args[0] === "bottom" || args[0] === "-b") {
      this.scrollSelectedTab("cmd_scrollBottom");
      return;
    }

    await this.appendLocalizedOutput(
      "browser-terminal-output-scroll-invalid",
      {},
      "error"
    );
    return null;
  },
  async executeTab(args, output) {
    const subcommand = {
      l: "executeTabList",
      rm: "executeTabClose",
      n: "executeTabNew",
      ns: "executeTabNewSelect",
      sl: "executeTabSelect",
    };
    const option = args.filter(arg => arg.startsWith("-"));
    if (option.length !== 1) {
      await this.appendLocalizedOutput(
        "browser-terminal-output-tab-invalidoption",
        {},
        "error"
      );
      return null;
    } else if (!Object.hasOwn(subcommand, option[0].slice(1))) {
      await this.appendLocalizedOutput(
        "browser-terminal-output-tab-invalidoption",
        { option: option[0] },
        "error"
      );
      return null;
    }

    const methodName = subcommand[option[0].slice(1)];

    if (typeof this[methodName] !== "function") {
      console.error(`Tab command method not found: ${methodName}`);
      return null;
    }
    const subargs = args.filter(arg => !arg.startsWith("-"));
    return await this[methodName](subargs, output);
  },
  //--command 関数群終わり--//
  clearOutput() {
    this.output.replaceChildren();
  },
  executeTabList(_args, _output) {
    const tabs = gBrowser.tabs;

    for (const tab of tabs) {
      const index = tabs.indexOf(tab) + 1;
      const url = tab.linkedBrowser.currentURI.spec;
      const selected = tab === gBrowser.selectedTab ? "*" : " ";

      this.appendOutput(
        `${selected} Tab ${index}: ${tab.label}\n  url: ${url}`
      );
    }
  },
  executeTabClose(args, _output) {
    if (args.length === 0) {
      gBrowser.removeTab(gBrowser.selectedTab);
      return;
    }

    if (args.length === 1 && args[0] === "all") {
      const tabsToClose = Array.from(gBrowser.tabs);
      gBrowser.removeTabs(tabsToClose);
      return;
    }

    const tabs = gBrowser.tabs;
    const tabsToClose = [];
    const seenIndices = new Set();

    for (const arg of args) {
      if (!/^[1-9]\d*$/.test(arg)) {
        this.appendLocalizedOutput(
          "browser-terminal-output-tab-invalidindex",
          { index: arg },
          "error"
        ).catch(console.error);
        continue;
      }

      const index = Number(arg) - 1;
      if (index < 0 || index >= tabs.length) {
        this.appendLocalizedOutput(
          "browser-terminal-output-tab-invalidindex",
          { index: arg },
          "error"
        ).catch(console.error);
        continue;
      }

      if (seenIndices.has(index)) {
        continue;
      }

      seenIndices.add(index);
      tabsToClose.push(tabs[index]);
    }

    if (!tabsToClose.length) {
      return;
    }

    if (tabsToClose.length === 1) {
      gBrowser.removeTab(tabsToClose[0]);
      return;
    }

    gBrowser.removeTabs(tabsToClose);
  },
  executeTabNew(args, output) {
    if (output !== undefined && output !== null && output !== "") {
      openWebLinkIn(output, "tab", { inBackground: true });
      return;
    }

    if (args.length === 0) {
      openTrustedLinkIn(BROWSER_NEW_TAB_URL, "tab", { inBackground: true });
      return;
    }

    for (const url of args) {
      openWebLinkIn(url, "tab", { inBackground: true });
    }
  },
  executeTabNewSelect(args, output) {
    if (output !== undefined && output !== null && output !== "") {
      openWebLinkIn(output, "tab", { inBackground: false });
      return;
    }

    if (args.length === 0) {
      openTrustedLinkIn(BROWSER_NEW_TAB_URL, "tab", { inBackground: false });
      return;
    }

    for (const url of args) {
      openWebLinkIn(url, "tab", { inBackground: false });
    }
  },
  executeTabSelect(args, _output) {
    if (args.length === 0) {
      this.appendLocalizedOutput(
        "browser-terminal-output-tab-select-noindex",
        {},
        "error"
      ).catch(console.error);
    } else if (args.length > 1) {
      this.appendLocalizedOutput(
        "browser-terminal-output-tab-select-multiple",
        {},
        "error"
      ).catch(console.error);
    } else {
      const arg = args[0];
      if (/^[1-9]\d*$/.test(arg)) {
        const index = Number(arg) - 1;
        if (index >= 0 && index < gBrowser.tabs.length) {
          gBrowser.selectedTab = gBrowser.tabs[index];
          this.focusInput();
        } else {
          this.appendLocalizedOutput(
            "browser-terminal-output-tab-invalidindex",
            { index: arg },
            "error"
          ).catch(console.error);
        }
      } else {
        this.appendLocalizedOutput(
          "browser-terminal-output-tab-invalidindex",
          { index: arg },
          "error"
        ).catch(console.error);
      }
    }
  },

  scrollSelectedTabByLine(direction) {
    const command = direction < 0 ? "cmd_scrollLineUp" : "cmd_scrollLineDown";
    this.scrollSelectedTab(command);
  },

  scrollSelectedTab(command) {
    gBrowser.selectedBrowser.focus();
    goDoCommand(command);
    this.input.focus();
  },

  appendOutput(text, type = "output") {
    const line = document.createElementNS(
      "http://www.w3.org/1999/xhtml",
      "div"
    );
    line.classList.add(
      "browser-terminal-line",
      `browser-terminal-line-${type}`
    );
    line.textContent = text;
    this.output.append(line);
    this.output.scrollTop = this.output.scrollHeight;
  },

  async appendLocalizedOutput(l10nId, args = undefined, type = "output") {
    const text = await document.l10n.formatValue(l10nId, args);
    this.appendOutput(text, type);
  },
};
