// fast-png decode, with its own exported converter for indexed images. The gray/RGB
// tRNS key it returns as `transparency` is left as it is: the shim applies nothing.
// Conversion and output use one row; the decoded buffer is the library's own.
import { readFileSync } from 'node:fs';
import { api, assert, Pam, decoderError, notInstalled } from './pam.mjs';
const { decode, convertIndexedToRgb } = await import('fast-png').catch(notInstalled);
const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/fast-png/package.json', import.meta.url))).version;
	console.log(`fast-png ${v} (node ${process.version})`); process.exit(0);
}
let img;
try { img = decode(readFileSync(input)); } catch (e) { decoderError(e); }
const { width: w, height: h, depth, channels, data, palette, transparency } = img;
console.log(`INFO width=${w} height=${h} depth=${depth} channels=${channels} palette=${palette ? palette.length : 0} transparency=${transparency ? transparency.length : 0}`);
// The result carries no color type. A palette image is one channel of indices plus a
// palette, which is all a caller can go by and what the library's converter goes by.
const indexed = !!palette && channels === 1;
api([1, 2, 4, 8, 16].includes(depth) && channels >= 1 && channels <= 4, `unexpected output depth/channels: ${depth}/${channels}`);
const samplesPerRow = w * channels;
const stride = depth < 8 ? Math.ceil(samplesPerRow * depth / 8) : samplesPerRow;
api(data.length === stride * h, `${data.length} samples, expected ${stride * h}`);
const maxval = indexed ? 255 : (1 << depth) - 1;
const bps = maxval > 255 ? 2 : 1;
// For a palette image the converter decides the channel count (RGB, or RGBA with tRNS).
let outChannels = indexed ? 0 : channels, out = null;
const open = (n) => { outChannels = n; out = new Pam(output, w, h, n, maxval); };
if (!indexed) open(outChannels);
const row = indexed ? null : Buffer.alloc(w * outChannels * bps);
for (let y = 0; y < h; y++) {
	const source = data.subarray(y * stride, (y + 1) * stride);
	if (indexed) {
		let converted;
		try { converted = convertIndexedToRgb({ ...img, height: 1, data: source }); }
		catch (e) { decoderError(e); }
		if (!out) open(converted.length / w);
		out.row(converted);
		continue;
	}
	const sample = i => depth < 8 ? (source[(i * depth) >> 3] >> (8 - depth - ((i * depth) & 7))) & maxval : source[i];
	let offset = 0;
	const put = value => {
		api(Number.isInteger(value) && value >= 0 && value <= maxval, `sample ${value} exceeds ${maxval}`);
		if (bps === 2) row.writeUInt16BE(value, offset); else row[offset] = value;
		offset += bps;
	};
	for (let i = 0; i < samplesPerRow; i++) put(sample(i));
	assert(offset === row.length, `wrote ${offset} of ${row.length} row bytes`); out.row(row);
}
if (!out) open(3); // no rows: the writer reports the empty image
out.close();
console.log(`OK ${w} ${h} ${outChannels} ${bps * 8}`);
