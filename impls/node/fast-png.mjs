// fast-png shim. Policy: the library used as its interface asks. Palette
// images come back as indices plus a palette whose entries carry alpha when a
// tRNS chunk is present, so those are expanded here because PAM cannot carry a
// palette. For greyscale/RGB images fast-png returns the tRNS value in
// `transparency` and leaves applying it to the caller, so this shim applies it
// as a conforming caller would: the value masked to the bit depth (third
// edition 11.3.1.1), alpha 0 where every sample matches.
import { readFileSync, writeFileSync } from 'node:fs';
// 127: the decoder library is not installed (see impls/CONTRACT.md).
const notInstalled = (e) => { console.log('ERR decoder library not installed: ' + e.message); process.exit(127); };
const { decode } = await import('fast-png').catch(notInstalled);

const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/fast-png/package.json', import.meta.url))).version;
	console.log(`fast-png ${v} (node ${process.version})`);
	process.exit(0);
}
const fail = (m) => { console.log('ERR ' + m); process.exit(1); };
let img;
try { img = decode(readFileSync(input)); } catch (e) { fail(e && e.message ? e.message : String(e)); }
const { width: w, height: h, depth, channels, data, palette, transparency } = img;
console.log(`INFO width=${w} height=${h} depth=${depth} channels=${channels} palette=${palette ? palette.length : 0} transparency=${transparency ? transparency.length : 0} data=${data.constructor.name}[${data.length}]`);
// fast-png keeps sub-byte depths packed (MSB first, rows padded to a byte);
// unpack to one byte per sample so PAM can carry them (maxval 2^depth-1).
function unpack(packed, samplesPerRow, rows, bits) {
	const rowBytes = Math.ceil(samplesPerRow * bits / 8);
	if (packed.length !== rowBytes * rows) fail(`packed ${bits}-bit data is ${packed.length} bytes, expected ${rowBytes * rows}`);
	const out = Buffer.alloc(samplesPerRow * rows);
	const mask = (1 << bits) - 1;
	for (let y = 0; y < rows; y++) {
		for (let i = 0; i < samplesPerRow; i++) {
			const bit = i * bits;
			out[y * samplesPerRow + i] = (packed[y * rowBytes + (bit >> 3)] >> (8 - bits - (bit & 7))) & mask;
		}
	}
	return out;
}
let body, outChannels, maxval;
// A PLTE in a truecolour image is only a suggested palette; the data is then
// full samples, not indices. fast-png does not expose the colour type, so
// tell the cases apart by sample count.
const indexed = palette && channels === 1 && data.length === (depth < 8 ? Math.ceil(w * depth / 8) * h : w * h);
if (indexed) {
	const idx = depth < 8 ? unpack(data, w, h, depth) : data;
	if (idx.length !== w * h) fail(`palette image: ${idx.length} indices for ${w * h} pixels`);
	const hasAlpha = palette.some(p => p.length >= 4);
	outChannels = hasAlpha ? 4 : 3;
	maxval = 255;
	body = Buffer.alloc(w * h * outChannels);
	let o = 0;
	for (let i = 0; i < idx.length; i++) {
		const p = palette[idx[i]];
		if (!p) fail(`palette index ${idx[i]} outside palette of ${palette.length}`);
		body[o++] = p[0]; body[o++] = p[1]; body[o++] = p[2];
		if (hasAlpha) body[o++] = p.length >= 4 ? p[3] : 255;
	}
} else {
	outChannels = channels;
	if (depth === 16) {
		if (data.length !== w * h * channels) fail(`${data.length} samples for ${w}x${h}x${channels}`);
		maxval = 65535;
		body = Buffer.alloc(data.length * 2);
		for (let i = 0; i < data.length; i++) body.writeUInt16BE(data[i], 2 * i);
	} else if (depth === 8) {
		if (data.length !== w * h * channels) fail(`${data.length} samples for ${w}x${h}x${channels}`);
		maxval = 255;
		body = Buffer.from(data.buffer, data.byteOffset, data.length);
	} else {
		maxval = (1 << depth) - 1;
		body = unpack(data, w * channels, h, depth);
	}
	if (transparency && (channels === 1 || channels === 3)) {
		if (transparency.length !== channels) fail(`transparency has ${transparency.length} values for ${channels} channels`);
		const bps = maxval > 255 ? 2 : 1;
		const sample = (i) => bps === 2 ? body.readUInt16BE(2 * i) : body[i];
		const key = Array.from(transparency, (v) => v & maxval);
		const withAlpha = Buffer.alloc((body.length / channels) * (channels + 1));
		for (let p = 0, o = 0; p < w * h; p++) {
			let match = true;
			for (let c = 0; c < channels; c++) {
				const v = sample(p * channels + c);
				if (v !== key[c]) match = false;
				if (bps === 2) withAlpha.writeUInt16BE(v, o); else withAlpha[o] = v;
				o += bps;
			}
			if (bps === 2) withAlpha.writeUInt16BE(match ? 0 : maxval, o); else withAlpha[o] = match ? 0 : maxval;
			o += bps;
		}
		body = withAlpha;
		outChannels = channels + 1;
	}
}
const tt = ['GRAYSCALE', 'GRAYSCALE_ALPHA', 'RGB', 'RGB_ALPHA'][outChannels - 1];
writeFileSync(output, Buffer.concat([Buffer.from(`P7\nWIDTH ${w}\nHEIGHT ${h}\nDEPTH ${outChannels}\nMAXVAL ${maxval}\nTUPLTYPE ${tt}\nENDHDR\n`), body]));
console.log(`OK ${w} ${h} ${outChannels} ${maxval > 255 ? 16 : 8}`);
