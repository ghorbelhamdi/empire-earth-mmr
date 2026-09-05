'use strict';
const { PNG } = require('pngjs');

// Empire Earth's white text sits on saturated blue/red/yellow artwork. Isolate
// bright, low-chroma pixels before OCR; this does not alter the evidence image.
function prepareForOCR(buffer, threshold = 145) {
  const source = PNG.sync.read(buffer);
  const scale = source.width < 1500 ? 2 : 1;
  const output = new PNG({ width: source.width * scale, height: source.height * scale });
  for (let y = 0; y < source.height; y++) for (let x = 0; x < source.width; x++) {
    const offset = (y * source.width + x) * 4;
    const [r, g, b] = source.data.subarray(offset, offset + 3);
    const ink = Math.min(r, g, b) >= threshold && Math.max(r, g, b) - Math.min(r, g, b) < 100;
    const value = ink ? 0 : 255;
    for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) {
      const target = ((y * scale + dy) * output.width + x * scale + dx) * 4;
      output.data[target] = value; output.data[target + 1] = value; output.data[target + 2] = value; output.data[target + 3] = 255;
    }
  }
  return PNG.sync.write(output);
}
module.exports = { prepareForOCR };
