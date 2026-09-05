'use strict';
const { PNG } = require('pngjs');
function readImage(buffer) { return PNG.sync.read(buffer); }
function fullImageScale(image) { return image.width <= 3000 ? 2 : 1; }
// Excess blank space around a tiny glyph makes Tesseract's word model unreliable.
// Retain all ink, including multiple digits, then give the enlarged text a small margin.
function trimInk(buffer, padding = 10) {
  const source = readImage(buffer);
  let left = source.width, right = -1, top = source.height, bottom = -1;
  for (let y = 0; y < source.height; y++) for (let x = 0; x < source.width; x++) {
    if (source.data[(y * source.width + x) * 4] < 128) {
      left = Math.min(left, x); right = Math.max(right, x);
      top = Math.min(top, y); bottom = Math.max(bottom, y);
    }
  }
  if (right < left) return null;
  const output = new PNG({ width: right - left + 1 + padding * 2, height: bottom - top + 1 + padding * 2 });
  output.data.fill(255);
  PNG.bitblt(source, output, left, top, right - left + 1, bottom - top + 1, padding, padding);
  return PNG.sync.write(output);
}
// Crop around glyphs, excluding bar borders; padding gives OCR a clean margin.
function prepareRegion(image, rectangle, { threshold = 145, scale = 3, padding = 20 } = {}) {
  const left = Math.max(0, Math.floor(rectangle.left)), top = Math.max(0, Math.floor(rectangle.top));
  const width = Math.max(1, Math.min(image.width - left, Math.ceil(rectangle.width))), height = Math.max(1, Math.min(image.height - top, Math.ceil(rectangle.height)));
  const output = new PNG({ width: width * scale + padding * 2, height: height * scale + padding * 2 }); output.data.fill(255);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const offset = ((y + top) * image.width + x + left) * 4, r = image.data[offset], g = image.data[offset + 1], b = image.data[offset + 2];
    const value = Math.min(r, g, b) >= threshold && Math.max(r, g, b) - Math.min(r, g, b) < 100 ? 0 : 255;
    for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) {
      const target = ((y * scale + dy + padding) * output.width + x * scale + dx + padding) * 4;
      output.data[target] = value; output.data[target + 1] = value; output.data[target + 2] = value;
    }
  }
  return PNG.sync.write(output);
}
function prepareForOCR(buffer, threshold = 185) {
  const image = Buffer.isBuffer(buffer) ? readImage(buffer) : buffer;
  return prepareRegion(image, { left: 0, top: 0, width: image.width, height: image.height }, { threshold, scale: fullImageScale(image), padding: 0 });
}
module.exports = { readImage, fullImageScale, prepareRegion, prepareForOCR, trimInk };
