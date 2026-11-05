const fs = require('fs');
const path = require('path');

function build4PageVisualPdf() {
  const images = [
    'C:/PrayerTimingsAutomation/guide_images/page1_user_guide.jpg',
    'C:/PrayerTimingsAutomation/guide_images/page2_user_guide.jpg',
    'C:/PrayerTimingsAutomation/guide_images/page3_user_guide.jpg',
    'C:/PrayerTimingsAutomation/guide_images/page4_user_guide.jpg'
  ];

  // A4 dimensions in points (72 DPI)
  const pageWidth = 595.28;
  const pageHeight = 841.89;

  // Aspect ratio: 950 x 1350 = 0.7037037
  const aspect = 950 / 1350;
  const maxW = pageWidth - 28; // 567.28
  const maxH = pageHeight - 28; // 813.89

  let renderW = maxW;
  let renderH = maxW / aspect; // ~806.12 pt
  if (renderH > maxH) {
    renderH = maxH;
    renderW = maxH * aspect;
  }

  const posX = ((pageWidth - renderW) / 2).toFixed(2);
  const posY = ((pageHeight - renderH) / 2).toFixed(2);
  const rW = renderW.toFixed(2);
  const rH = renderH.toFixed(2);

  // PDF Structure:
  // Obj 1: Catalog
  // Obj 2: Pages (Kids [3, 4, 5, 6])
  // Obj 3: Page 1 (Contents 7, Resources << /XObject << /Im1 11 >> >>)
  // Obj 4: Page 2 (Contents 8, Resources << /XObject << /Im2 12 >> >>)
  // Obj 5: Page 3 (Contents 9, Resources << /XObject << /Im3 13 >> >>)
  // Obj 6: Page 4 (Contents 10, Resources << /XObject << /Im4 14 >> >>)
  // Obj 7: Content stream 1
  // Obj 8: Content stream 2
  // Obj 9: Content stream 3
  // Obj 10: Content stream 4
  // Obj 11: Image 1 XObject
  // Obj 12: Image 2 XObject
  // Obj 13: Image 3 XObject
  // Obj 14: Image 4 XObject

  const buffers = [];
  const offsets = [];
  let currentOffset = 0;

  function write(chunk) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, 'latin1');
    buffers.push(buf);
    currentOffset += buf.length;
  }

  // Header
  write('%PDF-1.4\n%\xE2\xE3\xCF\xD3\n');

  // Object tracking
  const objIndices = {};

  function startObj(num) {
    offsets[num] = currentOffset;
    write(`${num} 0 obj\n`);
  }

  function endObj() {
    write('endobj\n');
  }

  // 1: Catalog
  startObj(1);
  write('<< /Type /Catalog /Pages 2 0 R >>\n');
  endObj();

  // 2: Pages
  startObj(2);
  write('<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R 6 0 R] /Count 4 >>\n');
  endObj();

  // 3-6: Pages
  for (let i = 0; i < 4; i++) {
    const pageNum = 3 + i;
    const contentNum = 7 + i;
    const imgNum = 11 + i;
    startObj(pageNum);
    write(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] /Contents ${contentNum} 0 R /Resources << /XObject << /Im${i + 1} ${imgNum} 0 R >> >> >>\n`);
    endObj();
  }

  // 7-10: Content streams
  for (let i = 0; i < 4; i++) {
    const contentNum = 7 + i;
    const stream = `q\n${rW} 0 0 ${rH} ${posX} ${posY} cm\n/Im${i + 1} Do\nQ\n`;
    const streamBuf = Buffer.from(stream, 'ascii');
    startObj(contentNum);
    write(`<< /Length ${streamBuf.length} >>\nstream\n`);
    write(streamBuf);
    write('\nendstream\n');
    endObj();
  }

  // 11-14: Image XObjects
  for (let i = 0; i < 4; i++) {
    const imgNum = 11 + i;
    const imgBuf = fs.readFileSync(images[i]);
    startObj(imgNum);
    write(`<< /Type /XObject /Subtype /Image /Width 950 /Height 1350 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${imgBuf.length} >>\nstream\n`);
    write(imgBuf);
    write('\nendstream\n');
    endObj();
  }

  // Cross-reference table
  const startXref = currentOffset;
  const numObjects = 15; // 0 to 14
  write(`xref\n0 ${numObjects}\n`);
  write('0000000000 65535 f \n');
  for (let i = 1; i < numObjects; i++) {
    const offStr = String(offsets[i]).padStart(10, '0');
    write(`${offStr} 00000 n \n`);
  }

  // Trailer
  write(`trailer\n<< /Size ${numObjects} /Root 1 0 R >>\nstartxref\n${startXref}\n%%EOF\n`);

  const fullPdf = Buffer.concat(buffers);
  const out1 = 'C:/PrayerTimingsAutomation/prayer_times_user_guide.pdf';
  const out2 = 'C:/PrayerTimingsAutomation/guide_images/prayer_times_user_guide.pdf';
  fs.writeFileSync(out1, fullPdf);
  fs.writeFileSync(out2, fullPdf);
  console.log(`4-Page Visual PDF created successfully!`);
  console.log(` - ${out1} (${fullPdf.length} bytes)`);
  console.log(` - ${out2} (${fullPdf.length} bytes)`);
}

build4PageVisualPdf();

