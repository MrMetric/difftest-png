// pngjs shim. Policy: PNG.sync.read with skipRescale, which keeps 16-bit data
// and also leaves sub-byte samples unscaled (one byte per sample, values up to
// 2^depth-1, written with that maxval); pngjs always returns RGBA with palette
// and tRNS applied.
import { readFileSync, writeFileSync } from 'node:fs';
// 127: the decoder library is not installed (see impls/CONTRACT.md).
const notInstalled = (e) => { console.log('ERR decoder library not installed: ' + e.message); process.exit(127); };
const { default: pngjs } = await import('pngjs').catch(notInstalled);
const { PNG } = pngjs;

const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/pngjs/package.json', import.meta.url))).version;
	console.log(`pngjs ${v} (node ${process.version})`);
	process.exit(0);
}
const fail = (m) => { console.log('ERR ' + m); process.exit(1); };
let png;
try { png = PNG.sync.read(readFileSync(input), { skipRescale: true }); } catch (e) { fail(e && e.message ? e.message : String(e)); }
const { width: w, height: h, depth, data } = png;
console.log(`INFO width=${w} height=${h} depth=${depth} colorType=${png.colorType} data=${data.constructor.name}[${data.length}] interlace=${png.interlace} palette=${png.palette} alpha=${png.alpha}`);
let body, maxval;
if (depth === 16) {
	// skipRescale hands back a Uint16Array of host-endian samples
	if (!(data instanceof Uint16Array) || data.length !== w * h * 4) fail(`unexpected 16-bit data ${data.constructor.name}[${data.length}] for ${w}x${h}`);
	maxval = 65535;
	body = Buffer.alloc(data.length * 2);
	for (let i = 0; i < data.length; i++) body.writeUInt16BE(data[i], 2 * i);
} else {
	if (data.length !== w * h * 4) fail(`unexpected data length ${data.length} for ${w}x${h}`);
	maxval = depth < 8 && !png.palette ? (1 << depth) - 1 : 255;
	body = data;
}
writeFileSync(output, Buffer.concat([Buffer.from(`P7\nWIDTH ${w}\nHEIGHT ${h}\nDEPTH 4\nMAXVAL ${maxval}\nTUPLTYPE RGB_ALPHA\nENDHDR\n`), body]));
console.log(`OK ${w} ${h} 4 ${maxval > 255 ? 16 : 8}`);
