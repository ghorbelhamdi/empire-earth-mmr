// Local diagnostic: npm run check:ocr -- "C:\\path\\to\\image.png"
// Prints recognized rows; never transmits or copies the input image.
'use strict';
const path = require('node:path');
const fs = require('node:fs');
const { createWorker } = require('tesseract.js');
const { readMilitary } = require('../src/lib/ocr');
(async () => {
  if (!process.argv[2]) throw new Error('Provide the path to a local Military screenshot.');
  const worker = await createWorker('eng', 1, { langPath: path.dirname(require.resolve('@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz')), cacheMethod: 'none', gzip: true });
  try {
    const result = await readMilitary(worker, fs.readFileSync(process.argv[2]));
    const { layout, ...report } = result;
    console.log(JSON.stringify(report, null, 2));
  } finally { await worker.terminate(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
