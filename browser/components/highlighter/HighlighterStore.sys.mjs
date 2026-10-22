/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

import { Sqlite } from "resource://gre/modules/Sqlite.sys.mjs";

let gConnectionPromise = null;

function getConnection() {
  if (!gConnectionPromise) {
    gConnectionPromise = openConnection();
  }
  return gConnectionPromise;
}

async function openConnection() {
  let path = PathUtils.join(PathUtils.profileDir, "highlights.sqlite");
  let connection = await Sqlite.openConnection({ path });
  Sqlite.shutdown.addBlocker("Highlighter: closing database", () =>
    connection.close()
  );
  await connection.execute(`
    CREATE TABLE IF NOT EXISTS highlights (
      id INTEGER PRIMARY KEY,
      url TEXT NOT NULL,
      text TEXT NOT NULL,
      color TEXT,
      created INTEGER NOT NULL
    )
  `);
  if ((await connection.getSchemaVersion()) < 1) {
    await connection.executeTransaction(async () => {
      await connection.execute(
        "ALTER TABLE highlights ADD COLUMN prefix TEXT NOT NULL DEFAULT ''"
      );
      await connection.execute(
        "ALTER TABLE highlights ADD COLUMN suffix TEXT NOT NULL DEFAULT ''"
      );
      await connection.execute(
        "ALTER TABLE highlights ADD COLUMN occurrence INTEGER NOT NULL DEFAULT 0"
      );
      await connection.setSchemaVersion(1);
    });
  }
  return connection;
}

export const HighlighterStore = {
  async add(url, { text, prefix, suffix, occurrence, color }) {
    let connection = await getConnection();
    let rows = await connection.execute(
      `INSERT INTO highlights (url, text, prefix, suffix, occurrence, color, created)
       VALUES (:url, :text, :prefix, :suffix, :occurrence, :color, :created)
       RETURNING id`,
      { url, text, prefix, suffix, occurrence, color, created: Date.now() }
    );
    Services.obs.notifyObservers(null, "highlighter-changed");
    return rows[0].getResultByName("id");
  },

  async getByUrl(url) {
    let connection = await getConnection();
    let rows = await connection.execute(
      `SELECT id, text, prefix, suffix, occurrence, color
       FROM highlights WHERE url = :url`,
      { url }
    );
    return rows.map(row => ({
      id: row.getResultByName("id"),
      text: row.getResultByName("text"),
      prefix: row.getResultByName("prefix"),
      suffix: row.getResultByName("suffix"),
      occurrence: row.getResultByName("occurrence"),
      color: row.getResultByName("color"),
    }));
  },

  async getAll() {
    let connection = await getConnection();
    let rows = await connection.execute(
      "SELECT id, url, text, color, created FROM highlights ORDER BY created DESC"
    );
    return rows.map(row => ({
      id: row.getResultByName("id"),
      url: row.getResultByName("url"),
      text: row.getResultByName("text"),
      color: row.getResultByName("color"),
      created: row.getResultByName("created"),
    }));
  },

  async delete(id, url) {
    let connection = await getConnection();
    await connection.execute(
      "DELETE FROM highlights WHERE id = :id AND url = :url",
      { id, url }
    );
    Services.obs.notifyObservers(null, "highlighter-changed");
  },
};
