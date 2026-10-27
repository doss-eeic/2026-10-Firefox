/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

async function checkTerminalPanel() {
  const win = await BrowserTestUtils.openNewBrowserWindow();
  registerCleanupFunction(() => BrowserTestUtils.closeWindow(win));
  await SimpleTest.promiseFocus(win);
  const doc = win.document;
  await doc.l10n.ready;

  const panel = doc.getElementById("browser-terminal-panel");
  await doc.l10n.translateFragment(panel);
  const input = doc.getElementById("browser-terminal-input");
  const output = doc.getElementById("browser-terminal-output");
  const close = doc.getElementById("browser-terminal-close-button");
  Assert.ok(input, "The input survives localization of the panel");
  Assert.ok(output, "The output survives localization of the panel");
  Assert.ok(close, "The close button survives localization of the panel");
  Assert.ok(panel.collapsed, "The terminal starts closed");

  async function toggleFromMenu() {
    const shown = BrowserTestUtils.waitForEvent(
      win.PanelUI.panel,
      "popupshown"
    );
    win.PanelUI.show();
    await shown;
    info("The application menu is shown");
    const button = doc.getElementById("appMenu-new-terminal-button");
    button.scrollIntoView();
    const hidden = BrowserTestUtils.waitForEvent(
      win.PanelUI.panel,
      "popuphidden"
    );
    EventUtils.synthesizeMouseAtCenter(button, {}, win);
    info(`After clicking Terminal: collapsed=${panel.collapsed}`);
    await hidden;
  }

  await toggleFromMenu();
  Assert.ok(!panel.collapsed, "The application menu opens the terminal");
  Assert.greater(
    panel.getBoundingClientRect().height,
    0,
    "The panel has height"
  );
  Assert.greaterOrEqual(
    panel.getBoundingClientRect().top,
    doc.getElementById("browser").getBoundingClientRect().bottom,
    "The terminal is below the browser area"
  );
  Assert.equal(doc.activeElement, input, "The command input receives focus");
  const inputRect = input.getBoundingClientRect();
  Assert.greater(inputRect.width, 0, "The input has width");
  Assert.greater(inputRect.height, 0, "The input has height");
  Assert.greaterOrEqual(
    inputRect.top,
    0,
    "The input is below the viewport top"
  );
  Assert.lessOrEqual(
    inputRect.bottom,
    win.innerHeight,
    "The input is above the viewport bottom"
  );
  Assert.equal(
    doc.elementFromPoint(
      inputRect.x + inputRect.width / 2,
      inputRect.y + inputRect.height / 2
    ),
    input,
    "The input is visible and is not covered by another element"
  );

  await TestUtils.waitForCondition(() => output.childElementCount > 0);
  input.value = "help";
  EventUtils.synthesizeKey("KEY_Enter", {}, win);
  const help = await doc.l10n.formatValue("browser-terminal-output-help");
  await TestUtils.waitForCondition(() => output.textContent.includes(help));
  Assert.equal(input.value, "", "Enter submits and clears the input");

  await toggleFromMenu();
  Assert.ok(panel.collapsed, "The application menu closes the terminal");
  await toggleFromMenu();
  EventUtils.synthesizeMouseAtCenter(close, {}, win);
  Assert.ok(panel.collapsed, "The close button closes the terminal");
}

add_task(async function terminal_survives_localization_and_opens_from_menu() {
  await checkTerminalPanel();
});

add_task(async function terminal_with_sidebar_revamp() {
  await SpecialPowers.pushPrefEnv({
    set: [
      ["sidebar.revamp", true],
      ["sidebar.verticalTabs", false],
    ],
  });
  await checkTerminalPanel();
  await SpecialPowers.popPrefEnv();
});

add_task(async function terminal_with_vertical_tabs() {
  await SpecialPowers.pushPrefEnv({
    set: [
      ["sidebar.revamp", true],
      ["sidebar.verticalTabs", true],
    ],
  });
  await checkTerminalPanel();
  await SpecialPowers.popPrefEnv();
});
