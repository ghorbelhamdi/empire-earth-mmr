// Local diagnostic: npm run check:ocr -- "C:\\path\\to\\image.png"
// Prints recognized rows; never transmits or copies the input image.
'use strict';
const path = require('node:path');
const fs = require('node:fs');
const { createWorker, PSM } = require('tesseract.js');
const { parseMilitary, linesFromBlocks } = require('../src/lib/parser');
const { prepareForOCR } = require('../src/lib/image');
(async () => {
  if (!process.argv[2]) throw new Error('Provide the path to a local Military screenshot.');
  const worker = await createWorker('eng', 1, { langPath: path.dirname(require.resolve('@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz')), cacheMethod: 'none', gzip: true });
  try {
    await worker.setParameters({ tessedit_pageseg_mode: PSM.SPARSE_TEXT });
    const { data } = await worker.recognize(prepareForOCR(fs.readFileSync(process.argv[2]), process.argv[3] ? Number(process.argv[3]) : undefined), {}, { text: true, blocks: true });
    console.log(JSON.stringify({ ...parseMilitary(data), aligned: linesFromBlocks(data.blocks) }, null, 2));
  } finally { await worker.terminate(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
