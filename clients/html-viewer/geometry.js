// Thoughts HTML viewer: review mask geometry.
//
// A port of Thoughts Desktop part/reviewmaskgeometry.h + gui/textlinegeometry.h
// (and Android's ReviewMaskGeometry.java, which implements the same rules), so an
// answer is hidden by the same blocks in the HTML view as in the PDF view.
//
// Rectangles are plain objects {left, top, right, bottom}. The PDF code works in
// page-normalized coordinates (x / page width, y / page height). The viewer passes
// coordinates divided by one reference width on BOTH axes (the text column width),
// so `pageHeightOverWidth` is 1 and the constants that are fractions of the page
// width (0.005, 0.012, 0.015) keep their meaning; every vertical rule is a ratio of
// line heights and does not depend on the unit.
//
// Strict-mode script without import/export: it can be injected with
// runJavaScript/evaluateJavascript (CSP script-src 'none') and also loaded as a
// side-effect ES module. It defines globalThis.ThoughtsViewerGeometry.
(function (root) {
  "use strict";

  function rect(left, top, right, bottom) {
    return { left: left, top: top, right: right, bottom: bottom };
  }
  function width(r) { return r.right - r.left; }
  function height(r) { return r.bottom - r.top; }
  function centerY(r) { return (r.top + r.bottom) / 2; }
  function valid(r) {
    return r && isFinite(r.left) && isFinite(r.top) && isFinite(r.right) && isFinite(r.bottom) &&
      width(r) > 0 && height(r) > 0;
  }
  function unite(a, b) {
    return rect(Math.min(a.left, b.left), Math.min(a.top, b.top), Math.max(a.right, b.right), Math.max(a.bottom, b.bottom));
  }

  // textlinegeometry.h mergeTextLineRects (horizontal): fragments of one visual line
  // become runs; a gap wider than a line is tall starts a new run.
  function mergeTextLineRects(input) {
    const rects = input.filter(valid).map(function (r) { return rect(r.left, r.top, r.right, r.bottom); });
    rects.sort(function (a, b) {
      if (centerY(a) !== centerY(b)) { return centerY(a) - centerY(b); }
      return a.left - b.left;
    });
    const merged = [];
    let first = 0;
    while (first < rects.length) {
      let end = first + 1;
      while (end < rects.length &&
             Math.max(height(rects[first]), height(rects[end])) <= 3 * Math.min(height(rects[first]), height(rects[end])) &&
             Math.abs(centerY(rects[first]) - centerY(rects[end])) <= Math.min(height(rects[first]), height(rects[end])) * 0.65) {
        ++end;
      }
      const line = rects.slice(first, end).sort(function (a, b) { return a.left - b.left; });
      let run = line[0];
      let lastHeight = height(run);
      for (let i = 1; i < line.length; ++i) {
        const fragment = line[i];
        // A normal space is shorter than a line is tall. Use original fragment
        // heights so growing bounds cannot bridge a distant region.
        if (fragment.left - run.right <= Math.max(lastHeight, height(fragment))) {
          run = unite(run, fragment);
        } else {
          merged.push(run);
          run = fragment;
        }
        lastHeight = height(fragment);
      }
      merged.push(run);
      first = end;
    }
    return merged;
  }

  // A table's columns line up: two rows split at horizontally overlapping gaps.
  // A paragraph's run-in heading or an inline formula also splits a row, but not
  // in line with another row.
  function alignedReviewMaskColumns(rows) {
    const gaps = [];
    for (let r = 0; r < rows.length; ++r) {
      const row = rows[r].slice().sort(function (a, b) { return a.left - b.left; });
      let right = row[0].right;
      for (let i = 1; i < row.length; ++i) {
        if (row[i].left > right) { gaps.push({ left: right, right: row[i].left, row: r }); }
        right = Math.max(right, row[i].right);
      }
    }
    for (let i = 0; i < gaps.length; ++i) {
      for (let j = 0; j < i; ++j) {
        if (gaps[i].row !== gaps[j].row && Math.max(gaps[i].left, gaps[j].left) < Math.min(gaps[i].right, gaps[j].right)) { return true; }
      }
    }
    return false;
  }

  function median(values) {
    const sorted = values.slice().sort(function (a, b) { return a - b; });
    return sorted[Math.floor(sorted.length / 2)];
  }

  // One selection over several paragraphs is one answer: join its blocks across
  // the blank space between them, as long as their enclosing rectangle hides no
  // page text outside the answer. A gap taller than three lines (a figure, a
  // display) or text beside the gap keeps them apart.
  function joinReviewMaskBlocksAcrossBlankGaps(blocksIn, fragments, pageWords) {
    const blocks = blocksIn.slice();
    if (!pageWords || pageWords.length === 0 || blocks.length < 2 || fragments.length === 0) { return blocks; }
    const lineHeight = median(fragments.map(height));
    function hidesOtherText(left, top, right, bottom) {
      const dx = 0.005, dy = 0.25 * lineHeight;
      for (let k = 0; k < pageWords.length; ++k) {
        const w = pageWords[k];
        const x = (w.left + w.right) / 2, y = (w.top + w.bottom) / 2;
        if (x <= left || x >= right || y <= top || y >= bottom) { continue; }
        let answer = false;
        for (let f = 0; f < fragments.length && !answer; ++f) {
          const fr = fragments[f];
          answer = x >= fr.left - dx && x <= fr.right + dx && y >= fr.top - dy && y <= fr.bottom + dy;
        }
        if (!answer) { return true; }
      }
      return false;
    }
    for (let changed = true; changed;) {
      changed = false;
      for (let i = 0; i < blocks.length && !changed; ++i) {
        for (let j = i + 1; j < blocks.length && !changed; ++j) {
          const a = blocks[i], b = blocks[j];
          const xGap = Math.max(a.left, b.left) - Math.min(a.right, b.right);
          const yGap = Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
          if (xGap > 0 || yGap > 3 * lineHeight) { continue; }
          const joined = unite(a, b);
          if (hidesOtherText(joined.left, joined.top, joined.right, joined.bottom)) { continue; }
          blocks[i] = joined;
          blocks.splice(j, 1);
          changed = true;
        }
      }
    }
    return blocks;
  }

  function unionFind(n) {
    const parent = [];
    for (let i = 0; i < n; ++i) { parent.push(i); }
    function root(i) {
      while (parent[i] !== i) {
        parent[i] = parent[parent[i]];
        i = parent[i];
      }
      return i;
    }
    return { parent: parent, root: root };
  }

  // reviewmaskgeometry.h continuousReviewMaskBlocks. `words` are the answer's text
  // fragments, `pageWords` every word near the answer (the "page"), `bounds` the
  // largest right/bottom a block may reach (the PDF clamps to the page, 0..1).
  function continuousReviewMaskBlocks(words, pageHeightOverWidth, pageWords, bounds) {
    pageWords = pageWords || [];
    const ratio = pageHeightOverWidth || 1;
    const maxRight = bounds && isFinite(bounds.right) ? bounds.right : 1;
    const maxBottom = bounds && isFinite(bounds.bottom) ? bounds.bottom : 1;
    // normalizedTextLineRects: merge in physical units, then back.
    let fragments = mergeTextLineRects(words.map(function (r) {
      return rect(r.left, r.top * ratio, r.right, r.bottom * ratio);
    })).map(function (r) { return rect(r.left, r.top / ratio, r.right, r.bottom / ratio); });

    // A table answer (several rows, some split into columns) is hidden as one
    // block: covering only its words leaves the rules, column gaps and empty
    // cells readable. Pad it so rules just outside the text are covered too.
    const blocks = [];
    {
      const cluster = unionFind(fragments.length);
      for (let i = 0; i < fragments.length; ++i) {
        for (let j = 0; j < i; ++j) {
          const a = fragments[i], b = fragments[j];
          const h = Math.min(height(a), height(b));
          const yGap = Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
          if (yGap <= 1.2 * h) { cluster.parent[cluster.root(i)] = cluster.root(j); }
        }
      }
      const members = new Map();
      for (let i = 0; i < fragments.length; ++i) {
        const key = cluster.root(i);
        if (!members.has(key)) { members.set(key, []); }
        members.get(key).push(i);
      }
      const keys = Array.from(members.keys()).sort(function (a, b) { return a - b; });
      const remaining = [];
      keys.forEach(function (key) {
        const group = members.get(key);
        // Rows are printed lines: fragments that overlap vertically, such as the
        // stacked parts of an inline fraction, belong to one row.
        const bands = [];
        const rows = [];
        group.forEach(function (index) {
          bands.push([fragments[index].top, fragments[index].bottom]);
          rows.push([fragments[index]]);
        });
        for (let merged = true; merged;) {
          merged = false;
          for (let a = 0; a < bands.length && !merged; ++a) {
            for (let b = a + 1; b < bands.length && !merged; ++b) {
              if (bands[a][0] < bands[b][1] && bands[b][0] < bands[a][1]) {
                bands[a] = [Math.min(bands[a][0], bands[b][0]), Math.max(bands[a][1], bands[b][1])];
                rows[a] = rows[a].concat(rows.splice(b, 1)[0]);
                bands.splice(b, 1);
                merged = true;
              }
            }
          }
        }
        if (bands.length < 2 || !alignedReviewMaskColumns(rows)) {
          group.forEach(function (index) { remaining.push(fragments[index]); });
          return;
        }
        const table = rect(fragments[group[0]].left, fragments[group[0]].top, fragments[group[0]].right, fragments[group[0]].bottom);
        let lineHeight = Infinity;
        group.forEach(function (index) {
          const f = fragments[index];
          table.left = Math.min(table.left, f.left);
          table.top = Math.min(table.top, f.top);
          table.right = Math.max(table.right, f.right);
          table.bottom = Math.max(table.bottom, f.bottom);
          lineHeight = Math.min(lineHeight, height(f));
        });
        let left = table.left - 0.012, top = table.top - 0.6 * lineHeight;
        let right = table.right + 0.012, bottom = table.bottom + 0.6 * lineHeight;
        if (pageWords.length > 0) {
          // Rules sit just outside the text column; stop short of any other text,
          // and never more than three lines away.
          let columnLeft = table.left, columnRight = table.right;
          pageWords.forEach(function (w) {
            columnLeft = Math.min(columnLeft, w.left);
            columnRight = Math.max(columnRight, w.right);
          });
          const gap = 0.3 * lineHeight;
          left = columnLeft - 0.015;
          right = columnRight + 0.015;
          top = table.top - 3 * lineHeight;
          bottom = table.bottom + 3 * lineHeight;
          pageWords.forEach(function (w) {
            const x = (w.left + w.right) / 2, y = (w.top + w.bottom) / 2;
            if (x >= table.left && x <= table.right && y >= table.top && y <= table.bottom) { return; } // the answer itself
            const besideRows = w.top < table.bottom && w.bottom > table.top;
            const withinColumns = w.left < table.right && w.right > table.left;
            if (withinColumns && w.bottom <= table.top) { top = Math.max(top, w.bottom + gap); }
            if (withinColumns && w.top >= table.bottom) { bottom = Math.min(bottom, w.top - gap); }
            if (besideRows && w.right <= table.left) { left = Math.max(left, w.right + 0.005); }
            if (besideRows && w.left >= table.right) { right = Math.min(right, w.left - 0.005); }
          });
          left = Math.min(left, table.left);
          top = Math.min(top, table.top);
          right = Math.max(right, table.right);
          bottom = Math.max(bottom, table.bottom);
        }
        const block = rect(Math.max(0, left), Math.max(0, top), Math.min(maxRight, right), Math.min(maxBottom, bottom));
        block.table = true;
        blocks.push(block);
      });
      fragments = remaining;
    }

    const parents = unionFind(fragments.length);
    for (let i = 0; i < fragments.length; ++i) {
      const a = fragments[i];
      for (let j = 0; j < i; ++j) {
        const b = fragments[j];
        // A tall visual region must not bridge unrelated text.
        const h = Math.min(height(a), height(b));
        const xGap = Math.max(a.left, b.left) - Math.min(a.right, b.right);
        const yGap = Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
        if (xGap <= 0 && yGap <= 0.8 * h) { parents.parent[parents.root(i)] = parents.root(j); }
      }
    }
    // Establish connectivity from the original fragments, not growing bounds.
    const bounds2 = new Map();
    for (let i = 0; i < fragments.length; ++i) {
      const key = parents.root(i);
      const r = fragments[i];
      if (!bounds2.has(key)) { bounds2.set(key, rect(r.left, r.top, r.right, r.bottom)); }
      else { bounds2.set(key, unite(bounds2.get(key), r)); }
    }
    const ordered = Array.from(bounds2.keys()).sort(function (a, b) { return a - b; }).map(function (k) { return bounds2.get(k); });
    const joined = joinReviewMaskBlocksAcrossBlankGaps(ordered, fragments, pageWords);
    // Drop blocks inside another block, such as the parts of an inline fraction within its paragraph.
    const result = blocks.concat(joined);
    const kept = [];
    for (let i = 0; i < result.length; ++i) {
      const a = result[i];
      let contained = false;
      for (let j = 0; j < result.length && !contained; ++j) {
        const b = result[j];
        const inside = j !== i && a.left >= b.left && a.top >= b.top && a.right <= b.right && a.bottom <= b.bottom;
        const same = a.left === b.left && a.top === b.top && a.right === b.right && a.bottom === b.bottom;
        // Of two identical blocks, keep the first.
        contained = inside && (j < i || !same);
      }
      if (!contained) { kept.push(a); }
    }
    return kept;
  }

  // PageView::reviewMaskBlocks: blocks in reading order (top, then left).
  function sortBlocks(blocks) {
    return blocks.slice().sort(function (a, b) { return a.top !== b.top ? a.top - b.top : a.left - b.left; });
  }

  root.ThoughtsViewerGeometry = {
    mergeTextLineRects: mergeTextLineRects,
    alignedReviewMaskColumns: alignedReviewMaskColumns,
    joinReviewMaskBlocksAcrossBlankGaps: joinReviewMaskBlocksAcrossBlankGaps,
    continuousReviewMaskBlocks: continuousReviewMaskBlocks,
    sortBlocks: sortBlocks
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
