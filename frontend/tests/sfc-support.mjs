import { readFileSync } from "node:fs";

import { JSDOM } from "jsdom";
import ts from "typescript";
import { compileScript, parse } from "vue/compiler-sfc";

export const dom = new JSDOM("<!doctype html><body></body>", {
  url: "http://localhost/",
});
// JSDOM has no native top-layer dialog implementation; exercise the real SFC lifecycle.
dom.window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
dom.window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
for (const key of [
  "window",
  "document",
  "history",
  "location",
  "Element",
  "SVGElement",
  "HTMLElement",
]) {
  globalThis[key] = key === "window" ? dom.window : dom.window[key];
}
export const vue = await import("vue");
export const vueRouter = await import("vue-router");

// Compile the actual SFCs; only the auth transport is replaced in these UI tests.
export function component(file, session, extra = {}) {
  const sourceFile = new URL(file, import.meta.url);
  const source = readFileSync(sourceFile, "utf8");
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: file, inlineTemplate: true });
  const { outputText } = ts.transpileModule(script.content, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  });
  const exports = {};
  const dependencies = { vue, "vue-router": vueRouter, ...extra };
  new Function("require", "exports", outputText)((name) => {
    if (name.endsWith("auth/session")) return session;
    if (dependencies[name]) return dependencies[name];
    if (name.endsWith(".vue"))
      return {
        default: component(new URL(name, sourceFile).href, session, extra),
      };
    throw new Error(`Unexpected SFC dependency: ${name}`);
  }, exports);
  return exports.default;
}

export const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setImmediate(resolve));
    await vue.nextTick();
  }
};
