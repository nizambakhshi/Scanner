import { test } from "node:test";
import assert from "node:assert/strict";
import { sparklineGeometry } from "../lib/sparkline";

/**
 * The glyph is small enough that a wrong tone is the whole message: green on a
 * falling series is worse than no glyph at all. The geometry is pure, so the
 * direction rule and the box arithmetic are pinned down here.
 */

const points = (path: string) =>
  [...path.matchAll(/[ML](\d+\.\d+) (\d+\.\d+)/g)].map((match) => ({ x: Number(match[1]), y: Number(match[2]) }));

test("fewer than two points is not a line", () => {
  assert.equal(sparklineGeometry([]), null);
  assert.equal(sparklineGeometry([42]), null);
});

test("a rising series is green and ends at the right edge, at the top", () => {
  const geometry = sparklineGeometry([10, 12, 14], 100, 24);
  assert.ok(geometry);
  assert.equal(geometry.tone, "positive");
  assert.equal(geometry.lastX, 98); // pad 2 … width − pad
  assert.equal(geometry.lastY, 2); // the max sits at the top pad
  assert.equal(geometry.first, 10);
  assert.equal(geometry.last, 14);
  assert.equal(points(geometry.path).length, 3);
});

test("a falling series is red", () => {
  assert.equal(sparklineGeometry([14, 12, 10], 100, 24)?.tone, "negative");
});

test("a close that lands where it started claims no direction", () => {
  // A dip and recovery nets to zero — grey, not green.
  assert.equal(sparklineGeometry([12, 10, 12], 100, 24)?.tone, "muted");
});

test("a flat series draws mid-height and claims no direction", () => {
  const geometry = sparklineGeometry([5, 5, 5], 96, 24);
  assert.ok(geometry);
  assert.equal(geometry.tone, "muted");
  assert.equal(geometry.lastY, 12);
});

test("nothing is clipped outside the padded box", () => {
  const geometry = sparklineGeometry([0, 100, 50, 25, 80], 96, 24);
  assert.ok(geometry);
  for (const point of points(geometry.path)) {
    assert.ok(point.x! >= 2 && point.x! <= 94, `x ${point.x} outside the box`);
    assert.ok(point.y! >= 2 && point.y! <= 22, `y ${point.y} outside the box`);
  }
});

test("the dot marks the newest close, not the extreme", () => {
  const geometry = sparklineGeometry([10, 30, 20], 96, 24);
  assert.ok(geometry);
  assert.equal(geometry.last, 20);
  assert.equal(geometry.lastY, 12); // half of the 10…30 range
});
