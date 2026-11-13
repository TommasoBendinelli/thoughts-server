// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
// Port of Android's ReviewMaskGeometryTest (same rules as Thoughts Desktop's
// reviewmaskgeometry.h): the viewer's geometry must give the same blocks.
// Run: node clients/html-viewer/tests/geometry.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
await import(join(here, "..", "geometry.js"));
const G = globalThis.ThoughtsViewerGeometry;

const r = (a) => ({ left: a[0], top: a[1], right: a[2], bottom: a[3] });
const arr = (b) => [b.left, b.top, b.right, b.bottom];
const blocks = (frags, ratio, page) => G.continuousReviewMaskBlocks(frags.map(r), ratio, page ? page.map(r) : []);
function close(actual, expected, eps) {
  actual.forEach((v, i) => assert.ok(Math.abs(v - expected[i]) <= eps, `${actual} != ${expected}`));
}
function numbers(name) {
  return readFileSync(join(here, "fixtures", name), "utf8").trim().split(/\s+/).map(Number);
}
function rects(name) {
  const n = numbers(name), out = [];
  for (let i = 0; i + 3 < n.length; i += 4) out.push([n[i], n[i + 1], n[i + 2], n[i + 3]]);
  return out;
}

const tests = {
  fillsWordAndLineGapsWithoutChangingSourceRegions() {
    const first = [0.1, 0.2, 0.2, 0.21];
    const b = blocks([first, [0.21, 0.2, 0.4, 0.21], [0.1, 0.214, 0.3, 0.224]], 1.4);
    assert.equal(b.length, 1);
    close(arr(b[0]), [0.1, 0.2, 0.4, 0.224], 1e-5);
    assert.equal(first[2], 0.2);
  },
  preservesSeparateParagraphsColumnsAndVisualRegions() {
    assert.equal(blocks([[0.1, 0.2, 0.4, 0.21], [0.1, 0.24, 0.4, 0.25], [0.5, 0.2, 0.8, 0.21], [0.1, 0.4, 0.8, 0.7]], 1.4).length, 4);
  },
  respectsPageAspectRatio() {
    const regions = [[0.1, 0.2, 0.2, 0.21], [0.212, 0.2, 0.3, 0.21]];
    assert.equal(blocks(regions, 1.5).length, 1);
    assert.equal(blocks(regions, 0.7).length, 2);
  },
  realSdpoCardFormsSolidBlocksAndKeepsEveryFragmentCovered() {
    const n = numbers("review-mask-sdpo.txt");
    const pages = new Map();
    for (let i = 0; i + 4 < n.length; i += 5) {
      if (!pages.has(n[i])) pages.set(n[i], []);
      pages.get(n[i]).push([n[i + 1], n[i + 2], n[i + 3], n[i + 4]]);
    }
    assert.deepEqual([...pages.keys()].sort(), [12, 13]);
    for (const [page, frags] of pages) {
      const b = blocks(frags, 842 / 595);
      assert.ok(b.length <= 3, `few solid blocks on page ${page}`);
      for (const f of frags) assert.ok(b.some((x) => x.left <= f[0] && x.top <= f[1] && x.right >= f[2] && x.bottom >= f[3]));
      assert.equal(blocks(frags.slice().reverse(), 842 / 595).length, b.length);
    }
  },
  hidesATableAsOneBlockOverItsRulesAndEmptyCells() {
    const table = [[0.20, 0.30, 0.30, 0.31], [0.50, 0.30, 0.60, 0.31], [0.70, 0.30, 0.80, 0.31],
      [0.20, 0.315, 0.28, 0.325], [0.70, 0.315, 0.78, 0.325], [0.20, 0.33, 0.30, 0.34], [0.50, 0.33, 0.55, 0.34]];
    const b = blocks(table, 1.4);
    assert.equal(b.length, 1);
    assert.ok(b[0].left < 0.2 && b[0].right > 0.8);
    assert.equal(blocks(table.concat([[0.1, 0.6, 0.9, 0.61]]), 1.4).length, 2);
    const page = table.concat([[0.10, 0.25, 0.90, 0.26], [0.10, 0.40, 0.90, 0.41]]);
    const grown = blocks(table, 1.4, page)[0];
    assert.ok(grown.left < 0.10 && grown.right > 0.90);
    assert.ok(grown.top > 0.26 && grown.bottom < 0.40 && grown.bottom > 0.36);
  },
  realOlmoTableIsHiddenUpToTheParagraphsAroundIt() {
    const b = blocks(rects("review-mask-olmo3-table.txt"), 841.89 / 595.276, rects("review-mask-olmo3-page.txt"));
    assert.equal(b.length, 1);
    assert.ok(b[0].left <= 0.1440 && b[0].right >= 0.8549);
    assert.ok(b[0].top > 0.2148 && b[0].top <= 0.2549);
    assert.ok(b[0].bottom >= 0.3640 && b[0].bottom < 0.3918);
  },
  selectionOverSeveralParagraphsIsHiddenAsOneBlock() {
    const b = blocks(MOPD, 1.294, surrounding());
    assert.equal(b.length, 1);
    close(arr(b[0]), [0.1185, 0.4257, 0.8823, 0.8220], 0.0002);
    assert.equal(blocks(MOPD, 1.294).length, 4);
  },
  paragraphsStayApartWhenJoiningWouldHideOtherText() {
    assert.equal(blocks(MOPD, 1.294, surrounding([0.80, 0.476, 0.86, 0.488])).length, 2);
  },
  paragraphsStayApartAcrossATallBlankGap() {
    const regions = [[0.1, 0.20, 0.8, 0.21], [0.1, 0.25, 0.8, 0.26], [0.1, 0.265, 0.8, 0.275]];
    assert.equal(blocks(regions, 1.4, regions).length, 2);
  },
  mergeTextLineRectsJoinsWordsOfOneLine() {
    const runs = G.mergeTextLineRects([r([0, 0, 10, 10]), r([12, 0, 20, 10]), r([60, 0, 70, 10]), r([0, 14, 30, 24])]);
    assert.equal(runs.length, 3);
  }
};

const MOPD = [
  [0.120054, 0.425655, 0.206262, 0.438004], [0.226494, 0.425655, 0.881054, 0.438004], [0.119832, 0.441645, 0.881053, 0.454124],
  [0.119707, 0.457739, 0.684701, 0.470217], [0.119311, 0.494114, 0.281854, 0.506463], [0.302023, 0.494114, 0.882244, 0.506463],
  [0.119676, 0.510104, 0.88224, 0.522583], [0.119832, 0.526198, 0.880753, 0.538676], [0.119707, 0.542304, 0.627799, 0.55477],
  [0.119534, 0.578563, 0.194905, 0.591016], [0.214322, 0.578563, 0.88107, 0.591016], [0.119498, 0.594657, 0.880624, 0.607135],
  [0.119739, 0.61075, 0.880632, 0.623229], [0.119605, 0.626844, 0.882077, 0.639322], [0.119505, 0.642952, 0.465319, 0.655417],
  [0.119523, 0.67921, 0.254801, 0.691662], [0.274492, 0.67921, 0.881724, 0.691662], [0.119822, 0.695316, 0.880162, 0.707781],
  [0.119593, 0.711398, 0.88017, 0.725777], [0.119506, 0.726994, 0.880641, 0.741871], [0.119829, 0.743585, 0.880195, 0.756063],
  [0.120363, 0.759882, 0.125971, 0.762157], [0.11915, 0.759181, 0.881057, 0.774058], [0.119515, 0.777347, 0.618963, 0.789825],
  [0.626658, 0.775209, 0.632175, 0.776219], [0.626567, 0.777551, 0.632175, 0.779825], [0.625354, 0.781146, 0.631086, 0.787083],
  [0.639045, 0.781146, 0.648428, 0.787083], [0.665445, 0.774849, 0.670331, 0.783956], [0.658563, 0.784765, 0.677254, 0.793276],
  [0.685005, 0.777192, 0.881097, 0.790137], [0.118514, 0.793453, 0.880594, 0.805919], [0.119597, 0.809534, 0.407537, 0.822013]
];
function surrounding(...others) {
  return MOPD.concat([[0.12, 0.395, 0.35, 0.41], [0.12, 0.835, 0.30, 0.85]], others);
}

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log("ok   geometry." + name); }
  catch (e) { failed++; console.log("FAIL geometry." + name + ": " + e.message); }
}
if (failed) { console.log(failed + " geometry test(s) failed"); process.exit(1); }
console.log("geometry: all " + Object.keys(tests).length + " passed");
