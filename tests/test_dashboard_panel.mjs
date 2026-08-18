import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const styles = await readFile(new URL("../dashboard_panel.css", import.meta.url), "utf8");

test("dashboard panel ships the layout utilities required by the error dialog", () => {
  for (const selector of [
    ".fixed",
    ".inset-0",
    ".min-h-0",
    ".overflow-auto",
    ".grid-cols-\\[340px_1fr\\]",
    ".w-\\[280px\\]",
  ]) {
    assert.ok(styles.includes(selector), `missing dashboard selector: ${selector}`);
  }
});

test("dashboard stylesheet uses host theme tokens", () => {
  assert.match(styles, /--roxy-color-/);
  assert.doesNotMatch(styles, /--ak-color-/);
});
