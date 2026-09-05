'use strict';
const { PNG } = require('pngjs');
function readImage(buffer) { return PNG.sync.read(buffer); }
function fullImageScale(image) { return image.width <= 3000 ? 2 : 1; }
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
module.exports = { readImage, fullImageScale, prepareRegion, prepareForOCR };
